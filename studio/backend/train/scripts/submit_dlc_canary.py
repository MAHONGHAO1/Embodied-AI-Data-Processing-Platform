from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime

from quictrain_config import resolve_config
from quictrain_core import LaunchSpec, ProviderState
from quictrain_model_specs import get_model
from quictrain_provider_aliyun_dlc import (
    AliyunDLCProvider,
    AliyunDLCSettings,
    create_sdk_client,
)

PUSHT_REVISION = "7628202a2180972f291ba1bc6723834921e72c19"
RUNTIME_JOB_SPEC_PATH = "/tmp/quictrain-job-spec.json"  # nosec B108 - container-local handoff path
RUNTIME_BOOTSTRAP_COMMAND = (
    "python -c 'import os,pathlib; "
    f'pathlib.Path("{RUNTIME_JOB_SPEC_PATH}").write_text('
    'os.environ["QUICTRAIN_JOB_SPEC_JSON"],encoding="utf-8")\' '
    "&& exec python -m quictrain_runner run "
    f'--job-spec {RUNTIME_JOB_SPEC_PATH} --plugin "$QUICTRAIN_RUNTIME_PLUGIN"'
)


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required setting: {name}")
    return value


def build_launch_spec(model_id: str, steps: int, max_minutes: int) -> LaunchSpec:
    model = get_model(model_id)
    job_id = f"canary_{model_id}_{datetime.now(UTC):%Y%m%d%H%M%S}"
    attempt_id = f"att_{job_id}_1"
    overrides = {
        "training.steps": steps,
        "training.batch_size": 1,
        "optimizer.warmup_steps": 0,
    }
    resolved, _ = resolve_config(model_id, overrides, role="admin")
    dataset_mount_uri = os.environ.get("QUICTRAIN_CANARY_DATASET_URI", "").rstrip("/")
    dataset_root = os.environ.get("QUICTRAIN_CANARY_DATASET_ROOT", "").rstrip("/")
    if bool(dataset_mount_uri) != bool(dataset_root):
        raise SystemExit(
            "QUICTRAIN_CANARY_DATASET_URI and QUICTRAIN_CANARY_DATASET_ROOT must be set together"
        )
    dataset_repo_id = os.environ.get(
        "QUICTRAIN_CANARY_DATASET_REPO_ID", "quicrobot/quictrain-pusht-smoke-v1"
    )
    output_mount = "/quictrain/output"
    artifact_root = os.environ.get("QUICTRAIN_ARTIFACT_ROOT", "").rstrip("/")
    output_uri = (
        f"{artifact_root}/{job_id}/{attempt_id}" if artifact_root.startswith("oss://") else None
    )
    output_dir = f"{output_mount}/{job_id}/{attempt_id}" if output_uri else output_mount
    if output_uri:
        resolved["runtime"]["output_uri"] = output_uri
    dataset = {
        "uri": dataset_mount_uri or f"hf://datasets/lerobot/pusht@{PUSHT_REVISION}",
        "repo_id": dataset_repo_id if dataset_mount_uri else "lerobot/pusht",
        "checksum": os.environ.get(
            "QUICTRAIN_CANARY_DATASET_CHECKSUM",
            "generated:quictrain-pusht-smoke-v1" if dataset_mount_uri else f"git:{PUSHT_REVISION}",
        ),
        "camera_keys": ["observation.image"],
        "action_dim": 2,
        "episodes": int(
            os.environ.get("QUICTRAIN_CANARY_DATASET_EPISODES", "4" if dataset_mount_uri else "206")
        ),
    }
    if dataset_root:
        dataset["root"] = dataset_root
    if not dataset_mount_uri:
        dataset["revision"] = PUSHT_REVISION
    job_spec = {
        "protocol_version": "v1alpha1",
        "job_id": job_id,
        "attempt_id": attempt_id,
        "model_id": model.id,
        "model_version_id": model.version_id,
        "recipe_id": "fine_tune",
        "adapter_version": model.adapter_version,
        "dataset": dataset,
        "resolved_config": resolved,
        "source": {
            "upstream_ref": model.upstream_ref,
            "image_digest": model.image_digest,
        },
        "output_dir": output_dir,
    }
    if output_uri:
        job_spec["output_uri"] = output_uri
    encoded = json.dumps(job_spec, separators=(",", ":"))
    plugin = {
        "act": "quictrain_runtime_lerobot.adapters:ActRuntime",
        "pi05": "quictrain_runtime_lerobot.adapters:Pi05Runtime",
    }[model_id]
    mounts: tuple[dict[str, str], ...] = ()
    if dataset_mount_uri:
        mounts += (
            {
                "source_uri": dataset_mount_uri,
                "target": "/quictrain/input",
                "read_only": "true",
            },
        )
    if output_uri:
        mounts += (
            {
                "source_uri": artifact_root,
                "target": output_mount,
                "read_only": "false",
            },
        )
    return LaunchSpec(
        job_id=job_id,
        attempt_id=attempt_id,
        idempotency_key=f"{job_id}:attempt:1",
        image_uri=model.image_uri,
        image_digest=model.image_digest,
        command=("sh", "-lc", RUNTIME_BOOTSTRAP_COMMAND),
        environment={
            "QUICTRAIN_JOB_SPEC_JSON": encoded,
            "QUICTRAIN_RUNTIME_PLUGIN": plugin,
            "QUICTRAIN_EXECUTE_TRAINING": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            **({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"} if dataset_mount_uri else {}),
        },
        mounts=mounts,
        resource={
            "gpu_count": 1,
            "gpu_type": "H20",
            "worker_count": 1,
            "job_max_running_time_minutes": max_minutes,
        },
        labels={"quictrain-purpose": "v1-canary", "quictrain-model-id": model_id},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Submit a bounded QuicTrain DLC canary")
    parser.add_argument("--model", choices=["act", "pi05"], default="act")
    parser.add_argument("--steps", type=int, default=5)
    parser.add_argument("--max-minutes", type=int, default=15)
    parser.add_argument("--submit", action="store_true")
    args = parser.parse_args()
    if args.steps < 1 or not 1 <= args.max_minutes <= 60:
        raise SystemExit("steps must be positive and max-minutes must be between 1 and 60")

    spec = build_launch_spec(args.model, args.steps, args.max_minutes)
    summary = {
        "job_id": spec.job_id,
        "model": args.model,
        "image": f"{spec.image_uri}@{spec.image_digest}",
        "gpu": "1x H20",
        "steps": args.steps,
        "max_minutes": args.max_minutes,
        "dataset": os.environ.get("QUICTRAIN_CANARY_DATASET_URI") or "lerobot/pusht",
    }
    print(json.dumps(summary, indent=2))
    if not args.submit:
        print("Dry run only. Add --submit after quota/SKU and artifact settings are verified.")
        return

    settings = AliyunDLCSettings(
        region_id=required("ALIYUN_REGION_ID"),
        workspace_id=required("ALIYUN_DLC_WORKSPACE_ID"),
        resource_id=required("ALIYUN_DLC_RESOURCE_ID"),
        ecs_spec=os.environ.get("ALIYUN_DLC_ECS_SPEC") or None,
        gpu_type=os.environ.get("ALIYUN_DLC_GPU_TYPE") or None,
        job_max_running_time_minutes=args.max_minutes,
        priority=int(os.environ.get("ALIYUN_DLC_PRIORITY", "1")),
        accessibility=os.environ.get("ALIYUN_DLC_ACCESSIBILITY", "PUBLIC"),
        endpoint=os.environ.get("ALIYUN_DLC_ENDPOINT"),
    )
    provider = AliyunDLCProvider(settings, create_sdk_client(settings))
    job = provider.submit(spec)
    print(json.dumps({"external_job_id": job.external_id, "status": job.raw_status}))
    while job.state not in {
        ProviderState.SUCCEEDED,
        ProviderState.FAILED,
        ProviderState.CANCELLED,
    }:
        time.sleep(15)
        job = provider.get(job.external_id)
        print(json.dumps({"external_job_id": job.external_id, "status": job.raw_status}))
    if job.state != ProviderState.SUCCEEDED:
        raise SystemExit(f"Canary ended in {job.raw_status}: {job.message or ''}")


if __name__ == "__main__":
    main()
