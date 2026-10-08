from __future__ import annotations

import argparse
import json
import os
import time

from quictrain_core import LaunchSpec, ProviderState
from quictrain_model_specs import get_model
from quictrain_provider_aliyun_dlc import (
    AliyunDLCProvider,
    AliyunDLCSettings,
    create_sdk_client,
)

PI05_REVISION = "7de663972b7817d2c4cf2d84c821153dfea772e9"

BOOTSTRAP_COMMAND = r"""
set -eu
workspace=/mnt/cpfs/quictrain
model_target="$workspace/models/pi05/lerobot-pi05-base"
model_tmp="$workspace/tmp/bootstrap-pi05"
tokenizer_target="$model_target/tokenizer"
tokenizer_tmp="$workspace/tmp/bootstrap-paligemma-tokenizer"
dataset_target="$workspace/datasets/lerobot/quictrain-pusht-smoke-v1"
dataset_tmp="$workspace/tmp/bootstrap-pusht"
mkdir -p "$workspace/models/act" "$workspace/models/pi05" "$workspace/datasets/lerobot"
mkdir -p "$workspace/runs" "$workspace/work" "$workspace/cache/huggingface" "$workspace/cache/torch"
mkdir -p "$workspace/tmp" "$workspace/exports"
if [ ! -f "$model_target/_quictrain_asset.json" ]; then
  rm -rf "$model_tmp"
  mkdir -p "$model_tmp"
  HF_ENDPOINT=https://hf-mirror.com MODEL_TMP="$model_tmp" python -c \
    'import os; from huggingface_hub import snapshot_download; '\
'snapshot_download(repo_id="lerobot/pi05_base", '\
'revision="7de663972b7817d2c4cf2d84c821153dfea772e9", '\
'local_dir=os.environ["MODEL_TMP"])'
  test -s "$model_tmp/model.safetensors"
  test -s "$model_tmp/config.json"
  test -s "$model_tmp/policy_preprocessor.json"
  test -s "$model_tmp/policy_postprocessor.json"
  printf '%s' '{"model_id":"pi05","source":"lerobot/pi05_base",' \
    > "$model_tmp/_quictrain_asset.json"
  printf '%s\n' '"revision":"7de663972b7817d2c4cf2d84c821153dfea772e9"}' \
    >> "$model_tmp/_quictrain_asset.json"
  mv "$model_tmp" "$model_target"
fi
if [ -d /quictrain/source/paligemma-tokenizer ]; then
  rm -rf "$tokenizer_tmp"
  mkdir -p "$tokenizer_tmp"
  cp -a /quictrain/source/paligemma-tokenizer/. "$tokenizer_tmp/"
  tokenizer_files="added_tokens.json special_tokens_map.json tokenizer.json"
  tokenizer_files="$tokenizer_files tokenizer.model tokenizer_config.json"
  for file in $tokenizer_files; do
    test -s "$tokenizer_tmp/$file"
  done
  rm -rf "$tokenizer_target"
  mv "$tokenizer_tmp" "$tokenizer_target"
fi
tokenizer_files="added_tokens.json special_tokens_map.json tokenizer.json"
tokenizer_files="$tokenizer_files tokenizer.model tokenizer_config.json"
for file in $tokenizer_files; do
  if [ ! -s "$tokenizer_target/$file" ]; then
    echo "MODEL_ASSET_MISSING: official PaliGemma tokenizer is not mirrored to CPFS ($file)" >&2
    exit 42
  fi
done
MODEL_TARGET="$model_target" python -c \
  'import hashlib,json,os,pathlib,shutil; p=pathlib.Path(os.environ["MODEL_TARGET"]); '\
'runtime=p/"policy_preprocessor.json"; upstream=p/"policy_preprocessor.upstream.json"; '\
'shutil.copy2(runtime,upstream) if not upstream.exists() else None; '\
'data=json.loads(upstream.read_text()); '\
'data["steps"]=[s for s in data["steps"] '\
'if s.get("registry_name")!="relative_actions_processor"]; '\
'tokenizer=next(s for s in data["steps"] '\
'if s.get("registry_name")=="tokenizer_processor"); '\
'tokenizer["config"]["tokenizer_name"]=str(p/"tokenizer"); '\
'runtime.write_text(json.dumps(data,indent=2,ensure_ascii=False)+"\n"); '\
'sha=lambda f:hashlib.sha256(f.read_bytes()).hexdigest(); '\
'marker={"model_id":"pi05","source":"lerobot/pi05_base",'\
'"revision":"7de663972b7817d2c4cf2d84c821153dfea772e9",'\
'"upstream_processor_sha256":sha(upstream),"runtime_processor_sha256":sha(runtime),'\
'"transformations":["remove_disabled_relative_actions_processor",'\
'"use_cpfs_paligemma_tokenizer"]}; '\
  '(p/"_quictrain_asset.json").write_text(json.dumps(marker,indent=2,ensure_ascii=False)+"\n")'
if [ ! -f "$dataset_target/_quictrain_asset.json" ]; then
  rm -rf "$dataset_tmp"
  mkdir -p "$dataset_tmp"
  cp -a /quictrain/source/pusht/. "$dataset_tmp/"
  DATASET_TMP="$dataset_tmp" python -c \
    'import json,os,pathlib; marker={"dataset_version_id":"dsv_pusht_cpfs_v1",'\
'"source":os.environ["QUICTRAIN_PUSHT_SOURCE_URI"]}; '\
'pathlib.Path(os.environ["DATASET_TMP"],"_quictrain_asset.json").write_text('\
'json.dumps(marker,indent=2,ensure_ascii=False)+"\n")'
  mv "$dataset_tmp" "$dataset_target"
fi
printf '%s' '{"model_id":"act","initialization":"from_scratch",' \
  > "$workspace/models/act/_quictrain_asset.json"
printf '%s\n' '"upstream_ref":"1396b9fab7aecddd10006c33c47a487ffdcb54b4"}' \
  >> "$workspace/models/act/_quictrain_asset.json"
sha256sum "$model_target/model.safetensors" "$model_target/config.json" \
  "$model_target/policy_preprocessor.upstream.json" "$model_target/policy_preprocessor.json" \
  "$model_target/policy_postprocessor.json" "$model_target/tokenizer/tokenizer.json"
du -sh "$model_target" "$dataset_target"
find "$workspace" -maxdepth 2 -type d | sort
""".strip()


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required setting: {name}")
    return value


def build_launch_spec(max_minutes: int) -> LaunchSpec:
    model = get_model("act")
    job_id = "admin_cpfs_bootstrap_v11b"
    attempt_id = "att_admin_cpfs_bootstrap_v11b_1"
    pusht_uri = required("QUICTRAIN_CPFS_BOOTSTRAP_PUSHT_URI")
    mounts = [
        {
            "source_uri": required("QUICTRAIN_CPFS_ROOT_URI") + "/",
            "target": "/mnt/cpfs",
            "read_only": "false",
        },
        {
            "source_uri": pusht_uri,
            "target": "/quictrain/source/pusht",
            "read_only": "true",
        },
    ]
    tokenizer_source_uri = os.environ.get("QUICTRAIN_PI05_TOKENIZER_SOURCE_URI", "").strip()
    if tokenizer_source_uri:
        mounts.append(
            {
                "source_uri": tokenizer_source_uri,
                "target": "/quictrain/source/paligemma-tokenizer",
                "read_only": "true",
            }
        )
    return LaunchSpec(
        job_id=job_id,
        attempt_id=attempt_id,
        idempotency_key=f"{job_id}:attempt:1",
        image_uri=model.image_uri,
        image_digest=model.image_digest,
        command=("bash", "-lc", BOOTSTRAP_COMMAND),
        environment={
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "QUICTRAIN_PUSHT_SOURCE_URI": pusht_uri,
        },
        mounts=tuple(mounts),
        resource={
            "gpu_count": 1,
            "gpu_type": "H20",
            "cpu": 8,
            "memory": "64Gi",
            "worker_count": 1,
            "job_max_running_time_minutes": max_minutes,
        },
        labels={"quictrain-purpose": "v1.1-cpfs-bootstrap"},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Idempotently bootstrap QuicTrain assets in CPFS")
    parser.add_argument("--max-minutes", type=int, default=90)
    parser.add_argument("--submit", action="store_true")
    args = parser.parse_args()
    if not 10 <= args.max_minutes <= 180:
        raise SystemExit("max-minutes must be between 10 and 180")

    spec = build_launch_spec(args.max_minutes)
    print(
        json.dumps(
            {
                "job_id": spec.job_id,
                "image": f"{spec.image_uri}@{spec.image_digest}",
                "cpfs_data_source_id": required("QUICTRAIN_CPFS_DATA_SOURCE_ID"),
                "cpfs_uri": spec.mounts[0]["source_uri"],
                "pi05_revision": PI05_REVISION,
                "pi05_tokenizer_source_uri": os.environ.get("QUICTRAIN_PI05_TOKENIZER_SOURCE_URI"),
                "pusht_uri": spec.mounts[1]["source_uri"],
            },
            indent=2,
        )
    )
    if not args.submit:
        return

    settings = AliyunDLCSettings(
        region_id=required("ALIYUN_REGION_ID"),
        workspace_id=required("ALIYUN_DLC_WORKSPACE_ID"),
        resource_id=required("ALIYUN_DLC_RESOURCE_ID"),
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
        raise SystemExit(f"Bootstrap ended in {job.raw_status}: {job.message or ''}")


if __name__ == "__main__":
    main()
