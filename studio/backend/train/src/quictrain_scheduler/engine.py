from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Protocol

from quictrain_core import (
    JobState,
    LaunchSpec,
    ProviderJob,
    ProviderState,
    infer_artifact_kind,
    new_id,
)
from quictrain_core.materialization import SCHEDULABLE_READINESS
from quictrain_core.provider import ComputeProvider
from quictrain_model_specs import get_model
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from quictrain_api.db import (
    ArtifactRecord,
    AttemptRecord,
    DatasetVersionRecord,
    JobLogRecord,
    JobRecord,
    ResourceProfileRecord,
    append_event,
    utcnow,
)
from quictrain_api.service import transition_job

RUNTIME_JOB_SPEC_PATH = "/tmp/quictrain-job-spec.json"  # nosec B108 - container-local handoff path
# Quota4090 OSS mirrors of CPFS π0.5 weights still embed absolute
# `/mnt/cpfs/.../tokenizer` in policy_preprocessor.json. Rebuild a writable
# overlay under QUICTRAIN_WORK_ROOT and rewrite tokenizer_name before Runner.
PI05_OSS_PRETRAINED_REBIND = (
    'python -c "'
    "import json,os,shutil; from pathlib import Path; "
    f"spec_path=Path('{RUNTIME_JOB_SPEC_PATH}'); "
    "spec=json.loads(spec_path.read_text(encoding='utf-8')); "
    "src=Path((spec.get('resolved_config') or {}).get('pretrained_path') or ''); "
    "work=Path(os.environ.get('QUICTRAIN_WORK_ROOT','/quictrain/work')); "
    "dst=work/'pi05_pretrained'; "
    "assert src.is_dir(), f'pretrained missing: {src}'; "
    "dst.mkdir(parents=True, exist_ok=True); "
    "["
    "(lambda p,t: ("
    "t.unlink(missing_ok=True) if (t.exists() or t.is_symlink()) else None, "
    "t.symlink_to(p) if (p.is_dir() or p.suffix=='.safetensors') else "
    "(shutil.copy2(p,t) if p.is_file() else None)"
    "))(p, dst/p.name) for p in src.iterdir()"
    "]; "
    "prep=dst/'policy_preprocessor.json'; "
    "data=json.loads(prep.read_text(encoding='utf-8')); "
    "tok=str(dst/'tokenizer'); "
    "["
    "step.setdefault('config',{}).__setitem__('tokenizer_name', tok) "
    "for step in (data.get('steps') or []) "
    "if 'tokenizer_name' in (step.get('config') or {})"
    "]; "
    "prep.write_text(json.dumps(data, indent=2), encoding='utf-8'); "
    "spec.setdefault('resolved_config',{})['pretrained_path']=str(dst); "
    "spec_path.write_text(json.dumps(spec, separators=(',',':'), ensure_ascii=False), encoding='utf-8'); "
    "print('pi05_pretrained_rebind', dst, tok)"
    '"'
)


def runtime_bootstrap_command(*, rebind_pi05_oss_pretrained: bool = False) -> str:
    """DLC user_command: materialize JobSpec, optional π0.5 OSS rebind, run Runner."""

    write_spec = (
        "python -c 'import os,pathlib; "
        f'pathlib.Path("{RUNTIME_JOB_SPEC_PATH}").write_text('
        'os.environ["QUICTRAIN_JOB_SPEC_JSON"],encoding="utf-8")\''
    )
    run_runner = (
        "python -m quictrain_runner run "
        f'--job-spec {RUNTIME_JOB_SPEC_PATH} --plugin "$QUICTRAIN_RUNTIME_PLUGIN"'
    )
    mirror = (
        'if [ -n "${QUICTRAIN_CONTROL_OUTPUT_DIR:-}" ]; then '
        "python -c 'import os,shutil; from pathlib import Path; "
        'src=Path(os.environ["QUICTRAIN_OUTPUT_DIR"]); '
        'dst=Path(os.environ["QUICTRAIN_CONTROL_OUTPUT_DIR"]); '
        'allowed={".json",".jsonl",".log",".txt",".yaml",".yml"}; '
        "copy=lambda p:(dst.joinpath(p.relative_to(src)).parent.mkdir(parents=True,exist_ok=True),"
        "shutil.copy2(p,dst.joinpath(p.relative_to(src)))); "
        '[copy(p) for p in src.rglob("*") if p.is_file() and p.suffix.lower() in allowed '
        'and p.stat().st_size <= 2097152 and p.name not in {"artifact_manifest.json","_SUCCESS"}]; '
        'copy(src/"artifact_manifest.json"); copy(src/"_SUCCESS")\'; '
        "fi"
    )
    parts = [write_spec]
    if rebind_pi05_oss_pretrained:
        parts.append(PI05_OSS_PRETRAINED_REBIND)
    parts.extend([run_runner, mirror])
    return " && ".join(parts)


# Backward-compatible default (H20 / non-OSS π0.5 path).
RUNTIME_BOOTSTRAP_COMMAND = runtime_bootstrap_command()
LOGGER = logging.getLogger(__name__)
ACTIVE_JOB_STATES = [
    JobState.QUEUED.value,
    JobState.SUBMITTING.value,
    JobState.ORPHANED.value,
    JobState.PROVISIONING.value,
    JobState.RUNNING.value,
    JobState.CANCEL_REQUESTED.value,
]


def parse_lerobot_metrics(message: str) -> dict[str, float]:
    """Parse both QuicTrain smoke logs and LeRobot MetricsTracker output."""

    aliases = {
        "loss": "train/loss",
        "lr": "train/lr",
        "grdn": "train/grad_norm",
        "updt_s": "train/update_seconds",
        "data_s": "train/dataloading_seconds",
        "epch": "train/epoch",
        "throughput": "train/throughput",
    }
    metrics: dict[str, float] = {}
    for key, value in re.findall(
        r"(?:^|\s)(loss|lr|grdn|updt_s|data_s|epch|throughput)[:=]([0-9.eE+-]+)",
        message,
    ):
        try:
            metrics[aliases[key]] = float(value)
        except ValueError:
            continue
    return metrics


class ArtifactClient(Protocol):
    def read_json(self, uri: str) -> dict[str, Any]: ...

    def exists(self, uri: str) -> bool: ...


class TrackingClient(Protocol):
    def create_run(self, tags: dict[str, str]) -> tuple[str, str]: ...

    def log_metrics(self, run_id: str, metrics: dict[str, float], step: int) -> None: ...

    def finish(self, run_id: str, status: str) -> None: ...


class Scheduler:
    def __init__(
        self,
        session_factory: sessionmaker,
        provider: ComputeProvider,
        *,
        providers: dict[str, ComputeProvider] | None = None,
        artifact_root: str = "./artifacts",
        artifact_client: ArtifactClient | None = None,
        tracking_client: TrackingClient | None = None,
        cpfs_data_source_id: str | None = None,
        cpfs_vpc_data_source_id: str | None = None,
        cpfs_vpc_mount_target: str | None = None,
        cpfs_root_uri: str | None = None,
        cpfs_mount_path: str = "/mnt/cpfs",
        cpfs_workspace_dir: str = "quictrain",
        cpfs_pi05_pretrained_dir: str = "models/pi05/lerobot-pi05-base",
        dlc_user_vpc: dict[str, object] | None = None,
        oss_cpfs_mirror_prefix: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.provider = provider
        self.providers: dict[str, ComputeProvider] = dict(providers or {})
        default_id = self._provider_id_of(provider)
        self.providers.setdefault(default_id, provider)
        self.artifact_root = artifact_root.rstrip("/")
        self.artifact_client = artifact_client
        self.tracking_client = tracking_client
        self.cpfs_data_source_id = cpfs_data_source_id
        self.cpfs_vpc_data_source_id = cpfs_vpc_data_source_id
        self.cpfs_vpc_mount_target = cpfs_vpc_mount_target
        self.cpfs_root_uri = cpfs_root_uri.rstrip("/") if cpfs_root_uri else None
        self.cpfs_mount_path = cpfs_mount_path.rstrip("/")
        self.cpfs_workspace_dir = cpfs_workspace_dir.strip("/")
        self.cpfs_pi05_pretrained_dir = cpfs_pi05_pretrained_dir.strip("/")
        self.dlc_user_vpc = dlc_user_vpc
        self.oss_cpfs_mirror_prefix = (
            oss_cpfs_mirror_prefix.rstrip("/") if oss_cpfs_mirror_prefix else None
        )

    @staticmethod
    def _provider_id_of(provider: ComputeProvider) -> str:
        explicit = getattr(provider, "provider_id", None)
        if isinstance(explicit, str) and explicit:
            return explicit
        name = provider.__class__.__name__
        if name == "FakeProvider":
            return "fake"
        if "Aliyun" in name or "DLC" in name:
            return "aliyun_dlc"
        return name.lower()

    def _resolve_provider(self, session: Session, job: JobRecord) -> tuple[ComputeProvider, str]:
        """Pick provider from ResourceProfile.provider_id; fall back to default pool."""

        wanted = self._provider_id_of(self.provider)
        profile = session.get(ResourceProfileRecord, job.resource_profile_id)
        if profile is not None and profile.provider_id:
            wanted = profile.provider_id
        if wanted in self.providers:
            return self.providers[wanted], wanted
        # Incomplete registry (typical Fake-only CI): use injected default pool.
        return self.provider, self._provider_id_of(self.provider)

    def run_once(self) -> bool:
        from .exporter import Exporter
        from .materializer import Materializer

        materializer_worked = Materializer(self.session_factory).run_once()
        exporter_worked = Exporter(self.session_factory).run_once()

        with self.session_factory() as session:
            job_ids = list(
                session.scalars(
                    select(JobRecord.id)
                    .where(JobRecord.state.in_(ACTIVE_JOB_STATES))
                    .order_by(JobRecord.priority.desc(), JobRecord.created_at.asc())
                    .limit(100)
                )
            )
        if not job_ids and not materializer_worked and not exporter_worked:
            return False

        # Reconcile each active job independently. A long-running queued job or
        # one provider read failure must not starve later submissions.
        for job_id in job_ids:
            try:
                with self.session_factory() as session:
                    job = session.get(JobRecord, job_id)
                    if job is None or job.state not in ACTIVE_JOB_STATES:
                        continue
                    self._reconcile(session, job)
                    session.commit()
            except Exception:
                LOGGER.exception("Failed to reconcile job %s", job_id)
        return True

    def _reconcile(self, session: Session, job: JobRecord) -> None:
        if job.state == JobState.QUEUED.value:
            self._create_and_submit(session, job)
            return

        attempt = job.attempts[-1] if job.attempts else None
        if attempt is None:
            job.state = JobState.QUEUED.value
            append_event(session, job, "job.warning", {"message": "Missing attempt; requeued"})
            return

        provider, _provider_id = self._resolve_provider(session, job)

        if job.state == JobState.ORPHANED.value:
            found = provider.find_by_idempotency_key(attempt.idempotency_key)
            if found is None:
                transition_job(session, job, JobState.SUBMITTING)
                self._submit_attempt(session, job, attempt)
            else:
                attempt.external_job_id = found.external_id
                transition_job(session, job, JobState.PROVISIONING)
            return

        if job.state == JobState.CANCEL_REQUESTED.value:
            if attempt.external_job_id:
                provider_job = provider.cancel(attempt.external_job_id)
                attempt.provider_state = provider_job.raw_status
            attempt.state = "CANCELLED"
            attempt.finished_at = utcnow()
            self._finish_tracking(attempt, "KILLED")
            transition_job(session, job, JobState.CANCELLED, "job.cancelled")
            return

        if not attempt.external_job_id:
            transition_job(session, job, JobState.ORPHANED, "job.warning")
            return

        provider_job = provider.get(attempt.external_job_id)
        attempt.provider_state = provider_job.raw_status
        attempt.provider_payload = {
            **(attempt.provider_payload or {}),
            "reason_code": provider_job.reason_code,
            "reason_message": provider_job.message,
        }
        if provider_job.state == ProviderState.PROVISIONING:
            job.stage = "QUEUE" if provider_job.raw_status == "Queuing" else "PROVISION"
            job.queue_reason = self._provider_summary(provider_job)
        else:
            job.queue_reason = None
        self._ingest_logs(session, job, attempt)
        self._enforce_max_runtime(session, job, attempt)
        if job.state in {JobState.CANCELLED.value, JobState.CANCEL_REQUESTED.value}:
            return
        self._apply_provider_state(session, job, attempt, provider_job)

    def _provider_for_attempt(
        self, session: Session, job: JobRecord, attempt: AttemptRecord
    ) -> ComputeProvider:
        if attempt.provider and attempt.provider in self.providers:
            return self.providers[attempt.provider]
        provider, _ = self._resolve_provider(session, job)
        return provider

    def _enforce_max_runtime(
        self, session: Session, job: JobRecord, attempt: AttemptRecord
    ) -> None:
        from quictrain_api.ops import max_runtime_seconds_for_job

        if job.started_at is None:
            return
        limit = max_runtime_seconds_for_job(session, job.project_id)
        started = job.started_at
        if started.tzinfo is None:
            from datetime import UTC

            started = started.replace(tzinfo=UTC)
        elapsed = (utcnow() - started).total_seconds()
        if elapsed <= limit:
            return
        if attempt.external_job_id:
            try:
                self._provider_for_attempt(session, job, attempt).cancel(attempt.external_job_id)
            except Exception:
                LOGGER.exception("runtime cancel failed for %s", job.id)
        attempt.state = "CANCELLED"
        attempt.finished_at = utcnow()
        job.failure_category = "RUNTIME_LIMIT_EXCEEDED"
        job.failure_message = f"超过项目最大 runtime {limit}s"
        self._finish_tracking(attempt, "KILLED")
        if job.state != JobState.CANCEL_REQUESTED.value:
            transition_job(
                session,
                job,
                JobState.CANCEL_REQUESTED,
                "job.cancel_requested",
                {"reason": "max_runtime_seconds", "limit": limit},
            )
        transition_job(
            session,
            job,
            JobState.CANCELLED,
            "job.cancelled",
            {"reason": "max_runtime_seconds", "limit": limit},
        )
        try:
            from quictrain_api.alerts import emit_alert

            emit_alert(
                severity="warning",
                title="job_runtime_limit_exceeded",
                details={"job_id": job.id, "limit": limit},
            )
        except Exception:
            LOGGER.exception("alert emit failed")

    def _create_and_submit(self, session: Session, job: JobRecord) -> None:
        number = len(job.attempts) + 1
        _provider, provider_id = self._resolve_provider(session, job)
        attempt = AttemptRecord(
            id=new_id("att"),
            job_id=job.id,
            number=number,
            state="SUBMITTING",
            provider=provider_id,
            idempotency_key=f"{job.id}:attempt:{number}",
            mlflow_run_id=None,
        )
        session.add(attempt)
        session.flush()
        if self.tracking_client is not None:
            try:
                run_id, experiment_id = self.tracking_client.create_run(
                    {
                        "quictrain.job_id": job.id,
                        "quictrain.attempt_id": attempt.id,
                        "quictrain.idempotency_key": attempt.idempotency_key,
                        "quictrain.model_id": job.model_id,
                        "quictrain.dataset_version_id": job.dataset_version_id,
                    }
                )
                attempt.mlflow_run_id = run_id
                attempt.provider_payload = {"mlflow_experiment_id": experiment_id}
            except Exception as exc:
                LOGGER.warning("MLflow run creation failed for %s: %s", job.id, exc)
        append_event(
            session, job, "job.attempt_created", {"attempt_id": attempt.id, "number": number}
        )
        transition_job(session, job, JobState.SUBMITTING)
        self._submit_attempt(session, job, attempt)

    def _submit_attempt(self, session: Session, job: JobRecord, attempt: AttemptRecord) -> None:
        model = get_model(job.model_id)
        dataset = session.get(DatasetVersionRecord, job.dataset_version_id)
        if dataset is None:
            raise RuntimeError(f"Dataset disappeared before submission: {job.dataset_version_id}")
        if dataset.status not in SCHEDULABLE_READINESS:
            transition_job(
                session,
                job,
                JobState.FAILED,
                "job.failed",
                {
                    "reason": "DATASET_NOT_READY",
                    "dataset_status": dataset.status,
                },
            )
            job.failure_category = "DATASET_NOT_READY"
            job.failure_message = (
                f"Dataset {dataset.id} is {dataset.status}; only READY is schedulable."
            )
            attempt.state = "FAILED"
            attempt.finished_at = utcnow()
            return
        resource = session.get(ResourceProfileRecord, job.resource_profile_id)
        gpu_count = getattr(resource, "gpu_count", 1) if resource is not None else 1
        gpu_models = getattr(resource, "gpu_models", []) if resource is not None else []
        pool_id = getattr(resource, "pool_id", None) if resource is not None else None
        provider, provider_id = self._resolve_provider(session, job)
        attempt.provider = provider_id
        from quictrain_api.pools import resolve_pool_resource_id, scale_worker_resources

        gpu_model = gpu_models[0] if gpu_models else "H20"
        scaled = scale_worker_resources(gpu_count, gpu_model=gpu_model, model_id=job.model_id)
        pool_resource_id = resolve_pool_resource_id(pool_id)
        # Quota4090 (ECS) ResourceAllocateFailed on BMCPFS VPC datasets today.
        # Use OSS mounts + UserVpc so jobs JobEnqueue and run when GPUs free.
        use_ecs_oss_path = bool(pool_id and "4090" in pool_id)
        cpfs_workspace = None if use_ecs_oss_path else self._cpfs_workspace_path()
        local_work_root = "/quictrain/work"
        output_uri = self._artifact_uri(job.id, attempt.id)
        if use_ecs_oss_path and self.artifact_root.startswith("oss://"):
            output_uri = f"{self.artifact_root}/{job.id}/{attempt.id}"
        output_mount = "/quictrain/output"
        output_dir = (
            f"{cpfs_workspace}/runs/{job.id}/{attempt.id}"
            if cpfs_workspace
            else f"{output_mount}/{job.id}/{attempt.id}"
            if output_uri
            else f"./artifacts/{job.id}/{attempt.id}"
        )
        control_prefix = (
            f"{self.artifact_root}/{job.id}/{attempt.id}"
            if (cpfs_workspace or use_ecs_oss_path) and self.artifact_root.startswith("oss://")
            else None
        )
        control_mount = "/quictrain/control"
        control_output_dir = f"{control_mount}/{job.id}/{attempt.id}" if control_prefix else None
        dataset_payload = dict(dataset.manifest)
        mounts: list[dict[str, str]] = []
        if cpfs_workspace:
            mounts.append(self._cpfs_mount_for_pool(pool_id))
        materialized = (dataset.materialized_uri or "").strip()
        if use_ecs_oss_path and dataset.uri.startswith("oss://"):
            dataset_payload["root"] = "/quictrain/input/dataset"
            if materialized:
                dataset_payload["materialized_uri"] = materialized
            mounts.append(
                {
                    "source_uri": dataset.uri,
                    "target": "/quictrain/input/dataset",
                    "read_only": "true",
                }
            )
        elif use_ecs_oss_path and dataset.uri.startswith(("bmcpfs://", "cpfs://")):
            # Quota4090 cannot mount BMCPFS (ResourceAllocateFailed). Require an
            # OSS mirror of the CPFS path and mount that for training I/O.
            oss_dataset = self._oss_mirror_uri(dataset.uri)
            if not oss_dataset:
                raise RuntimeError(
                    "Quota4090 requires an OSS mirror of CPFS datasets. "
                    "Set QUICTRAIN_OSS_CPFS_MIRROR_PREFIX (e.g. oss://bucket/quictrain) "
                    f"and sync {dataset.uri} under that prefix."
                )
            dataset_payload["root"] = "/quictrain/input/dataset"
            dataset_payload["uri"] = oss_dataset
            dataset_payload["source_uri"] = dataset.uri
            dataset_payload["ecs_oss_path"] = True
            if materialized:
                dataset_payload["materialized_uri"] = materialized
            mounts.append(
                {
                    "source_uri": oss_dataset,
                    "target": "/quictrain/input/dataset",
                    "read_only": "true",
                }
            )
        elif materialized:
            # Prefer idempotent CPFS/local layout produced by the materializer.
            dataset_root = materialized
            if (
                cpfs_workspace
                and self.cpfs_mount_path
                and materialized.startswith(f"{cpfs_workspace}/")
            ):
                relative = materialized.removeprefix(cpfs_workspace).lstrip("/")
                dataset_root = f"{self.cpfs_mount_path}/{relative}"
            dataset_payload["root"] = dataset_root
            dataset_payload["materialized_uri"] = materialized
        elif dataset.uri.startswith(("bmcpfs://", "cpfs://")):
            if not cpfs_workspace or not self.cpfs_root_uri:
                raise RuntimeError("CPFS dataset selected but CPFS workspace is not configured")
            elif not dataset.uri.startswith(f"{self.cpfs_root_uri}/"):
                raise RuntimeError("CPFS dataset is outside the configured QuicTrain filesystem")
            else:
                relative = dataset.uri.removeprefix(self.cpfs_root_uri).lstrip("/")
                dataset_payload["root"] = f"{self.cpfs_mount_path}/{relative}"
        elif dataset.uri.startswith("oss://"):
            dataset_payload["root"] = "/quictrain/input/dataset"
            mounts.append(
                {
                    "source_uri": dataset.uri,
                    "target": "/quictrain/input/dataset",
                    "read_only": "true",
                }
            )
        if control_prefix:
            mounts.append(
                {
                    "source_uri": self.artifact_root,
                    "target": control_mount,
                    "read_only": "false",
                }
            )
        elif output_uri:
            mounts.append(
                {
                    # Mount the already-validated OSS root and create the
                    # immutable job/attempt directories inside it. This is the
                    # exact layout used by the successful direct Runner canary.
                    "source_uri": self.artifact_root,
                    "target": output_mount,
                    "read_only": "false",
                }
            )

        resolved_config = json.loads(json.dumps(job.resolved_config))
        if output_uri:
            resolved_config["runtime"]["output_uri"] = output_uri
        if model.id == "pi05":
            if cpfs_workspace:
                resolved_config["pretrained_path"] = (
                    f"{cpfs_workspace}/{self.cpfs_pi05_pretrained_dir}"
                )
            elif use_ecs_oss_path:
                # Mirror CPFS pretrained weights under OSS for Quota4090.
                pretrained_cpfs = (
                    f"{self.cpfs_root_uri}/{self.cpfs_workspace_dir}/"
                    f"{self.cpfs_pi05_pretrained_dir}"
                    if self.cpfs_root_uri
                    else None
                )
                pretrained_oss = self._oss_mirror_uri(pretrained_cpfs) if pretrained_cpfs else None
                if not pretrained_oss:
                    raise RuntimeError(
                        "Quota4090 π0.5 requires OSS-mirrored pretrained weights. "
                        "Sync models/pi05/lerobot-pi05-base under "
                        "QUICTRAIN_OSS_CPFS_MIRROR_PREFIX."
                    )
                mounts.append(
                    {
                        "source_uri": pretrained_oss,
                        "target": "/quictrain/input/pretrained",
                        "read_only": "true",
                    }
                )
                resolved_config["pretrained_path"] = "/quictrain/input/pretrained"
        job_spec = {
            "protocol_version": "v1alpha1",
            "job_id": job.id,
            "attempt_id": attempt.id,
            "model_id": model.id,
            "model_version_id": model.version_id,
            "recipe_id": job.recipe_id,
            "adapter_version": model.adapter_version,
            "dataset": dataset_payload,
            "resolved_config": resolved_config,
            "source": job.source_snapshot,
            "output_dir": output_dir,
            "mlflow": {
                "run_id": attempt.mlflow_run_id,
            },
        }
        if output_uri:
            job_spec["output_uri"] = output_uri
        # The published v0.2.0 runtime image validates this immutable schema and
        # predates the control mirror fields. Keep its JobSpec compatible; the
        # bootstrap command mirrors only small inspectable files after Runner
        # has atomically completed the authoritative CPFS output.
        encoded_job_spec = json.dumps(job_spec, separators=(",", ":"), ensure_ascii=False)
        if len(encoded_job_spec.encode()) > 48_000:
            raise RuntimeError("Resolved job spec exceeds the DLC environment payload safety limit")
        plugin = {
            "act": "quictrain_runtime_lerobot.adapters:ActRuntime",
            "pi05": "quictrain_runtime_lerobot.adapters:Pi05Runtime",
        }[model.id]
        spec = LaunchSpec(
            job_id=job.id,
            attempt_id=attempt.id,
            idempotency_key=attempt.idempotency_key,
            image_uri=model.image_uri,
            image_digest=model.image_digest,
            command=(
                "sh",
                "-lc",
                runtime_bootstrap_command(
                    rebind_pi05_oss_pretrained=bool(use_ecs_oss_path and model.id == "pi05")
                ),
            ),
            environment={
                "QUICTRAIN_JOB_ID": job.id,
                "QUICTRAIN_ATTEMPT_ID": attempt.id,
                "QUICTRAIN_JOB_SPEC_JSON": encoded_job_spec,
                "QUICTRAIN_RUNTIME_PLUGIN": plugin,
                "QUICTRAIN_EXECUTE_TRAINING": "1",
                "MLFLOW_RUN_ID": attempt.mlflow_run_id or "",
                "HF_HUB_DISABLE_TELEMETRY": "1",
                **(
                    {
                        "QUICTRAIN_OUTPUT_DIR": output_dir,
                        "QUICTRAIN_CONTROL_OUTPUT_DIR": control_output_dir,
                    }
                    if control_output_dir
                    else {}
                ),
                **(
                    {
                        "QUICTRAIN_WORK_ROOT": f"{cpfs_workspace}/work",
                        "HF_HOME": f"{cpfs_workspace}/cache/huggingface",
                        "TORCH_HOME": f"{cpfs_workspace}/cache/torch",
                        "TMPDIR": f"{cpfs_workspace}/tmp/{job.id}/{attempt.id}",  # nosec B108 - isolated job workspace
                    }
                    if cpfs_workspace
                    else {
                        "QUICTRAIN_WORK_ROOT": local_work_root,
                        "HF_HOME": f"{local_work_root}/cache/huggingface",
                        "TORCH_HOME": f"{local_work_root}/cache/torch",
                        "TMPDIR": f"{local_work_root}/tmp/{job.id}/{attempt.id}",  # nosec B108 - isolated job workspace
                    }
                ),
                "QUICTRAIN_GPU_COUNT": str(scaled["gpu_count"]),
                **(
                    {
                        "CUDA_VISIBLE_DEVICES": ",".join(
                            str(index) for index in range(int(scaled["gpu_count"]))
                        ),
                    }
                    if int(scaled["gpu_count"]) > 1
                    else {}
                ),
                **(
                    {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
                    if dataset.uri.startswith(("oss://", "bmcpfs://", "cpfs://"))
                    else {}
                ),
            },
            mounts=tuple(mounts),
            resource={
                "profile": job.resource_profile_id,
                "provider_id": provider_id,
                "pool_id": pool_id,
                "resource_id": pool_resource_id,
                "gpu_count": scaled["gpu_count"],
                "gpu_type": gpu_model,
                "cpu": scaled["cpu"],
                "memory": scaled["memory"],
                **(
                    {"shared_memory": scaled["shared_memory"]}
                    if scaled.get("shared_memory")
                    else {}
                ),
                "worker_count": scaled["worker_count"],
                **(
                    {"user_vpc": self.dlc_user_vpc}
                    if use_ecs_oss_path and self.dlc_user_vpc
                    else {}
                ),
            },
            labels={"quictrain-model-id": job.model_id, "quictrain-recipe-id": job.recipe_id},
        )
        try:
            submitted = provider.submit(spec)
        except TimeoutError:
            attempt.state = "ORPHANED"
            transition_job(session, job, JobState.ORPHANED, "job.warning", {"retryable": True})
            return
        except Exception as exc:
            message = str(exc)
            permanent = any(
                token in message
                for token in (
                    "not bound with workspace",
                    "InvalidResourceId",
                    "ResourceId is invalid",
                    "Resource not found",
                    "paidataset:GetDataset",
                    "not authorized to use this DataSource",
                    "ResourceAllocateFailed",
                    "create dataset with vpc mountpoint",
                    "requires an OSS mirror",
                    "OSS-mirrored pretrained",
                    "QUICTRAIN_OSS_CPFS_MIRROR_PREFIX",
                )
            )
            if not permanent:
                raise
            attempt.state = "FAILED"
            attempt.finished_at = utcnow()
            job.failure_category = "PROVIDER_REJECTED"
            job.failure_message = message[:800]
            append_event(
                session,
                job,
                "job.failed",
                {
                    "attempt_id": attempt.id,
                    "reason": "PROVIDER_REJECTED",
                    "message": message[:500],
                },
            )
            transition_job(
                session,
                job,
                JobState.FAILED,
                "job.failed",
                {"reason": "PROVIDER_REJECTED", "retryable": False},
            )
            return
        attempt.external_job_id = submitted.external_id
        attempt.provider_state = submitted.raw_status
        attempt.provider_payload = {
            **(attempt.provider_payload or {}),
            "pool_id": pool_id,
            "provider_id": provider_id,
            "resource_id": pool_resource_id,
            "gpu_count": scaled["gpu_count"],
            "artifact_prefix": output_uri,
            "artifact_manifest_uri": (
                f"{control_prefix or output_uri}/artifact_manifest.json" if output_uri else None
            ),
            "artifact_control_prefix": control_prefix,
            "reason_code": submitted.reason_code,
            "reason_message": submitted.message,
        }
        attempt.state = "PROVISIONING"
        append_event(
            session,
            job,
            "job.provider_submitted",
            {"attempt_id": attempt.id, "external_job_id": submitted.external_id},
        )
        transition_job(session, job, JobState.PROVISIONING)
        job.stage = "PROVISION"

    def _ingest_logs(self, session: Session, job: JobRecord, attempt: AttemptRecord) -> None:
        if not attempt.external_job_id:
            return
        lines, cursor = self._provider_for_attempt(session, job, attempt).get_logs(
            attempt.external_job_id, attempt.log_cursor
        )
        next_sequence = int(
            session.scalar(
                select(func.coalesce(func.max(JobLogRecord.sequence), 0)).where(
                    JobLogRecord.attempt_id == attempt.id
                )
            )
            or 0
        )
        for line in lines:
            exists = session.scalar(
                select(JobLogRecord.id).where(
                    JobLogRecord.attempt_id == attempt.id,
                    JobLogRecord.source == line.source,
                    JobLogRecord.provider_sequence == line.sequence,
                )
            )
            if exists:
                continue
            next_sequence += 1
            session.add(
                JobLogRecord(
                    id=new_id("log"),
                    job_id=job.id,
                    attempt_id=attempt.id,
                    sequence=next_sequence,
                    provider_sequence=line.sequence,
                    timestamp=line.timestamp,
                    level=line.level,
                    source=line.source,
                    message=line.message,
                )
            )
            self._classify_failure_log(job, line.message)
            if line.message.startswith("QUICTRAIN_EVENT "):
                try:
                    event = json.loads(line.message.removeprefix("QUICTRAIN_EVENT "))
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "metric":
                    name = str(event["name"])
                    value = float(event["value"])
                    step = int(event.get("step", 0))
                    job.latest_metrics = {**(job.latest_metrics or {}), name: value}
                    self._sync_metrics(attempt, {name: value}, step)
                    append_event(
                        session,
                        job,
                        "job.metric",
                        {
                            "name": name,
                            "value": value,
                            "step": step,
                            "at": event.get("at"),
                        },
                    )
                elif event.get("type") == "stage":
                    job.stage = str(event.get("stage") or job.stage)
                continue
            parsed_metrics = parse_lerobot_metrics(line.message)
            if parsed_metrics:
                job.latest_metrics = {**(job.latest_metrics or {}), **parsed_metrics}
                self._sync_metrics(attempt, parsed_metrics, 0)
        attempt.log_cursor = cursor

    def _sync_metrics(self, attempt: AttemptRecord, metrics: dict[str, float], step: int) -> None:
        if self.tracking_client is None or not attempt.mlflow_run_id:
            return
        try:
            self.tracking_client.log_metrics(attempt.mlflow_run_id, metrics, step)
        except Exception as exc:
            LOGGER.warning("MLflow metric sync failed for %s: %s", attempt.id, exc)

    def _finish_tracking(self, attempt: AttemptRecord, status: str) -> None:
        if self.tracking_client is None or not attempt.mlflow_run_id:
            return
        try:
            self.tracking_client.finish(attempt.mlflow_run_id, status)
        except Exception as exc:
            LOGGER.warning("MLflow termination failed for %s: %s", attempt.id, exc)

    @staticmethod
    def _classify_failure_log(job: JobRecord, message: str) -> None:
        lowered = message.lower()
        if "policy_preprocessor.json" in lowered and "pi05_base" in lowered:
            job.failure_category = "MODEL_ASSET_MISSING"
            job.failure_message = (
                "π0.5 预训练资源不完整：缺少 policy_preprocessor.json。"
                "该模型已暂停提交，待离线模型资产完成验收后恢复。"
            )
        elif "tokenizer_processor" in lowered and (
            "couldn't connect" in lowered or "cached files" in lowered or "paligemma" in lowered
        ):
            job.failure_category = "MODEL_ASSET_MISSING"
            job.failure_message = (
                "π0.5 缺少官方 PaliGemma tokenizer 离线资产。"
                "请先接受 Google Gemma 许可并将 tokenizer 合规镜像到 CPFS；"
                "该模型在资产验收通过前保持不可提交。"
            )
        elif "cuda out of memory" in lowered or "torch.outofmemoryerror" in lowered:
            job.failure_category = "RESOURCE_OOM"
            job.failure_message = (
                "训练进程耗尽 GPU 显存。请降低 batch size、启用梯度检查点，"
                "或改用显存更大的资源配置。"
            )
        elif "exited with status -9" in lowered or "signal 9" in lowered or "sigkill" in lowered:
            job.failure_category = "RESOURCE_OOM"
            job.failure_message = (
                "训练进程被系统强制终止（SIGKILL，常见于主机内存 cgroup OOM）。"
                "π0.5 请使用更高 Memory 的 DLC 配置，或降低并行加载。"
            )
        elif "tokenizer_name" in lowered and (
            "/mnt/cpfs/" in lowered or "repo id must be in the form" in lowered
        ):
            job.failure_category = "MODEL_ASSET_PATH"
            job.failure_message = (
                "π0.5 preprocessor 仍引用不可用的 tokenizer 路径（常见于 CPFS 绝对路径"
                "被带到 Quota4090 OSS 镜像）。请确认 OSS 镜像已重写 tokenizer_name，"
                "或使用 Scheduler 的 π0.5 OSS pretrained rebind。"
            )

    def _apply_provider_state(
        self,
        session: Session,
        job: JobRecord,
        attempt: AttemptRecord,
        provider_job: ProviderJob,
    ) -> None:
        provider_state = provider_job.state
        if provider_state == ProviderState.RUNNING and job.state == JobState.PROVISIONING.value:
            attempt.state = "RUNNING"
            attempt.started_at = utcnow()
            job.stage = "TRAIN"
            transition_job(session, job, JobState.RUNNING)
        elif provider_state == ProviderState.SUCCEEDED and job.state in {
            JobState.PROVISIONING.value,
            JobState.RUNNING.value,
        }:
            if job.state == JobState.PROVISIONING.value:
                transition_job(session, job, JobState.RUNNING)
            job.stage = "FINALIZE"
            provider = self._provider_for_attempt(session, job, attempt)
            if provider.__class__.__name__ == "FakeProvider":
                self._ensure_artifacts(session, job, attempt)
            else:
                if not self._ingest_artifact_manifest(session, job, attempt):
                    job.stage = "ARTIFACT"
                    self._append_once(
                        session,
                        job,
                        "job.artifact_manifest_pending",
                        {"uri": attempt.provider_payload.get("artifact_manifest_uri")},
                    )
                    return
            attempt.state = "SUCCEEDED"
            attempt.finished_at = utcnow()
            self._finish_tracking(attempt, "FINISHED")
            transition_job(session, job, JobState.SUCCEEDED)
        elif provider_state == ProviderState.FAILED:
            attempt.state = "FAILED"
            attempt.finished_at = utcnow()
            self._finish_tracking(attempt, "FAILED")
            job.failure_category = job.failure_category or provider_job.reason_code or "PROVIDER"
            job.failure_message = (
                job.failure_message
                or provider_job.message
                or "计算任务失败，请查看 Provider 日志。"
            )
            transition_job(
                session,
                job,
                JobState.FAILED,
                "job.failed",
                {
                    "reason_code": job.failure_category,
                    "reason_message": job.failure_message,
                    "external_job_id": attempt.external_job_id,
                },
            )
        elif provider_state == ProviderState.UNKNOWN and job.state != JobState.ORPHANED.value:
            attempt.state = "ORPHANED"
            transition_job(session, job, JobState.ORPHANED, "job.warning", {"retryable": True})

    @staticmethod
    def _ensure_artifacts(session: Session, job: JobRecord, attempt: AttemptRecord) -> None:
        if job.artifacts:
            return
        for name, _legacy_kind, size in [
            ("final_model.safetensors", "checkpoint", 4_829_174_784),
            ("resolved_config.json", "config", 8_420),
            ("summary.json", "summary", 18_640),
            ("artifact_manifest.json", "manifest", 4_216),
        ]:
            digest = hashlib.sha256(f"{job.id}:{name}".encode()).hexdigest()
            kind = infer_artifact_kind(name)
            session.add(
                ArtifactRecord(
                    id=new_id("art"),
                    job_id=job.id,
                    attempt_id=attempt.id,
                    name=name,
                    kind=kind,
                    uri=f"oss://quictrain-artifacts/{job.id}/{attempt.id}/{name}",
                    size_bytes=size,
                    sha256=digest,
                )
            )
            append_event(session, job, "job.artifact_available", {"name": name, "kind": kind})

    def _oss_mirror_uri(self, cpfs_or_path_uri: str | None) -> str | None:
        """Map a CPFS URI (or workspace-relative path) to the OSS mirror prefix.

        ``bmcpfs://fs.host/quictrain/datasets/...`` →
        ``{oss_cpfs_mirror_prefix}/datasets/...`` when the prefix already ends
        with the workspace root segment (``…/quictrain``).
        """

        if not cpfs_or_path_uri or not self.oss_cpfs_mirror_prefix:
            return None
        uri = cpfs_or_path_uri.strip()
        relative: str | None = None
        if self.cpfs_root_uri and uri.startswith(f"{self.cpfs_root_uri}/"):
            relative = uri.removeprefix(f"{self.cpfs_root_uri}/").lstrip("/")
        elif uri.startswith(("bmcpfs://", "cpfs://")):
            # bmcpfs://host/path… → path…
            without_scheme = uri.split("://", 1)[-1]
            parts = without_scheme.split("/", 1)
            relative = parts[1] if len(parts) == 2 else None
        elif uri.startswith("/"):
            # Absolute mount path /mnt/cpfs/quictrain/... → quictrain/...
            prefix = f"{self.cpfs_mount_path}/"
            if uri.startswith(prefix):
                relative = uri.removeprefix(prefix).lstrip("/")
        else:
            relative = uri.lstrip("/")
        if not relative:
            return None
        # If mirror prefix already includes workspace dir, strip duplicate.
        workspace = self.cpfs_workspace_dir
        if relative.startswith(f"{workspace}/") and self.oss_cpfs_mirror_prefix.endswith(
            f"/{workspace}"
        ):
            relative = relative.removeprefix(f"{workspace}/")
        return f"{self.oss_cpfs_mirror_prefix}/{relative}"

    def _cpfs_mount_for_pool(self, pool_id: str | None) -> dict[str, str]:
        """Build the CPFS mount for the selected compute pool.

        Lingjun H20 quotas accept direct bmcpfs:// mounts. ECS/general quotas
        such as Quota4090 require a VPC-aware BMCPFS dataset (preferred) or an
        explicit VPC mountTarget option.
        """

        if not self.cpfs_root_uri:
            raise RuntimeError("CPFS root URI is not configured")
        entry: dict[str, str] = {
            "target": self.cpfs_mount_path,
            "read_only": "false",
        }
        needs_vpc = bool(pool_id and "4090" in pool_id)
        if needs_vpc:
            # ECS/general quotas require a VPC BMCPFS dataset for allocation.
            # Uri+mountTarget alone can pass CreateJob but then fail with
            # ResourceAllocateFailed ("create dataset with vpc mountpoint").
            # DataSourceId needs paidataset:GetDataset on the caller identity
            # (ECS role or explicit ALIBABA_CLOUD_ACCESS_KEY_*).
            if self.cpfs_vpc_data_source_id:
                entry["data_source_id"] = self.cpfs_vpc_data_source_id
                return entry
            entry["source_uri"] = f"{self.cpfs_root_uri}/"
            if not self.cpfs_vpc_mount_target:
                raise RuntimeError(
                    "Quota4090 requires QUICTRAIN_CPFS_VPC_DATA_SOURCE_ID "
                    "or QUICTRAIN_CPFS_VPC_MOUNT_TARGET."
                )
            entry["options"] = json.dumps(
                {
                    "mountTarget": self.cpfs_vpc_mount_target,
                    "isVpcMount": True,
                },
                separators=(",", ":"),
            )
            return entry
        entry["source_uri"] = f"{self.cpfs_root_uri}/"
        return entry

    def _artifact_uri(self, job_id: str, attempt_id: str) -> str | None:
        if self.cpfs_root_uri and self.cpfs_data_source_id:
            return f"{self.cpfs_root_uri}/{self.cpfs_workspace_dir}/runs/{job_id}/{attempt_id}"
        if not self.artifact_root.startswith("oss://"):
            return None
        return f"{self.artifact_root}/{job_id}/{attempt_id}"

    def _cpfs_workspace_path(self) -> str | None:
        if not self.cpfs_root_uri or not self.cpfs_data_source_id:
            return None
        return f"{self.cpfs_mount_path}/{self.cpfs_workspace_dir}"

    def _ingest_artifact_manifest(
        self, session: Session, job: JobRecord, attempt: AttemptRecord
    ) -> bool:
        manifest_uri = attempt.provider_payload.get("artifact_manifest_uri")
        prefix = attempt.provider_payload.get("artifact_prefix")
        control_prefix = attempt.provider_payload.get("artifact_control_prefix") or prefix
        if not manifest_uri or not prefix or not control_prefix or self.artifact_client is None:
            return False
        if not self.artifact_client.exists(f"{control_prefix}/_SUCCESS"):
            return False
        try:
            manifest = self.artifact_client.read_json(manifest_uri)
        except FileNotFoundError:
            return False
        if manifest.get("protocol_version") != "v1alpha1":
            raise RuntimeError(f"Unsupported artifact manifest at {manifest_uri}")
        items = manifest.get("artifacts")
        if not isinstance(items, list) or not items:
            raise RuntimeError(f"Empty artifact manifest at {manifest_uri}")
        for item in items:
            uri = str(item.get("uri") or "")
            digest = str(item.get("sha256") or "")
            if not uri.startswith(f"{prefix}/") or len(digest) != 64:
                raise RuntimeError(f"Invalid artifact entry at {manifest_uri}")
            name = str(item["name"])
            session.add(
                ArtifactRecord(
                    id=new_id("art"),
                    job_id=job.id,
                    attempt_id=attempt.id,
                    name=name,
                    kind=infer_artifact_kind(name),
                    uri=uri,
                    size_bytes=int(item["size_bytes"]),
                    sha256=digest,
                )
            )
            append_event(
                session,
                job,
                "job.artifact_available",
                {"name": name, "kind": infer_artifact_kind(name)},
            )
        append_event(session, job, "job.artifact_manifest_verified", {"uri": manifest_uri})
        return True

    @staticmethod
    def _append_once(
        session: Session,
        job: JobRecord,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        if job.events and job.events[-1].event_type == event_type:
            return
        append_event(session, job, event_type, payload)

    @staticmethod
    def _provider_summary(provider_job: ProviderJob) -> str:
        details = ": ".join(
            item for item in (provider_job.reason_code, provider_job.message) if item
        )
        return f"{provider_job.raw_status}{f' · {details}' if details else ''}"
