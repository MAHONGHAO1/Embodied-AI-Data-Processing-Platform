"""Compute-pool → cloud ResourceId / capacity mapping for hybrid DLC submission."""

from __future__ import annotations

import os
from typing import Any

from .settings import get_settings

# Built-in defaults for the co-located Beijing workspace. Env QUICTRAIN_POOL_RESOURCE_IDS
# / QUICTRAIN_POOL_CAPACITIES override or extend these (comma-separated pool:value pairs).
#
# Production (2026-09): training jobs prefer PAI-DLC CreateJob with quota* ResourceId.
# DSW instances (dsw-*) share the same quotas and are managed via DSW SDK for
# interactive access / verify / stop — never pass dsw-* as CreateJob ResourceId.
_DEFAULT_POOL_RESOURCE_IDS = {
    # Workspace 420135 — Quota4090_48G (8×RTX 4090 48G). Not DSW instance IDs.
    "cn-beijing-4090": "quota87wrnuka7bx",
    # H20-5 / GU8T — same quota as DSW quic-train-5000 / dsw-n1n8o1usk465z51gam.
    "cn-beijing-h20": "quota1lnd98qodrh",
    "cn-beijing-h20-8": "quota1lnd98qodrh",
}
_DEFAULT_POOL_CAPACITIES = {
    # Quota4090_48G capacity (shared with DSW Nv48g8094 when Running).
    "cn-beijing-4090": 8,
    "cn-beijing-h20": 40,
    "cn-beijing-h20-8": 40,
    "local-sim": 0,
}
_DEFAULT_POOL_REGIONS = {
    "cn-beijing-4090": "cn-beijing",
    "cn-beijing-h20": "cn-beijing",
    "cn-beijing-h20-8": "cn-beijing",
    "local-sim": "local",
}


def _parse_kv_map(raw: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not raw:
        return out
    for part in raw.split(","):
        item = part.strip()
        if not item or ":" not in item:
            continue
        key, value = item.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            out[key] = value
    return out


def pool_resource_id_map() -> dict[str, str]:
    settings = get_settings()
    merged = dict(_DEFAULT_POOL_RESOURCE_IDS)
    merged.update(_parse_kv_map(settings.pool_resource_ids))
    # Legacy env only fills cn-beijing-h20 when operators have not bound the
    # dedicated H20 resource group (or an explicit pool map entry).
    if "cn-beijing-h20" not in merged:
        h20 = os.environ.get("ALIYUN_DLC_RESOURCE_ID") or settings.default_dlc_resource_id
        if h20:
            merged["cn-beijing-h20"] = h20
    return merged


def pool_capacity_map() -> dict[str, int]:
    settings = get_settings()
    merged = dict(_DEFAULT_POOL_CAPACITIES)
    for key, value in _parse_kv_map(settings.pool_capacities).items():
        try:
            merged[key] = int(value)
        except ValueError:
            continue
    return merged


def resolve_pool_resource_id(pool_id: str | None) -> str | None:
    if not pool_id:
        return None
    return pool_resource_id_map().get(pool_id)


def resolve_pool_capacity(pool_id: str | None) -> int | None:
    if not pool_id:
        return None
    return pool_capacity_map().get(pool_id)


def resolve_pool_region(pool_id: str | None) -> str | None:
    if not pool_id:
        return None
    overrides = _parse_kv_map(get_settings().pool_regions)
    if pool_id in overrides:
        return overrides[pool_id]
    return _DEFAULT_POOL_REGIONS.get(pool_id)


def scale_worker_resources(
    gpu_count: int,
    *,
    gpu_model: str | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    """Per-GPU CPU/memory for PAI-DLC ResourceConfig completeness.

    Quota4090 previously used 32Gi/card; π0.5 died at \"Creating policy\" with
    LeRobot exit -9 (SIGKILL / cgroup OOM) under that limit. Align 4090 with the
    H20 baseline (64Gi/card) and give π0.5 extra host RAM for weight load.
    """

    count = max(1, int(gpu_count or 1))
    _ = gpu_model  # retained for call-site compatibility / future SKU tuning
    mid = (model_id or "").lower()
    # Dedicated quotas historically used 8 CPU + 64Gi per card (H20 acceptance).
    cpu_per = 8
    mem_per_gi = 64
    if mid == "pi05":
        # ~14GB safetensors + PyTorch/policy construction peaks well above weights.
        mem_per_gi = 128
    shared_gi = min(64, max(16, mem_per_gi // 4))
    return {
        "cpu": cpu_per * count,
        "memory": f"{mem_per_gi * count}Gi",
        "shared_memory": f"{shared_gi * count}Gi",
        "worker_count": 1,
        "gpu_count": count,
    }


def match_resource_profile(
    *,
    profile_id: str,
    provider_id: str,
    pool_id: str | None,
    gpu_count: int,
    selectable: bool,
    gpu_models: list[str] | None = None,
) -> dict[str, Any]:
    """Validate profile ↔ pool ↔ ResourceId binding for UI / pre-submit checks."""

    region = resolve_pool_region(pool_id)
    resource_id = resolve_pool_resource_id(pool_id)
    capacity = resolve_pool_capacity(pool_id)
    issues: list[str] = []
    if not selectable:
        issues.append("Profile 未开放（selectable=false）。")
    if provider_id == "fake" or (gpu_models and "LOCAL" in gpu_models):
        return {
            "profile_id": profile_id,
            "ok": True if selectable else False,
            "status": "LOCAL_OK" if selectable else "NOT_SELECTABLE",
            "provider_id": provider_id,
            "pool_id": pool_id,
            "resource_id": None,
            "region": "local",
            "gpu_count": gpu_count,
            "pool_capacity_gpus": capacity,
            "issues": issues,
        }
    if not pool_id:
        issues.append("缺少 pool_id，无法定位云端资源组。")
    if not resource_id:
        issues.append(
            f"池 {pool_id or '—'} 未绑定 ResourceId（设置 QUICTRAIN_POOL_RESOURCE_IDS）。"
        )
    if capacity is not None and gpu_count > capacity:
        issues.append(f"请求 {gpu_count} GPU 超过池容量 {capacity}。")
    ok = not issues
    return {
        "profile_id": profile_id,
        "ok": ok,
        "status": "MATCHED" if ok else "MISMATCH",
        "provider_id": provider_id,
        "pool_id": pool_id,
        "resource_id": resource_id,
        "region": region,
        "gpu_count": gpu_count,
        "pool_capacity_gpus": capacity,
        "issues": issues,
    }
