from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from data.config import get_public_runtime_config, update_runtime_config
from data.schemas.common import ConfigUpdate
from data.security.audit import emit_audit_event
from data.utils.helpers import get_current_user, require_permission, success
from data.utils.storage_paths import resolve_file_for_serve

router = APIRouter(prefix="/config", tags=["平台配置"])

_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
}


def _media_type_for(path) -> str | None:
    return _MEDIA_TYPES.get(path.suffix.lower())


@router.get("")
def get_config(user: dict = Depends(get_current_user)):
    require_permission(user, "workspace:read")
    emit_audit_event("config.read", actor=user.get("email"))
    return success(get_public_runtime_config())


@router.put("")
def update_config(body: ConfigUpdate, user: dict = Depends(get_current_user)):
    require_permission(user, "*")
    try:
        updated = update_runtime_config(body.section, body.data)
    except ValueError as e:
        emit_audit_event(
            "config.write_denied",
            actor=user.get("email"),
            resource=body.section,
            detail={"keys": sorted((body.data or {}).keys())},
            level="warning",
        )
        return {"code": 400, "message": str(e), "data": None}
    emit_audit_event(
        "config.write",
        actor=user.get("email"),
        resource=body.section,
        detail={"keys": sorted((body.data or {}).keys())},
        level="warning",
    )
    return success(updated)


files_router = APIRouter(prefix="/files", tags=["文件"])


@files_router.get("/preview")
def preview_file(path: str = Query(...), user: dict = Depends(get_current_user)):
    require_permission(user, "qrdf:read")
    p = resolve_file_for_serve(path)
    if not p:
        return {"code": 404, "message": "文件不存在", "data": None}
    emit_audit_event(
        "file.preview",
        actor=user.get("email"),
        resource=p.name,
    )
    return FileResponse(p, media_type=_media_type_for(p), filename=p.name)


@files_router.get("/download")
def download_file(path: str = Query(...), user: dict = Depends(get_current_user)):
    """Generic path download: only relative paths within storage_root are permitted (dedicated resource download routes preferred starting from P1)."""
    require_permission(user, "export:read")
    p = resolve_file_for_serve(path)
    if not p:
        return {"code": 404, "message": "文件不存在", "data": None}
    emit_audit_event(
        "file.download",
        actor=user.get("email"),
        resource=p.name,
        detail={"via": "files/download"},
    )
    return FileResponse(p, filename=p.name)
