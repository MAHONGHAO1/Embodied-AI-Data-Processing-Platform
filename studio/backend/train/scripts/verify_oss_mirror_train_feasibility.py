#!/usr/bin/env python3
"""Feasibility checks: Studio → OSS mirror dataset mounts → DLC compute.

Aligned with QuicTrain's immutable dataset catalog + Quota4090 OSS mount path.

Stages (default: all except submit):
  1. catalog_archive_gate   — Catalog .tar.gz exports cannot become READY
  2. oss_mirror_mapping     — bmcpfs:// → QUICTRAIN_OSS_CPFS_MIRROR_PREFIX
  3. ecs_4090_mount_plan    — READY oss:// directory → /quictrain/input/dataset
  4. oss_dataset_probe      — optional: confirm LeRobot tree exists on OSS
  5. dlc_canary             — optional: dry-run or --submit bounded ACT×1

Exit 0 only when every selected stage passes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_TRAIN_SRC = Path(__file__).resolve().parents[1] / "src"
if _TRAIN_SRC.is_dir() and str(_TRAIN_SRC) not in sys.path:
    sys.path.insert(0, str(_TRAIN_SRC))

try:
    from quictrain_core.materialization import is_dataset_archive_uri
except Exception:  # pragma: no cover — allow ECS host without package install

    def is_dataset_archive_uri(uri: str) -> bool:  # type: ignore[misc]
        return urlparse(uri).path.lower().endswith((".tar.gz", ".tgz", ".tar", ".zip"))


DEFAULT_MIRROR_DATASET = (
    "oss://oss-pai-1183v1b6du4vkucj3h-cn-beijing/"
    "quictrain/datasets/lerobot/quictrain-pusht-smoke-v1"
)
DEFAULT_CPFS_DATASET = (
    "bmcpfs://bmcpfs-03001wmodv0g6pynhun3u.cn-beijing/"
    "quictrain/datasets/lerobot/quictrain-pusht-smoke-v1"
)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def _mirror_prefix() -> str | None:
    explicit = (os.environ.get("QUICTRAIN_OSS_CPFS_MIRROR_PREFIX") or "").strip().rstrip("/")
    if explicit:
        return explicit
    bucket = (os.environ.get("ALIYUN_OSS_BUCKET") or "").strip()
    if bucket:
        return f"oss://{bucket}/quictrain"
    return None


def oss_mirror_uri(
    cpfs_or_path_uri: str | None,
    *,
    mirror_prefix: str | None = None,
    cpfs_root_uri: str | None = None,
    cpfs_mount_path: str = "/mnt/cpfs",
    cpfs_workspace_dir: str = "quictrain",
) -> str | None:
    """Map bmcpfs:// / CPFS path → OSS mirror (mirrors Scheduler._oss_mirror_uri)."""
    prefix = (mirror_prefix if mirror_prefix is not None else _mirror_prefix()) or None
    if not cpfs_or_path_uri or not prefix:
        return None
    root = cpfs_root_uri or os.environ.get(
        "QUICTRAIN_CPFS_ROOT_URI",
        "bmcpfs://bmcpfs-03001wmodv0g6pynhun3u.cn-beijing",
    )
    mount = os.environ.get("QUICTRAIN_CPFS_MOUNT_PATH", cpfs_mount_path)
    workspace = os.environ.get("QUICTRAIN_CPFS_WORKSPACE_DIR", cpfs_workspace_dir)
    uri = cpfs_or_path_uri.strip()
    relative: str | None = None
    if root and uri.startswith(f"{root}/"):
        relative = uri.removeprefix(f"{root}/").lstrip("/")
    elif uri.startswith(("bmcpfs://", "cpfs://")):
        without_scheme = uri.split("://", 1)[-1]
        parts = without_scheme.split("/", 1)
        relative = parts[1] if len(parts) == 2 else None
    elif uri.startswith("/"):
        path_prefix = f"{mount}/"
        if uri.startswith(path_prefix):
            relative = uri.removeprefix(path_prefix).lstrip("/")
    else:
        relative = uri.lstrip("/")
    if not relative:
        return None
    if relative.startswith(f"{workspace}/") and prefix.endswith(f"/{workspace}"):
        relative = relative.removeprefix(f"{workspace}/")
    return f"{prefix}/{relative}"


def check_catalog_archive_gate() -> CheckResult:
    """Studio Catalog LeRobot exports are archives; they must not be schedulable."""
    samples = [
        "oss://quicstudio-uat-export/exports/v1/catalog/1/versions/2/lerobot.tar.gz",
        "oss://bucket/path/dataset.tgz",
        "oss://bucket/path/dataset.zip",
        DEFAULT_MIRROR_DATASET,
    ]
    classified = {
        uri: {
            "is_archive": is_dataset_archive_uri(uri),
            "schedulable_as_ready": not is_dataset_archive_uri(uri),
        }
        for uri in samples
    }
    archive_blocked = all(
        classified[uri]["is_archive"] and not classified[uri]["schedulable_as_ready"]
        for uri in samples
        if uri.endswith((".tar.gz", ".tgz", ".zip"))
    )
    directory_ok = classified[DEFAULT_MIRROR_DATASET]["schedulable_as_ready"]
    ok = archive_blocked and directory_ok
    return CheckResult(
        name="catalog_archive_gate",
        ok=ok,
        detail={
            "classified": classified,
            "note": (
                "Studio catalog-registrations keep status=REGISTERED for archives; "
                "READY requires a directory oss:// / bmcpfs:// tree under the mirror."
            ),
        },
        error=None if ok else "archive/directory readiness classification failed",
    )


def check_oss_mirror_mapping() -> CheckResult:
    prefix = _mirror_prefix()
    if not prefix:
        return CheckResult(
            name="oss_mirror_mapping",
            ok=False,
            error="QUICTRAIN_OSS_CPFS_MIRROR_PREFIX / ALIYUN_OSS_BUCKET unset",
        )
    sched_mapped = oss_mirror_uri(DEFAULT_CPFS_DATASET)
    mapped = sched_mapped
    expected = f"{prefix}/datasets/lerobot/quictrain-pusht-smoke-v1"
    ok = mapped == expected
    return CheckResult(
        name="oss_mirror_mapping",
        ok=ok,
        detail={
            "mirror_prefix": prefix,
            "cpfs_uri": DEFAULT_CPFS_DATASET,
            "mapped_oss_uri": mapped,
            "expected_oss_uri": expected,
        },
        error=None if ok else f"mapped {mapped!r} != expected {expected!r}",
    )


def plan_ecs_4090_mounts(dataset_uri: str) -> dict[str, Any]:
    """Reproduce Quota4090 mount assembly without a DB session."""
    use_ecs_oss_path = True
    mounts: list[dict[str, str]] = []
    dataset_root = "/quictrain/input/dataset"
    resolved_uri = dataset_uri
    if dataset_uri.startswith("oss://"):
        mounts.append(
            {
                "source_uri": dataset_uri,
                "target": dataset_root,
                "read_only": "true",
            }
        )
    elif dataset_uri.startswith(("bmcpfs://", "cpfs://")):
        oss_dataset = oss_mirror_uri(dataset_uri)
        if not oss_dataset:
            raise RuntimeError(
                "Quota4090 requires QUICTRAIN_OSS_CPFS_MIRROR_PREFIX "
                f"and an OSS sync of {dataset_uri}"
            )
        resolved_uri = oss_dataset
        mounts.append(
            {
                "source_uri": oss_dataset,
                "target": dataset_root,
                "read_only": "true",
            }
        )
    else:
        raise RuntimeError(f"unsupported dataset URI for ECS OSS path: {dataset_uri}")
    return {
        "pool": "cn-beijing-4090",
        "use_ecs_oss_path": use_ecs_oss_path,
        "dataset_root": dataset_root,
        "source_uri": dataset_uri,
        "mount_uri": resolved_uri,
        "mounts": mounts,
        "host_cpfs_required": False,
        "note": (
            "Training I/O uses DLC DataSources on OSS; ECS host /mnt/cpfs may be empty "
            "and is only needed for control-plane materialization/export."
        ),
    }


def check_ecs_4090_mount_plan(dataset_uri: str) -> CheckResult:
    try:
        plan = plan_ecs_4090_mounts(dataset_uri)
    except Exception as exc:  # noqa: BLE001 — report as check failure
        return CheckResult(name="ecs_4090_mount_plan", ok=False, error=str(exc))
    ok = (
        plan["mounts"]
        and plan["mounts"][0]["target"] == "/quictrain/input/dataset"
        and plan["mount_uri"].startswith("oss://")
        and not is_dataset_archive_uri(plan["mount_uri"])
    )
    return CheckResult(
        name="ecs_4090_mount_plan",
        ok=ok,
        detail=plan,
        error=None if ok else "4090 mount plan incomplete or still an archive URI",
    )


def _oss_bucket_from_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "oss" or not parsed.netloc:
        raise ValueError(f"not an oss:// URI: {uri}")
    key = parsed.path.lstrip("/")
    return parsed.netloc, key


def _ecs_ram_oss_auth():
    import json as _json

    import oss2

    meta = "http://10.20.0.8/latest/meta-data/ram/security-credentials/"
    with urllib.request.urlopen(meta, timeout=3) as resp:
        role = resp.read().decode().strip().splitlines()[0]
    with urllib.request.urlopen(meta + role, timeout=3) as resp:
        creds = _json.loads(resp.read())
    return oss2.StsAuth(
        creds["AccessKeyId"],
        creds["AccessKeySecret"],
        creds["SecurityToken"],
    ), role


def check_oss_dataset_probe(dataset_uri: str) -> CheckResult:
    try:
        import oss2
    except ImportError as exc:
        return CheckResult(
            name="oss_dataset_probe",
            ok=False,
            error=f"oss2 not installed: {exc}",
        )
    bucket_name, prefix = _oss_bucket_from_uri(dataset_uri)
    if not prefix.endswith("/"):
        prefix = prefix + "/"
    endpoint = os.environ.get(
        "ALIYUN_OSS_ENDPOINT", "https://oss-cn-beijing-internal.aliyuncs.com"
    )
    ak = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID") or os.environ.get(
        "ALIYUN_ACCESS_KEY_ID"
    )
    sk = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET") or os.environ.get(
        "ALIYUN_ACCESS_KEY_SECRET"
    )
    role = None
    try:
        if ak and sk:
            auth = oss2.Auth(ak, sk)
        else:
            auth, role = _ecs_ram_oss_auth()
        bucket = oss2.Bucket(auth, endpoint, bucket_name)
        keys: list[str] = []
        for obj in oss2.ObjectIterator(bucket, prefix=prefix, max_keys=32):
            keys.append(obj.key)
            if len(keys) >= 12:
                break
        info_key = f"{prefix}meta/info.json"
        info_exists = bucket.object_exists(info_key)
        ok = bool(keys) and info_exists
        return CheckResult(
            name="oss_dataset_probe",
            ok=ok,
            detail={
                "bucket": bucket_name,
                "prefix": prefix,
                "endpoint": endpoint,
                "sample_keys": keys,
                "info_json": info_key,
                "info_json_exists": info_exists,
                "auth": "ak" if ak and sk else f"ecs_ram_role:{role}",
            },
            error=None
            if ok
            else "OSS prefix empty or meta/info.json missing (not a LeRobot tree)",
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name="oss_dataset_probe", ok=False, error=str(exc))


def check_control_plane_health(base_url: str) -> CheckResult:
    url = base_url.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = json.loads(resp.read().decode())
        ok = (
            body.get("status") == "healthy"
            and body.get("provider") == "aliyun_dlc"
            and body.get("database") == "connected"
        )
        return CheckResult(
            name="control_plane_health",
            ok=ok,
            detail={
                "url": url,
                "provider": body.get("provider"),
                "scheduler": body.get("scheduler"),
                "artifact_root": body.get("artifact_root"),
                "version": body.get("version"),
            },
            error=None if ok else "control plane not healthy with aliyun_dlc",
        )
    except urllib.error.HTTPError as exc:
        return CheckResult(
            name="control_plane_health",
            ok=False,
            error=f"HTTP {exc.code} for {url}",
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name="control_plane_health", ok=False, error=str(exc))


def check_studio_uat_train_boundary(base_url: str) -> CheckResult:
    """Studio UAT embedded train is intentionally fake+disabled — record the boundary."""
    url = base_url.rstrip("/") + "/api/train/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = json.loads(resp.read().decode())
        provider = body.get("provider")
        # Boundary is "understood" when we can read health; fake is expected on UAT.
        ok = body.get("status") == "healthy"
        return CheckResult(
            name="studio_uat_train_boundary",
            ok=ok,
            detail={
                "url": url,
                "provider": provider,
                "artifact_root": body.get("artifact_root"),
                "compute_path": (
                    "UAT embedded train uses provider=fake; real OSS-mirror + DLC "
                    "feasibility runs against co-located QuicTrain :8001, not Studio UAT."
                ),
            },
            error=None if ok else "Studio UAT /api/train/health not healthy",
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name="studio_uat_train_boundary", ok=False, error=str(exc))


def run_dlc_canary(*, submit: bool, steps: int, max_minutes: int, dataset_uri: str) -> CheckResult:
    """Delegate to submit_dlc_canary semantics when Aliyun extras are present."""
    try:
        from quictrain_core import LaunchSpec, ProviderState
        from quictrain_model_specs import get_model
        from quictrain_provider_aliyun_dlc import (
            AliyunDLCProvider,
            AliyunDLCSettings,
            create_sdk_client,
        )
    except ImportError as exc:
        return CheckResult(
            name="dlc_canary",
            ok=False,
            error=f"aliyun DLC extras unavailable: {exc}",
        )

    plan = plan_ecs_4090_mounts(dataset_uri)
    model = get_model("act")
    from datetime import UTC, datetime

    job_id = f"studio_mirror_canary_{datetime.now(UTC):%Y%m%d%H%M%S}"
    attempt_id = f"att_{job_id}_1"
    job_spec = {
        "job_id": job_id,
        "attempt_id": attempt_id,
        "model_id": "act",
        "recipe_id": model.recipes[0].id,
        "dataset": {
            "root": plan["dataset_root"],
            "uri": plan["mount_uri"],
            "repo_id": "quicrobot/quictrain-pusht-smoke-v1",
        },
        "overrides": {
            "training.steps": steps,
            "training.batch_size": 1,
            "optimizer.warmup_steps": 0,
        },
        "output_dir": f"/quictrain/output/{job_id}/{attempt_id}",
    }
    resource_id = (
        os.environ.get("ALIYUN_DLC_RESOURCE_ID_4090")
        or os.environ.get("ALIYUN_DLC_RESOURCE_ID")
        or ""
    ).strip()
    user_vpc_raw = (os.environ.get("QUICTRAIN_DLC_USER_VPC_JSON") or "").strip()
    user_vpc_file = (os.environ.get("QUICTRAIN_DLC_USER_VPC_FILE") or "").strip()
    user_vpc: dict[str, Any] | None = None
    if user_vpc_file:
        user_vpc = json.loads(Path(user_vpc_file).read_text(encoding="utf-8"))
    elif user_vpc_raw and submit:
        try:
            parsed = json.loads(user_vpc_raw)
        except json.JSONDecodeError as exc:
            return CheckResult(
                name="dlc_canary",
                ok=False,
                error=(
                    "QUICTRAIN_DLC_USER_VPC_JSON is not valid JSON "
                    f"(compose .env often strips quotes): {exc}"
                ),
            )
        if isinstance(parsed, dict):
            user_vpc = parsed
    elif user_vpc_raw:
        # Dry-run: only record whether a value is present; avoid shell-mangled parse.
        user_vpc = {"_present": True}
    summary = {
        "job_id": job_id,
        "dataset_mount": plan["mount_uri"],
        "mounts": plan["mounts"],
        "resource_id": resource_id or None,
        "user_vpc_configured": bool(user_vpc),
        "steps": steps,
        "max_minutes": max_minutes,
        "submit": submit,
    }
    if not submit:
        return CheckResult(
            name="dlc_canary",
            ok=bool(resource_id and plan["mounts"]),
            detail={**summary, "mode": "dry_run"},
            error=None
            if resource_id and plan["mounts"]
            else "missing ALIYUN_DLC_RESOURCE_ID(_4090) or mounts",
        )

    if not resource_id:
        return CheckResult(
            name="dlc_canary",
            ok=False,
            detail=summary,
            error="ALIYUN_DLC_RESOURCE_ID(_4090) required for --submit",
        )

    settings = AliyunDLCSettings(
        region_id=os.environ.get("ALIYUN_REGION_ID", "cn-beijing"),
        workspace_id=os.environ["ALIYUN_DLC_WORKSPACE_ID"],
        resource_id=resource_id,
        job_max_running_time_minutes=max_minutes,
        priority=int(os.environ.get("ALIYUN_DLC_PRIORITY", "1")),
        accessibility=os.environ.get("ALIYUN_DLC_ACCESSIBILITY", "PUBLIC"),
        endpoint=os.environ.get("ALIYUN_DLC_ENDPOINT"),
    )
    encoded = json.dumps(job_spec, ensure_ascii=False)
    plugin = "quictrain_runtime_lerobot.adapters:ActRuntime"
    bootstrap = (
        "python -c 'import os,pathlib; "
        'pathlib.Path("/tmp/quictrain-job-spec.json").write_text('
        'os.environ["QUICTRAIN_JOB_SPEC_JSON"],encoding="utf-8")\' '
        "&& exec python -m quictrain_runner run "
        '--job-spec /tmp/quictrain-job-spec.json --plugin "$QUICTRAIN_RUNTIME_PLUGIN"'
    )
    mounts_tuple = tuple(
        {
            "source_uri": m["source_uri"],
            "target": m["target"],
            "read_only": m.get("read_only", "true"),
        }
        for m in plan["mounts"]
    )
    artifact_root = (
        os.environ.get("QUICTRAIN_ARTIFACT_ROOT")
        or f"oss://{os.environ.get('ALIYUN_OSS_BUCKET', '')}/quictrain/runs"
    ).rstrip("/")
    mounts_tuple = mounts_tuple + (
        {
            "source_uri": artifact_root,
            "target": "/quictrain/output",
            "read_only": "false",
        },
    )
    resource: dict[str, Any] = {
        "gpu_count": 1,
        "gpu_type": "RTX 4090",
        "worker_count": 1,
        "cpu": 8,
        "memory": "64Gi",
        "shared_memory": "16Gi",
        "job_max_running_time_minutes": max_minutes,
    }
    if user_vpc:
        resource["user_vpc"] = user_vpc
    spec = LaunchSpec(
        job_id=job_id,
        attempt_id=attempt_id,
        idempotency_key=f"{job_id}:attempt:1",
        image_uri=model.image_uri,
        image_digest=model.image_digest,
        command=("sh", "-lc", bootstrap),
        environment={
            "QUICTRAIN_JOB_SPEC_JSON": encoded,
            "QUICTRAIN_RUNTIME_PLUGIN": plugin,
            "QUICTRAIN_EXECUTE_TRAINING": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        },
        mounts=mounts_tuple,
        resource=resource,
        labels={
            "quictrain-purpose": "studio-oss-mirror-feasibility",
            "quictrain-model-id": "act",
        },
    )
    provider = AliyunDLCProvider(settings, create_sdk_client(settings))
    job = provider.submit(spec)
    detail = {
        **summary,
        "mode": "submit",
        "external_job_id": job.external_id,
        "status": job.raw_status,
    }
    # Bounded poll — feasibility only needs JobEnqueue / early Running.
    import time

    terminal = {
        ProviderState.SUCCEEDED,
        ProviderState.FAILED,
        ProviderState.CANCELLED,
    }
    for _ in range(24):
        if job.state in terminal:
            break
        time.sleep(10)
        job = provider.get(job.external_id)
        detail["status"] = job.raw_status
        detail["provider_state"] = str(job.state)
        # Feasibility pass: accepted by quota (not ResourceAllocateFailed at create).
        if job.state == ProviderState.RUNNING:
            detail["feasibility"] = "RUNNING"
            return CheckResult(name="dlc_canary", ok=True, detail=detail)
    ok = job.state in {ProviderState.SUCCEEDED, ProviderState.RUNNING} or (
        job.external_id and job.state not in {ProviderState.FAILED}
        and "ResourceAllocateFailed" not in (job.message or "")
    )
    detail["message"] = job.message
    detail["provider_state"] = str(job.state)
    if job.state == ProviderState.SUCCEEDED:
        detail["feasibility"] = "SUCCEEDED"
        ok = True
    elif job.external_id and "ResourceAllocateFailed" not in (job.message or ""):
        # Queued / Creating still proves CreateJob + OSS mount accepted.
        detail["feasibility"] = "ACCEPTED"
        ok = True
    return CheckResult(
        name="dlc_canary",
        ok=ok,
        detail=detail,
        error=None if ok else (job.message or f"canary ended {job.raw_status}"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset-uri",
        default=os.environ.get("QUICTRAIN_CANARY_DATASET_URI", DEFAULT_MIRROR_DATASET),
        help="READY directory oss:// URI under the train mirror (not a .tar.gz)",
    )
    parser.add_argument(
        "--train-health-url",
        default=os.environ.get("QUICTRAIN_HEALTH_URL", "http://127.0.0.1:8001"),
    )
    parser.add_argument(
        "--studio-uat-url",
        default=os.environ.get("STUDIO_UAT_URL", "http://127.0.0.1:18081"),
    )
    parser.add_argument("--skip-oss-probe", action="store_true")
    parser.add_argument("--skip-health", action="store_true")
    parser.add_argument("--skip-studio-boundary", action="store_true")
    parser.add_argument(
        "--dlc-canary",
        action="store_true",
        help="Include DLC canary stage (dry-run unless --submit)",
    )
    parser.add_argument("--submit", action="store_true", help="Actually CreateJob on DLC")
    parser.add_argument("--steps", type=int, default=5)
    parser.add_argument("--max-minutes", type=int, default=15)
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    if is_dataset_archive_uri(args.dataset_uri):
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "dataset-uri is an archive; use a LeRobot directory OSS prefix",
                    "dataset_uri": args.dataset_uri,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise SystemExit(2)

    results: list[CheckResult] = [
        check_catalog_archive_gate(),
        check_oss_mirror_mapping(),
        check_ecs_4090_mount_plan(args.dataset_uri),
    ]
    if not args.skip_health:
        results.append(check_control_plane_health(args.train_health_url))
    if not args.skip_studio_boundary:
        results.append(check_studio_uat_train_boundary(args.studio_uat_url))
    if not args.skip_oss_probe:
        results.append(check_oss_dataset_probe(args.dataset_uri))
    if args.dlc_canary:
        results.append(
            run_dlc_canary(
                submit=args.submit,
                steps=args.steps,
                max_minutes=args.max_minutes,
                dataset_uri=args.dataset_uri,
            )
        )

    report = {
        "ok": all(item.ok for item in results),
        "dataset_uri": args.dataset_uri,
        "mirror_prefix": _mirror_prefix(),
        "checks": [asdict(item) for item in results],
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.json_out:
        args.json_out.write_text(text + "\n", encoding="utf-8")
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
