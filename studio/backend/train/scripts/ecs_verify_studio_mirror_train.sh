#!/usr/bin/env bash
# ECS host runner for Studio ↔ QuicTrain OSS-mirror training feasibility.
# Intended to run on the co-located ECS (i-2ze11xs8mqln1c3ehrj9) via Cloud Assistant
# or an interactive shell. Does not print secrets.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPORT_DIR="${REPORT_DIR:-/tmp/studio-oss-mirror-feasibility}"
DATASET_URI="${QUICTRAIN_CANARY_DATASET_URI:-oss://oss-pai-1183v1b6du4vkucj3h-cn-beijing/quictrain/datasets/lerobot/quictrain-pusht-smoke-v1}"
SUBMIT="${SUBMIT:-0}"
mkdir -p "${REPORT_DIR}"

echo "== 1) inject QUICTRAIN_OSS_CPFS_MIRROR_PREFIX into live QuicTrain compose if missing =="
COMPOSE="/opt/quictrain/current/infra/ecs/docker-compose.yml"
ENV_FILE="/opt/quictrain/current/infra/ecs/.env"
if [[ -f "${COMPOSE}" ]]; then
  if ! grep -q 'QUICTRAIN_OSS_CPFS_MIRROR_PREFIX:' "${COMPOSE}"; then
    python3 - <<'PY'
from pathlib import Path
path = Path("/opt/quictrain/current/infra/ecs/docker-compose.yml")
text = path.read_text()
needle = "      QUICTRAIN_EXPORT_OSS_PREFIX: oss://${ALIYUN_OSS_BUCKET}/quictrain/exports\n"
insert = (
    needle
    + "      # Quota4090 / ECS: bmcpfs://…/quictrain/… → {prefix}/… for DLC OSS mounts.\n"
    + "      QUICTRAIN_OSS_CPFS_MIRROR_PREFIX: ${QUICTRAIN_OSS_CPFS_MIRROR_PREFIX:-oss://${ALIYUN_OSS_BUCKET}/quictrain}\n"
)
if needle not in text:
    raise SystemExit("compose needle not found; refuse to patch")
if "QUICTRAIN_OSS_CPFS_MIRROR_PREFIX:" in text:
    print("already present")
else:
    path.write_text(text.replace(needle, insert, 1))
    print("patched compose")
PY
    (cd /opt/quictrain/current && docker compose --env-file infra/ecs/.env -f infra/ecs/docker-compose.yml up -d --force-recreate api scheduler) || true
    sleep 12
    docker ps --filter name=quictrain-api --format '{{.Names}} {{.Status}}' || true
    docker ps --filter name=quictrain-scheduler --format '{{.Names}} {{.Status}}' || true
  else
    echo "compose already declares QUICTRAIN_OSS_CPFS_MIRROR_PREFIX"
  fi
else
  echo "WARN: ${COMPOSE} missing — skip live compose patch"
fi

echo "== 2) export runtime env from compose containers (no secret values) =="
export ALIYUN_OSS_BUCKET="$(docker exec quictrain-api-1 printenv ALIYUN_OSS_BUCKET 2>/dev/null || true)"
export ALIYUN_DLC_WORKSPACE_ID="$(docker exec quictrain-api-1 printenv ALIYUN_DLC_WORKSPACE_ID 2>/dev/null || true)"
export ALIYUN_DLC_RESOURCE_ID="$(docker exec quictrain-api-1 printenv ALIYUN_DLC_RESOURCE_ID 2>/dev/null || true)"
export ALIYUN_DLC_ENDPOINT="$(docker exec quictrain-api-1 printenv ALIYUN_DLC_ENDPOINT 2>/dev/null || true)"
export ALIYUN_REGION_ID="$(docker exec quictrain-api-1 printenv ALIYUN_REGION_ID 2>/dev/null || echo cn-beijing)"
export QUICTRAIN_CPFS_ROOT_URI="$(docker exec quictrain-api-1 printenv QUICTRAIN_CPFS_ROOT_URI 2>/dev/null || true)"
    # Prefer reading UserVpc from the api container file to avoid shell quoting.
    if [[ -z "${QUICTRAIN_DLC_USER_VPC_JSON:-}" ]]; then
      QUICTRAIN_DLC_USER_VPC_JSON="$(docker exec quictrain-api-1 printenv QUICTRAIN_DLC_USER_VPC_JSON 2>/dev/null || true)"
      export QUICTRAIN_DLC_USER_VPC_JSON
    fi
    docker exec quictrain-api-1 printenv QUICTRAIN_DLC_USER_VPC_JSON >/tmp/quictrain_user_vpc.json 2>/dev/null || true
    if [[ -s /tmp/quictrain_user_vpc.json ]]; then
      export QUICTRAIN_DLC_USER_VPC_JSON="$(cat /tmp/quictrain_user_vpc.json)"
    fi
export QUICTRAIN_ARTIFACT_ROOT="$(docker exec quictrain-api-1 printenv QUICTRAIN_ARTIFACT_ROOT 2>/dev/null || true)"
export ALIYUN_OSS_ENDPOINT="$(docker exec quictrain-api-1 printenv ALIYUN_OSS_ENDPOINT 2>/dev/null || echo https://oss-cn-beijing-internal.aliyuncs.com)"
if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  mirror_line="$(grep -E '^QUICTRAIN_OSS_CPFS_MIRROR_PREFIX=' "${ENV_FILE}" | head -1 || true)"
  if [[ -n "${mirror_line}" ]]; then
    export QUICTRAIN_OSS_CPFS_MIRROR_PREFIX="${mirror_line#*=}"
  fi
fi
if [[ -z "${QUICTRAIN_OSS_CPFS_MIRROR_PREFIX:-}" && -n "${ALIYUN_OSS_BUCKET:-}" ]]; then
  export QUICTRAIN_OSS_CPFS_MIRROR_PREFIX="oss://${ALIYUN_OSS_BUCKET}/quictrain"
fi
# Prefer 4090 quota for OSS-mirror feasibility when present in .env pool map.
if [[ -f "${ENV_FILE}" ]]; then
  pool_line="$(grep -E '^QUICTRAIN_POOL_RESOURCE_IDS=' "${ENV_FILE}" | head -1 || true)"
  if [[ "${pool_line}" == *4090:* ]]; then
    rid="$(python3 -c "
import re
from pathlib import Path
text = Path(r'''${ENV_FILE}''').read_text()
m = re.search(r'QUICTRAIN_POOL_RESOURCE_IDS=([^\n]+)', text)
ids = m.group(1) if m else ''
mm = re.search(r'cn-beijing-4090:([^,\s]+)', ids)
print(mm.group(1) if mm else '')
")"
    if [[ -n "${rid}" ]]; then
      export ALIYUN_DLC_RESOURCE_ID_4090="${rid}"
    fi
  fi
fi

echo "bucket=${ALIYUN_OSS_BUCKET:-unset} mirror=${QUICTRAIN_OSS_CPFS_MIRROR_PREFIX:-unset} ws=${ALIYUN_DLC_WORKSPACE_ID:-unset}"
echo "host_cpfs=$(if [[ -d /mnt/cpfs/quictrain ]]; then echo present; else echo absent_or_empty; fi)"

echo "== 2b) host-side Studio UAT + OSS probes (container network/oss2 may lack these) =="
HOST_CHECKS_JSON="${REPORT_DIR}/host_checks.json"
python3 - <<PY
import json, urllib.request, pathlib
report = {"studio_uat_train_boundary": None, "oss_dataset_probe": None}
# Studio UAT embedded train boundary
try:
    with urllib.request.urlopen("http://127.0.0.1:18081/api/train/health", timeout=5) as resp:
        body = json.loads(resp.read().decode())
    report["studio_uat_train_boundary"] = {
        "ok": body.get("status") == "healthy",
        "detail": {
            "provider": body.get("provider"),
            "artifact_root": body.get("artifact_root"),
            "compute_path": (
                "UAT embedded train uses provider=fake; real OSS-mirror + DLC "
                "feasibility runs against co-located QuicTrain :8001."
            ),
        },
        "error": None if body.get("status") == "healthy" else "not healthy",
    }
except Exception as exc:
    report["studio_uat_train_boundary"] = {"ok": False, "detail": {}, "error": str(exc)}

# OSS LeRobot tree via public endpoint + ECS role if available, else skip mark
dataset = "${DATASET_URI}"
prefix = dataset.split("oss://", 1)[-1]
bucket_name, _, key = prefix.partition("/")
if not key.endswith("/"):
    key = key + "/"
info_key = key + "meta/info.json"
try:
    import oss2, urllib.request as u
    role = None
    auth = None
    for meta in (
        "http://10.10.10.10/latest/meta-data/ram/security-credentials/",
        "http://100.100.100.200/latest/meta-data/ram/security-credentials/",
    ):
        try:
            role = u.urlopen(meta, timeout=2).read().decode().strip().splitlines()[0]
            creds = json.loads(u.urlopen(meta + role, timeout=2).read())
            auth = oss2.StsAuth(creds["AccessKeyId"], creds["AccessKeySecret"], creds["SecurityToken"])
            break
        except Exception:
            continue
    if auth is None:
        raise RuntimeError("ECS RAM role metadata unreachable from host")
    bucket = oss2.Bucket(auth, "https://oss-cn-beijing-internal.aliyuncs.com", bucket_name)
    keys = []
    for obj in oss2.ObjectIterator(bucket, prefix=key, max_keys=32):
        keys.append(obj.key)
        if len(keys) >= 8:
            break
    info_exists = bucket.object_exists(info_key)
    ok = bool(keys) and info_exists
    report["oss_dataset_probe"] = {
        "ok": ok,
        "detail": {
            "bucket": bucket_name,
            "prefix": key,
            "sample_keys": keys,
            "info_json": info_key,
            "info_json_exists": info_exists,
            "auth": f"ecs_ram_role:{role}",
        },
        "error": None if ok else "empty prefix or missing meta/info.json",
    }
except Exception as exc:
    report["oss_dataset_probe"] = {"ok": False, "detail": {}, "error": str(exc)}

pathlib.Path("${HOST_CHECKS_JSON}").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\\n")
print(json.dumps(report, ensure_ascii=False, indent=2))
PY

echo "== 3) run feasibility script (prefer QuicTrain container PYTHONPATH) =="
SCRIPT_SRC="${ROOT_DIR}/scripts/verify_oss_mirror_train_feasibility.py"
if [[ ! -f "${SCRIPT_SRC}" ]]; then
  # When invoked from a copied path under /tmp
  SCRIPT_SRC="$(dirname "${BASH_SOURCE[0]}")/verify_oss_mirror_train_feasibility.py"
fi
cp -f "${SCRIPT_SRC}" /tmp/verify_oss_mirror_train_feasibility.py

CANARY_ARGS=()
if [[ "${SUBMIT}" == "1" ]]; then
  CANARY_ARGS=(--dlc-canary --submit)
else
  CANARY_ARGS=(--dlc-canary)
fi

# Recover UserVpc for submit from compose config (running container may have
# quote-stripped QUICTRAIN_DLC_USER_VPC_JSON from .env interpolation).
if [[ "${SUBMIT}" == "1" ]]; then
  python3 - <<'PY' > /tmp/quictrain_user_vpc.json
import json, re, subprocess, sys

def recover_mangled(raw: str) -> dict:
    """Recover UserVpc after compose .env strips JSON quotes."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # {VpcId:vpc-xxx,SwitchId:vsw-yyy,SecurityGroupId:sg-zzz,DefaultRoute:eth1,ExtendedCIDRs:[192.168.1.0/24]}
    m = re.fullmatch(r"\{(.*)\}", raw, flags=re.S)
    if not m:
        raise ValueError(f"unrecognized UserVpc payload: {raw[:80]!r}")
    body = m.group(1)
    out: dict = {}
    # ExtendedCIDRs list first
    list_m = re.search(r"ExtendedCIDRs:\[([^\]]*)\]", body)
    if list_m:
        out["ExtendedCIDRs"] = [item.strip() for item in list_m.group(1).split(",") if item.strip()]
        body = body[: list_m.start()] + body[list_m.end() :]
    for part in body.split(","):
        part = part.strip().strip(",")
        if not part or ":" not in part:
            continue
        key, value = part.split(":", 1)
        out[key.strip()] = value.strip()
    required = {"VpcId", "SwitchId", "SecurityGroupId"}
    if not required.issubset(out):
        raise ValueError(f"recovered UserVpc missing keys: {sorted(required - set(out))}")
    return out

parsed = None
# 1) YAML compose config (often keeps quotes inside single-quoted scalar)
raw = subprocess.check_output(
    [
        "docker",
        "compose",
        "--env-file",
        "infra/ecs/.env",
        "-f",
        "infra/ecs/docker-compose.yml",
        "config",
    ],
    cwd="/opt/quictrain/current",
    text=True,
)
m = re.search(r"QUICTRAIN_DLC_USER_VPC_JSON:\s*'(\{.*?\})'", raw, flags=re.S)
if m:
    try:
        parsed = json.loads(m.group(1))
    except json.JSONDecodeError:
        parsed = recover_mangled(m.group(1))
# 2) live container env (may be quote-stripped)
if parsed is None:
    live = subprocess.check_output(
        ["docker", "exec", "quictrain-api-1", "printenv", "QUICTRAIN_DLC_USER_VPC_JSON"],
        text=True,
    ).strip()
    parsed = recover_mangled(live)

json.dump(parsed, sys.stdout)
print(" recovered_keys=", sorted(parsed), file=sys.stderr)
PY
  docker cp /tmp/quictrain_user_vpc.json quictrain-api-1:/tmp/quictrain_user_vpc.json
fi

# Run inside api container so quictrain_* imports and RAM/role match control plane.
# Do NOT override QUICTRAIN_DLC_USER_VPC_JSON via -e — shell quoting strips JSON quotes.
docker cp /tmp/verify_oss_mirror_train_feasibility.py quictrain-api-1:/tmp/verify_oss_mirror_train_feasibility.py
set +e
if [[ "${SUBMIT}" == "1" ]]; then
  docker exec \
    -e QUICTRAIN_CANARY_DATASET_URI="${DATASET_URI}" \
    -e ALIYUN_DLC_RESOURCE_ID_4090="${ALIYUN_DLC_RESOURCE_ID_4090:-}" \
    -e QUICTRAIN_DLC_USER_VPC_FILE=/tmp/quictrain_user_vpc.json \
    quictrain-api-1 \
    python /tmp/verify_oss_mirror_train_feasibility.py \
      --dataset-uri "${DATASET_URI}" \
      --train-health-url http://127.0.0.1:8000 \
      --skip-studio-boundary \
      --skip-oss-probe \
      --json-out /tmp/studio_oss_mirror_feasibility.json \
      "${CANARY_ARGS[@]}"
else
  docker exec \
    -e QUICTRAIN_CANARY_DATASET_URI="${DATASET_URI}" \
    -e ALIYUN_DLC_RESOURCE_ID_4090="${ALIYUN_DLC_RESOURCE_ID_4090:-}" \
    quictrain-api-1 \
    python /tmp/verify_oss_mirror_train_feasibility.py \
      --dataset-uri "${DATASET_URI}" \
      --train-health-url http://127.0.0.1:8000 \
      --skip-studio-boundary \
      --skip-oss-probe \
      --json-out /tmp/studio_oss_mirror_feasibility.json \
      "${CANARY_ARGS[@]}"
fi
RC=$?
set -e
docker cp quictrain-api-1:/tmp/studio_oss_mirror_feasibility.json "${REPORT_DIR}/report.json" 2>/dev/null || true
python3 - <<PY
import json, pathlib
report_path = pathlib.Path("${REPORT_DIR}/report.json")
host_path = pathlib.Path("${HOST_CHECKS_JSON}")
if report_path.exists() and host_path.exists():
    report = json.loads(report_path.read_text())
    host = json.loads(host_path.read_text())
    for name, payload in host.items():
        if not payload:
            continue
        report.setdefault("checks", []).append({"name": name, **payload})
    report["ok"] = all(item.get("ok") for item in report.get("checks", []))
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["ok"] else 1)
raise SystemExit(${RC})
PY
