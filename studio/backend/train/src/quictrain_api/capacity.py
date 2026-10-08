"""Live capacity discovery with honest unknowns and DLC probe reservation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from quictrain_api.settings import get_settings


@dataclass
class CapacityPool:
    id: str
    name: str
    gpu_model: str
    available: int | None
    total: int | None
    source: str
    status: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "gpu_model": self.gpu_model,
            "available": self.available,
            "total": self.total,
            "source": self.source,
            "status": self.status,
            "enabled": self.status != "disabled",
        }


class CapacityProbe(Protocol):
    def discover(self) -> list[CapacityPool]: ...


class HonestUnknownProbe:
    def discover(self) -> list[CapacityPool]:
        return []


class StaticConfiguredProbe:
    """Only reports totals from env — never invents free GPU counts."""

    def discover(self) -> list[CapacityPool]:
        settings = get_settings()
        if settings.capacity_gpu_model and settings.capacity_total_gpus is not None:
            return [
                CapacityPool(
                    id="configured-quota",
                    name="Configured DLC quota",
                    gpu_model=settings.capacity_gpu_model,
                    available=None,  # free count unknown without live probe
                    total=settings.capacity_total_gpus,
                    source="env_config",
                    status="unknown_availability",
                )
            ]
        return []


class AliyunDlcCapacityProbe:
    """Reserved live probe — requires PAI SDK + RAM Role on ECS."""

    def discover(self) -> list[CapacityPool]:
        settings = get_settings()
        try:
            # Import boundary: optional cloud dependency.
            import quictrain_provider_aliyun_dlc  # noqa: F401
        except ImportError:
            return [
                CapacityPool(
                    id="dlc-reserved",
                    name="PAI-DLC (probe reserved)",
                    gpu_model=settings.capacity_gpu_model or "H20",
                    available=None,
                    total=settings.capacity_total_gpus,
                    source="aliyun_dlc_reserved",
                    status="RESERVED_PENDING_SDK",
                )
            ]
        # Live ListQuotas / GetQuota is environment-specific; keep reserved until wired.
        return [
            CapacityPool(
                id="dlc-reserved",
                name="PAI-DLC (live probe not yet wired)",
                gpu_model=settings.capacity_gpu_model or "H20",
                available=None,
                total=settings.capacity_total_gpus,
                source="aliyun_dlc_reserved",
                status="RESERVED_PENDING_LIVE_API",
            )
        ]


def build_capacity_probe() -> CapacityProbe:
    settings = get_settings()
    mode = settings.capacity_discovery_mode
    if mode == "static_config":
        return StaticConfiguredProbe()
    if mode == "aliyun_dlc":
        return AliyunDlcCapacityProbe()
    return HonestUnknownProbe()


def capacity_snapshot() -> dict[str, Any]:
    settings = get_settings()
    pools = [pool.as_dict() for pool in build_capacity_probe().discover()]
    # Never invent free GPU numbers.
    for pool in pools:
        if pool.get("available") is not None and pool.get("source") == "static_fake":
            pool["available"] = None
            pool["status"] = "rejected_static_fake"
    message = {
        "honest_unknown": "容量需通过 Provider 活体发现；当前未配置静态假 GPU。",
        "static_config": "仅展示配置总量；available 故意为空直至活体探针。",
        "aliyun_dlc": "DLC 活体探针预留；未接线前 available 为空。",
    }.get(settings.capacity_discovery_mode, "capacity discovery pending")
    return {
        "mode": settings.capacity_discovery_mode,
        "provider": settings.provider,
        "provider_disabled": settings.provider_disabled,
        "pools": pools,
        "message": message,
    }
