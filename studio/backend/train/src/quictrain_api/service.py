from __future__ import annotations

import hashlib
import re
from typing import Any

from quictrain_config import canonical_hash, resolve_config
from quictrain_core import JobState, new_id
from quictrain_core.domain import require_transition
from quictrain_core.materialization import SCHEDULABLE_READINESS, is_dataset_archive_uri
from quictrain_model_specs import MODEL_REGISTRY, DatasetVersion, compatibility_issues, get_model
from quictrain_model_specs.registry import ResourceProfile, missing_dataset_metadata
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import (
    ArtifactRecord,
    AttemptRecord,
    AuditEventRecord,
    DatasetVersionRecord,
    JobRecord,
    ModelVersionRecord,
    ResourceProfileRecord,
    UserRecord,
    append_event,
    utcnow,
)
from .errors import ServiceError
from .ops import enforce_submission_limits
from .schemas import DatasetRegisterRequest, JobCreateRequest, JobValidationRequest
from .settings import get_settings

ALLOWED_DATASET_URI_PREFIXES = ("oss://", "bmcpfs://", "cpfs://", "hf://", "file://")

SEED_DATASETS = [
    DatasetVersion(
        id="dsv_pusht_smoke_v1",
        dataset_id="quictrain_pusht_smoke",
        name="PushT 平台验收样本",
        version="v1",
        uri=(
            "oss://oss-pai-1183v1b6du4vkucj3h-cn-beijing/"
            "quictrain/datasets/lerobot/quictrain-pusht-smoke-v1"
        ),
        checksum="generated:quictrain-pusht-smoke-v1",
        repo_id="quicrobot/quictrain-pusht-smoke-v1",
        episodes=4,
        frames=128,
        duration_hours=0.004,
        fps=10,
        robot_type="pusht",
        camera_keys=["observation.image"],
        action_dim=2,
        state_dim=2,
        language_tasks=False,
    ),
    DatasetVersion(
        id="dsv_kitchen_v17",
        dataset_id="qrdf_kitchen",
        name="双臂厨房整理",
        version="v17",
        uri="oss://quicdata/lerobot/kitchen/v17",
        checksum="sha256:5a9a9a0f7f65cf0cf1eec7b5aef17e9f",
        episodes=128,
        frames=486_320,
        duration_hours=4.5,
        fps=30,
        robot_type="dual_arm_x",
        camera_keys=["observation.images.left", "observation.images.right"],
        action_dim=14,
        state_dim=14,
    ),
    DatasetVersion(
        id="dsv_fold_v8",
        dataset_id="qrdf_folding",
        name="柔性衣物折叠",
        version="v8",
        uri="oss://quicdata/lerobot/folding/v8",
        checksum="sha256:344bda11d56d2749b8f419f64fd94826",
        episodes=72,
        frames=294_110,
        duration_hours=2.8,
        fps=30,
        robot_type="dual_arm_x",
        camera_keys=[
            "observation.images.left",
            "observation.images.right",
            "observation.images.overhead",
        ],
        action_dim=14,
        state_dim=14,
    ),
    DatasetVersion(
        id="dsv_assembly_v4",
        dataset_id="qrdf_assembly",
        name="精密零件装配",
        version="v4",
        uri="oss://quicdata/lerobot/assembly/v4",
        checksum="sha256:9ccf7ee8ba7d923fda865fe98cad3108",
        episodes=34,
        frames=102_600,
        duration_hours=0.9,
        fps=30,
        robot_type="single_arm_y",
        camera_keys=["observation.images.wrist"],
        action_dim=7,
        state_dim=7,
    ),
]


def seed_catalog(session: Session) -> None:
    """Sync supported model recipes; sample datasets require a development/test opt-in."""
    datasets = SEED_DATASETS if get_settings().example_dataset_seed_enabled else ()
    for dataset in datasets:
        if session.get(DatasetVersionRecord, dataset.id) is None:
            manifest = dataset.model_dump(mode="json")
            session.add(
                DatasetVersionRecord(
                    id=dataset.id,
                    project_id="prj_robot_arm",
                    external_dataset_id=dataset.dataset_id,
                    name=dataset.name,
                    version=dataset.version,
                    status=dataset.status,
                    format=dataset.format,
                    format_version=dataset.format_version,
                    uri=dataset.uri,
                    checksum=dataset.checksum,
                    manifest=manifest,
                )
            )

    active_model_versions = {model.version_id for model in MODEL_REGISTRY.values()}
    for record in session.scalars(select(ModelVersionRecord)).all():
        record.deprecated = record.id not in active_model_versions
    for profile in session.scalars(select(ResourceProfileRecord)).all():
        if profile.model_version_id not in active_model_versions:
            profile.enabled = False
            profile.selectable = False

    for model in MODEL_REGISTRY.values():
        record = session.get(ModelVersionRecord, model.version_id)
        if record is None:
            session.add(
                ModelVersionRecord(
                    id=model.version_id,
                    model_id=model.id,
                    display_name=model.name,
                    version=model.version,
                    backend=model.backend,
                    maturity=model.maturity,
                    manifest=model.model_dump(mode="json"),
                    schema_snapshot=model.schema,
                    schema_hash=model.schema_hash,
                )
            )
        else:
            record.deprecated = False
            record.display_name = model.name
            record.version = model.version
            record.backend = model.backend
            record.maturity = model.maturity
            record.manifest = model.model_dump(mode="json")
            record.schema_snapshot = model.schema
            record.schema_hash = model.schema_hash
        for recipe in model.recipes:
            for profile in recipe.resource_profiles:
                record_profile = session.get(ResourceProfileRecord, profile.id)
                if record_profile is None:
                    session.add(
                        ResourceProfileRecord(
                            id=profile.id,
                            model_version_id=model.version_id,
                            recipe_id=recipe.id,
                            name=profile.name,
                            gpu_models=profile.gpu_models,
                            gpu_count=profile.gpu_count,
                            vram_gb_min=profile.vram_gb_min,
                            calibration_status=profile.calibration_status,
                            selectable=profile.selectable,
                            warning=profile.warning,
                            provider_id=profile.provider_id,
                            pool_id=profile.pool_id,
                        )
                    )
                else:
                    record_profile.enabled = True
                    record_profile.selectable = profile.selectable
                    record_profile.provider_id = profile.provider_id
                    record_profile.pool_id = profile.pool_id
                    record_profile.gpu_models = profile.gpu_models
                    record_profile.gpu_count = profile.gpu_count
                    record_profile.vram_gb_min = profile.vram_gb_min
                    record_profile.calibration_status = profile.calibration_status
                    record_profile.warning = profile.warning
    session.commit()


def dataset_from_record(record: DatasetVersionRecord) -> DatasetVersion:
    return DatasetVersion.model_validate(record.manifest)


def _slugify_dataset_id(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-._")
    return (slug or "dataset")[:120]


def register_dataset_version(
    session: Session,
    *,
    project_id: str,
    actor_id: str,
    request: DatasetRegisterRequest,
) -> tuple[DatasetVersionRecord, bool]:
    """Register an immutable dataset version for training mounts (OSS / CPFS / HF)."""
    uri = request.uri.strip()
    if not uri.startswith(ALLOWED_DATASET_URI_PREFIXES):
        raise ServiceError(
            "DATASET_URI_UNSUPPORTED",
            "仅支持 oss://、bmcpfs://、cpfs://、hf:// 或 file:// 数据集 URI。",
            field="uri",
            status_code=400,
            details={"allowed_prefixes": list(ALLOWED_DATASET_URI_PREFIXES)},
        )
    external_dataset_id = (request.dataset_id or _slugify_dataset_id(request.display_name)).strip()
    external_version_id = request.version.strip()
    checksum = (request.checksum or "").strip() or (
        "operator:"
        + hashlib.sha256(f"{external_dataset_id}:{external_version_id}:{uri}".encode()).hexdigest()[
            :32
        ]
    )
    status = "REGISTERED" if request.request_materialization else request.status
    if status == "READY" and not uri.startswith(("oss://", "bmcpfs://", "cpfs://", "file://")):
        raise ServiceError(
            "DATASET_NOT_READY_SOURCE",
            "READY 状态需要可挂载的 oss:// / bmcpfs:// / cpfs:// / file:// URI；"
            "hf:// 请先登记为 REGISTERED 并物化。",
            field="uri",
            status_code=400,
        )
    existing = session.scalar(
        select(DatasetVersionRecord).where(
            DatasetVersionRecord.external_dataset_id == external_dataset_id,
            DatasetVersionRecord.version == external_version_id,
        )
    )
    if existing is not None:
        if existing.project_id != project_id:
            raise ServiceError(
                "DATASET_VERSION_PROJECT_CONFLICT",
                "数据集标识和版本已被其他项目使用，请选择当前项目的独立标识。",
                status_code=409,
            )
        if existing.checksum != checksum:
            raise ServiceError(
                "IMMUTABLE_VERSION_CONFLICT", "不可变数据版本 checksum 冲突。", status_code=409
            )
        return existing, False
    if status == "READY" and is_dataset_archive_uri(uri):
        raise ServiceError(
            "DATASET_ARCHIVE_NOT_READY",
            "归档文件尚未物化为已验证的数据集目录，只能登记为 REGISTERED。",
            field="status",
            status_code=409,
        )
    missing = missing_dataset_metadata(request)
    if status == "READY" and missing:
        raise ServiceError(
            "DATASET_METADATA_REQUIRED",
            "READY 数据版本需要完整的实际训练元数据。",
            field=missing[0],
            details={"missing": missing},
        )
    duration_hours = request.duration_hours
    if duration_hours is None and request.frames is not None and request.fps is not None:
        duration_hours = request.frames / request.fps / 3600
    try:
        validated = DatasetVersion.model_validate(
            {
                "id": new_id("dsv"),
                "dataset_id": external_dataset_id,
                "name": request.display_name.strip(),
                "version": external_version_id,
                "status": status,
                "format": request.format,
                "format_version": request.format_version,
                "uri": uri,
                "checksum": checksum,
                "episodes": request.episodes,
                "frames": request.frames,
                "duration_hours": duration_hours,
                "fps": request.fps,
                "robot_type": request.robot_type.strip() if request.robot_type else None,
                "camera_keys": request.camera_keys,
                "action_dim": request.action_dim,
                "state_dim": request.state_dim,
                "language_tasks": request.language_tasks,
            }
        )
    except Exception as exc:
        raise ServiceError(
            "DATASET_VERSION_INVALID",
            "数据版本字段校验失败。",
            status_code=400,
            details={"error": str(exc)},
        ) from exc
    record = DatasetVersionRecord(
        id=validated.id,
        project_id=project_id,
        external_dataset_id=validated.dataset_id,
        name=validated.name,
        version=validated.version,
        status=validated.status,
        format=validated.format,
        format_version=validated.format_version,
        uri=validated.uri,
        checksum=validated.checksum,
        manifest=validated.model_dump(mode="json"),
    )
    session.add(record)
    session.add(
        AuditEventRecord(
            id=new_id("aud"),
            actor_id=actor_id,
            action="dataset.register",
            resource_type="dataset_version",
            resource_id=record.id,
            details={
                "client_request_id": request.client_request_id,
                "uri": uri,
                "status": status,
                "request_materialization": request.request_materialization,
            },
        )
    )
    session.flush()
    return record, True


def is_local_sim_profile(profile: ResourceProfile) -> bool:
    return (
        profile.provider_id == "fake"
        or "LOCAL" in profile.gpu_models
        or profile.id.endswith("-local-sim")
    )


def selectable_resource_profiles(
    model_id: str, recipe_profiles: list[ResourceProfile]
) -> list[ResourceProfile]:
    """Default pool (QUICTRAIN_PROVIDER) filters LOCAL; fake prefers *-local-sim first."""
    settings = get_settings()
    selectable = [profile for profile in recipe_profiles if profile.selectable]
    if settings.provider == "fake":
        local = [profile for profile in selectable if is_local_sim_profile(profile)]
        cloud = [profile for profile in selectable if not is_local_sim_profile(profile)]
        return local + cloud
    return [profile for profile in selectable if not is_local_sim_profile(profile)]


def local_runtime_injection() -> dict[str, Any] | None:
    settings = get_settings()
    if settings.provider != "fake":
        return None
    root = settings.artifact_root.rstrip("/")
    return {"runtime.output_uri": f"{root}/system-assigned"}


def validate_job(
    session: Session, request: JobValidationRequest, role: str = "admin"
) -> dict[str, Any]:
    dataset_record = session.get(DatasetVersionRecord, request.dataset_version_id)
    if dataset_record is None:
        raise ServiceError(
            "DATASET_NOT_FOUND", "数据版本不存在。", field="dataset_version_id", status_code=404
        )
    try:
        model = get_model(request.model_version_id)
    except KeyError as exc:
        raise ServiceError(
            "MODEL_NOT_FOUND", "模型版本不存在。", field="model_version_id", status_code=404
        ) from exc

    if not model.selectable:
        raise ServiceError(
            "MODEL_NOT_READY",
            model.availability_message or "模型尚未通过生产验收。",
            field="model_version_id",
            status_code=409,
            details={"model_id": model.id, "model_version_id": model.version_id},
        )

    recipe = next((item for item in model.recipes if item.id == request.recipe_id), None)
    if recipe is None:
        raise ServiceError(
            "RECIPE_NOT_FOUND", "Recipe 不存在。", field="recipe_id", status_code=404
        )

    issues = compatibility_issues(dataset_from_record(dataset_record), model)
    if dataset_record.status not in SCHEDULABLE_READINESS:
        issues.append(
            {
                "severity": "BLOCKER",
                "code": "DATASET_NOT_READY",
                "field": "status",
                "message": f"数据版本状态为 {dataset_record.status}，需 READY 后方可调度。",
                "remediation": "触发物化并等待 READY，或修复 FAILED 后重试。",
            }
        )
    try:
        resolved, diff = resolve_config(
            model.id,
            request.config_overrides,
            role=role,
            runtime_injection=local_runtime_injection(),
        )
    except KeyError as exc:
        raise ServiceError(
            "CONFIG_PATH_UNKNOWN", f"未知配置字段：{exc.args[0]}", field=str(exc.args[0])
        ) from exc
    except PermissionError as exc:
        raise ServiceError(
            "CONFIG_OVERRIDE_FORBIDDEN",
            f"没有权限修改字段：{exc.args[0]}",
            field=str(exc.args[0]),
            status_code=403,
        ) from exc
    except ValueError as exc:
        raise ServiceError("CONFIG_INVALID", str(exc), field="config_overrides") from exc

    profiles = selectable_resource_profiles(model.id, recipe.resource_profiles)
    selected = None
    if request.resource_selection.profile:
        selected = next(
            (profile for profile in profiles if profile.id == request.resource_selection.profile),
            None,
        )
        if selected is None:
            raise ServiceError(
                "RESOURCE_PROFILE_INVALID",
                "所选资源配置不可用于该模型。",
                field="resource_selection.profile",
            )
    else:
        selected = profiles[0]

    return {
        "valid": not any(issue["severity"] == "BLOCKER" for issue in issues),
        "issues": issues,
        "schema_hash": model.schema_hash,
        "resolved_config_preview": _redact(resolved),
        "config_diff": diff,
        "config_hash": canonical_hash(resolved),
        "resource_recommendation": {
            "profile": selected.id,
            "name": selected.name,
            "flavor_candidates": selected.gpu_models,
            "gpu_count": selected.gpu_count,
            "vram_gb_min": selected.vram_gb_min,
            "calibration_status": selected.calibration_status,
            "warnings": [selected.warning] if selected.warning else [],
        },
        "provenance": {
            "dataset_checksum": dataset_record.checksum,
            "model_version": model.version,
            "upstream_ref": model.upstream_ref,
            "image_digest": model.image_digest,
            "adapter_version": model.adapter_version,
        },
    }


def _normalize_creator_id(creator_id: Any) -> int | None:
    if creator_id is None:
        return None
    try:
        return int(creator_id)
    except (ValueError, TypeError):
        return 1


def create_job(session: Session, request: JobCreateRequest, creator_id: int | str = 1) -> JobRecord:
    int_creator_id = _normalize_creator_id(creator_id)
    existing = session.scalar(
        select(JobRecord).where(
            JobRecord.project_id == request.project_id,
            JobRecord.creator_id == int_creator_id,
            JobRecord.client_request_id == request.client_request_id,
        )
    )
    if existing:
        return existing

    validation = validate_job(session, request)
    if not validation["valid"]:
        raise ServiceError(
            "JOB_VALIDATION_BLOCKED",
            "数据与模型存在兼容性阻断项。",
            details={"issues": validation["issues"]},
        )

    model = get_model(request.model_version_id)
    dataset = session.get(DatasetVersionRecord, request.dataset_version_id)
    assert dataset is not None
    if dataset.status not in SCHEDULABLE_READINESS:
        raise ServiceError(
            "DATASET_NOT_READY",
            "数据版本尚未物化为 READY。",
            field="dataset_version_id",
            status_code=409,
            details={"status": dataset.status},
        )
    resolved, _ = resolve_config(
        model.id,
        request.config_overrides,
        role="admin",
        runtime_injection=local_runtime_injection(),
    )
    profile_id = validation["resource_recommendation"]["profile"]
    enforce_submission_limits(
        session, project_id=request.project_id, resource_profile_id=profile_id
    )
    display_name = (
        request.display_name or f"{model.id}-{dataset.external_dataset_id}-{dataset.version}"
    )
    job = JobRecord(
        id=new_id("job"),
        project_id=request.project_id,
        creator_id=int_creator_id,
        client_request_id=request.client_request_id,
        display_name=display_name,
        state=JobState.VALIDATING.value,
        stage="VALIDATE",
        dataset_version_id=request.dataset_version_id,
        model_version_id=model.version_id,
        model_id=model.id,
        recipe_id=request.recipe_id,
        resource_profile_id=profile_id,
        schema_hash=model.schema_hash,
        config_hash=canonical_hash(resolved),
        schema_snapshot=model.schema,
        user_overrides=request.config_overrides,
        resolved_config=resolved,
        source_snapshot={
            "dataset_checksum": dataset.checksum,
            "dataset_materialized_uri": dataset.materialized_uri,
            "upstream_repo": model.upstream_repo,
            "upstream_ref": model.upstream_ref,
            "adapter_version": model.adapter_version,
            "image_uri": model.image_uri,
            "image_digest": model.image_digest,
        },
    )
    session.add(job)
    session.flush()
    append_event(session, job, "job.created", {"state": job.state})
    transition_job(session, job, JobState.QUEUED, "job.queued", {"reason": "capacity_available"})
    session.add(
        AuditEventRecord(
            id=new_id("aud"),
            actor_id=str(int_creator_id),
            action="job.create",
            resource_type="job",
            resource_id=job.id,
            details={"client_request_id": request.client_request_id},
        )
    )
    session.commit()
    session.refresh(job)
    return job


def transition_job(
    session: Session,
    job: JobRecord,
    state: JobState,
    event_type: str = "job.state_changed",
    payload: dict[str, Any] | None = None,
) -> None:
    old = JobState(job.state)
    require_transition(old, state)
    job.state = state.value
    if state == JobState.RUNNING and job.started_at is None:
        job.started_at = utcnow()
    if state in {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}:
        job.finished_at = utcnow()
    append_event(
        session,
        job,
        event_type,
        {"old_state": old.value, "new_state": state.value, **(payload or {})},
    )


def _resolve_creator_display_name(session: Session, creator_id: Any) -> str:
    if creator_id is None:
        return "Admin"
    try:
        int_id = int(creator_id)
    except (TypeError, ValueError):
        return "Admin"

    if get_settings().auth_mode == "studio":
        # The integrated service shares QuicStudio's integer-keyed users table.
        # UserRecord targets the standalone QuicTrain schema instead.
        from data.database import User as StudioUser

        user = session.get(StudioUser, int_id)
    else:
        user = session.get(UserRecord, str(int_id))
    return user.display_name if user is not None else "Admin"


def serialize_job(job: JobRecord, session: Session | None = None) -> dict[str, Any]:
    current = job.attempts[-1] if job.attempts else None
    public_mlflow_url = get_settings().public_mlflow_url.rstrip("/")
    creator_name = "Admin"
    provider_id: str | None = None
    pool_id: str | None = None
    dashboard: str | None = None
    if session is not None and job.creator_id is not None:
        creator_name = _resolve_creator_display_name(session, job.creator_id)
        profile = session.get(ResourceProfileRecord, job.resource_profile_id)
        if profile is not None:
            provider_id = profile.provider_id
            pool_id = profile.pool_id
    if current and current.external_job_id:
        try:
            from .runtime import get_provider

            pid = current.provider or provider_id
            dashboard = get_provider(pid).get_dashboard_url(current.external_job_id)
        except Exception:
            dashboard = None
    return {
        "id": job.id,
        "display_name": job.display_name,
        "project_id": job.project_id,
        "creator": {"id": job.creator_id, "display_name": creator_name},
        "state": job.state,
        "stage": job.stage,
        "queue_reason": job.queue_reason,
        "priority": job.priority,
        "dataset_version_id": job.dataset_version_id,
        "model": {
            "version_id": job.model_version_id,
            "model_id": job.model_id,
            "recipe_id": job.recipe_id,
            "backend": "lerobot",
        },
        "resource": {
            "profile": job.resource_profile_id,
            "selection_mode": "AUTO",
            "provider_id": provider_id,
            "pool_id": pool_id,
        },
        "current_attempt": serialize_attempt(current) if current else None,
        "attempts": [serialize_attempt(attempt) for attempt in job.attempts],
        "latest_metrics": job.latest_metrics or {},
        "schema_hash": job.schema_hash,
        "config_hash": job.config_hash,
        "resolved_config": job.resolved_config,
        "user_overrides": job.user_overrides,
        "source": job.source_snapshot,
        "failure": (
            {"category": job.failure_category, "message": job.failure_message}
            if job.failure_category
            else None
        ),
        "artifacts": [serialize_artifact(item) for item in job.artifacts],
        "events": [
            {
                "id": event.sequence,
                "type": event.event_type,
                "payload": event.payload,
                "created_at": event.created_at.isoformat(),
            }
            for event in job.events
        ],
        "links": {
            "mlflow": (
                f"{public_mlflow_url}/#/experiments/"
                f"{current.provider_payload.get('mlflow_experiment_id')}/runs/"
                f"{current.mlflow_run_id}"
            )
            if current
            and current.mlflow_run_id
            and current.provider_payload.get("mlflow_experiment_id")
            else None,
            "provider_dashboard": dashboard,
        },
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "parent_job_id": job.parent_job_id,
    }


def serialize_attempt(attempt: AttemptRecord | None) -> dict[str, Any] | None:
    if attempt is None:
        return None
    provider_payload = attempt.provider_payload or {}
    return {
        "id": attempt.id,
        "number": attempt.number,
        "state": attempt.state,
        "provider": attempt.provider,
        "external_job_id": attempt.external_job_id,
        "provider_state": attempt.provider_state,
        "provider_reason_code": provider_payload.get("reason_code"),
        "provider_reason_message": provider_payload.get("reason_message"),
        "artifact_prefix": provider_payload.get("artifact_prefix"),
        "mlflow_run_id": attempt.mlflow_run_id,
        "created_at": attempt.created_at.isoformat(),
        "started_at": attempt.started_at.isoformat() if attempt.started_at else None,
        "finished_at": attempt.finished_at.isoformat() if attempt.finished_at else None,
    }


def serialize_artifact(artifact: ArtifactRecord) -> dict[str, Any]:
    return {
        "id": artifact.id,
        "name": artifact.name,
        "kind": artifact.kind,
        "uri": artifact.uri,
        "export_uri": artifact.export_uri,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
        "retention_days": artifact.retention_days,
        "available": artifact.available,
        "exported": bool(artifact.export_uri),
        "created_at": artifact.created_at.isoformat(),
    }


def retry_job(session: Session, job: JobRecord, actor_id: str = "usr_demo") -> JobRecord:
    if job.state not in {JobState.FAILED.value, JobState.CANCELLED.value}:
        raise ServiceError("JOB_NOT_RETRYABLE", "只有失败或取消的任务可以重试。")
    job.state = JobState.QUEUED.value
    job.stage = "QUEUE"
    job.finished_at = None
    job.failure_category = None
    job.failure_message = None
    append_event(session, job, "job.queued", {"reason": "manual_retry"})
    session.add(
        AuditEventRecord(
            id=new_id("aud"),
            actor_id=actor_id,
            action="job.retry",
            resource_type="job",
            resource_id=job.id,
        )
    )
    session.commit()
    return job


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***"
            if any(token in key.lower() for token in ("token", "secret", "password"))
            else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value
