"""DSW ops helpers: known-instance catalog + lifecycle actions."""

from __future__ import annotations

from typing import Any

from quictrain_api.errors import ServiceError
from quictrain_api.settings import get_settings


def parse_known_dsw_instances(raw: str | None = None) -> list[dict[str, str]]:
    settings = get_settings()
    text = raw if raw is not None else settings.dsw_known_instances
    out: list[dict[str, str]] = []
    for part in (text or "").split(","):
        item = part.strip()
        if not item:
            continue
        bits = item.split(":")
        if len(bits) < 1 or not bits[0].strip():
            continue
        instance_id = bits[0].strip()
        name = bits[1].strip() if len(bits) > 1 else instance_id
        family = bits[2].strip().lower() if len(bits) > 2 else "unknown"
        out.append({"instance_id": instance_id, "name": name, "family": family})
    return out


def get_dsw_client():
    import os

    from quictrain_provider_aliyun_dlc import AliyunDSWSettings, create_dsw_sdk_client

    region = os.environ.get("ALIYUN_REGION_ID", "cn-beijing")
    workspace = os.environ.get("ALIYUN_DLC_WORKSPACE_ID", "420135")
    return create_dsw_sdk_client(
        AliyunDSWSettings(
            region_id=region,
            workspace_id=workspace,
            endpoint=os.environ.get("ALIYUN_DSW_ENDPOINT"),
        )
    )


def dsw_mode_status() -> dict[str, Any]:
    settings = get_settings()
    return {
        "preferred_training_provider": "aliyun_dlc",
        "interactive_provider": "aliyun_dsw",
        "note": (
            "训练任务走 PAI-DLC（CreateJob + quota* ResourceId）。"
            "DSW SDK 用于实例启停与 Notebook/Terminal 访问；Running 的 DSW 仍占用同配额 GPU。"
            "H20 DSW / H20-5 资源组默认禁止关闭。"
        ),
        "known_instances": parse_known_dsw_instances(),
        "dedicated_dsw_instance_id": settings.dedicated_dsw_instance_id,
        "dedicated_dsw_name": settings.dedicated_dsw_name,
        "protect_h20_dsw": settings.protect_h20_dsw,
        "protect_h20_resource_groups": settings.protect_h20_resource_groups,
        "dlc_quotas": {
            "4090": "quota16ktslmgp8r",
            "h20": "quota1lnd98qodrh",
        },
    }


def list_dsw_instances(*, family: str | None = None) -> dict[str, Any]:
    known = parse_known_dsw_instances()
    if family:
        known = [item for item in known if item["family"] == family.lower()]
    client = get_dsw_client()
    instances: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for item in known:
        try:
            inst = client.get_instance(item["instance_id"])
            payload = inst.to_public_dict()
            payload["family"] = item["family"]
            payload["catalog_name"] = item["name"]
            instances.append(payload)
        except Exception as exc:  # noqa: BLE001
            errors.append({"instance_id": item["instance_id"], "error": str(exc)})
    listed: list[dict[str, Any]] = []
    try:
        import os

        workspace = os.environ.get("ALIYUN_DLC_WORKSPACE_ID", "420135")
        for inst in client.list_instances(workspace_id=workspace):
            listed.append(inst.to_public_dict())
    except Exception as exc:  # noqa: BLE001
        errors.append({"instance_id": "*", "error": f"list_instances: {exc}"})
    return {
        **dsw_mode_status(),
        "instances": instances,
        "workspace_list": listed,
        "errors": errors,
    }


def get_dsw_instance(instance_id: str) -> dict[str, Any]:
    client = get_dsw_client()
    try:
        inst = client.get_instance(instance_id)
    except Exception as exc:  # noqa: BLE001
        raise ServiceError("DSW_GET_FAILED", str(exc), status_code=502) from exc
    payload = inst.to_public_dict()
    for item in parse_known_dsw_instances():
        if item["instance_id"] == instance_id:
            payload["family"] = item["family"]
            payload["catalog_name"] = item["name"]
            break
    return payload


def start_dsw_instance(instance_id: str) -> dict[str, Any]:
    client = get_dsw_client()
    try:
        client.start_instance(instance_id)
    except Exception as exc:  # noqa: BLE001
        raise ServiceError("DSW_START_FAILED", str(exc), status_code=502) from exc
    return get_dsw_instance(instance_id)


def _dsw_protection_context(instance_id: str) -> dict[str, str | None]:
    known = {item["instance_id"]: item for item in parse_known_dsw_instances()}
    catalog = known.get(instance_id) or {}
    family = catalog.get("family")
    resource_id = None
    name = catalog.get("name")
    if family == "h20":
        resource_id = "quota1lnd98qodrh"
    else:
        try:
            payload = get_dsw_instance(instance_id)
            resource_id = payload.get("resource_id")
            if not family and resource_id == "quota1lnd98qodrh":
                family = "h20"
            name = name or payload.get("instance_name")
        except Exception:  # noqa: BLE001
            if instance_id in {"dsw-n1n8o1usk465z51gam", "dsw-c0me85cekg6m8prdf4"}:
                family = "h20"
                resource_id = "quota1lnd98qodrh"
    return {"family": family, "resource_id": resource_id, "name": name}


def stop_dsw_instance(instance_id: str, *, force: bool = False) -> dict[str, Any]:
    """Stop a DSW instance with H20 hard-protection.

    H20 DSW / H20-5 quota instances are refused unless both
    ``QUICTRAIN_ALLOW_H20_DSW_STOP=true`` and ``force=True``.
    """

    settings = get_settings()
    ctx = _dsw_protection_context(instance_id)
    family = (ctx.get("family") or "").lower()
    resource_id = ctx.get("resource_id") or ""
    protected_families = {
        item.strip().lower()
        for item in settings.dsw_stop_protected_families.split(",")
        if item.strip()
    }
    protected_quotas = {
        item.strip() for item in settings.dsw_stop_protected_resource_ids.split(",") if item.strip()
    }
    is_h20 = family in protected_families or resource_id in protected_quotas
    if settings.protect_h20_dsw and is_h20:
        if not (settings.allow_h20_dsw_stop and force):
            raise ServiceError(
                "DSW_STOP_PROTECTED",
                (
                    f"拒绝停止 H20 DSW `{ctx.get('name') or instance_id}` "
                    f"（family={family or 'h20'}, resource={resource_id or 'quota1lnd98qodrh'}）。"
                    "H20 实例与 H20-5 资源组默认受保护；禁止直接关闭。"
                ),
                status_code=403,
                details={
                    "instance_id": instance_id,
                    "family": family or "h20",
                    "resource_id": resource_id or "quota1lnd98qodrh",
                    "protect_h20_dsw": True,
                    "protect_h20_resource_groups": settings.protect_h20_resource_groups,
                },
            )
    client = get_dsw_client()
    try:
        client.stop_instance(instance_id)
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if any(token in message for token in ("already", "Stopped", "Stopping")):
            return get_dsw_instance(instance_id)
        raise ServiceError("DSW_STOP_FAILED", message, status_code=502) from exc
    return get_dsw_instance(instance_id)


def verify_dsw_instances(instance_ids: list[str] | None = None) -> dict[str, Any]:
    """Probe GetInstance + access URLs for known (or given) DSW IDs."""

    targets = instance_ids or [item["instance_id"] for item in parse_known_dsw_instances()]
    results: list[dict[str, Any]] = []
    for instance_id in targets:
        try:
            payload = get_dsw_instance(instance_id)
            ok = bool(payload.get("instance_id")) and payload.get("status") not in {None, ""}
            access_ok = bool(
                payload.get("jupyterlab_url")
                or payload.get("webide_url")
                or payload.get("terminal_url")
            )
            results.append(
                {
                    "instance_id": instance_id,
                    "ok": ok and access_ok,
                    "status": payload.get("status"),
                    "resource_id": payload.get("resource_id"),
                    "gpu_count": payload.get("gpu_count"),
                    "access_urls_present": access_ok,
                    "instance": payload,
                }
            )
        except Exception as exc:  # noqa: BLE001
            results.append({"instance_id": instance_id, "ok": False, "error": str(exc)})
    return {
        "preferred_training_provider": "aliyun_dlc",
        "verified": all(item.get("ok") for item in results) if results else False,
        "results": results,
    }
