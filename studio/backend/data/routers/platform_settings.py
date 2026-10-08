"""Administrator-only runtime settings for cloud import and AI integration."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from data.config import settings
from data.database import get_db
from data.security.audit import emit_audit_event
from data.services.runtime_health import build_runtime_health_snapshot
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/platform-settings", tags=["平台设置"])


def _admin(user: dict) -> None:
    require_permission(user, "*")


@router.get("")
def get_platform_settings(
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _admin(user)
    emit_audit_event("platform_settings.read", actor=str(user.get("email") or ""))
    return success({})


@router.get("/runtime-health")
def get_runtime_health(
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _admin(user)
    return success(
        build_runtime_health_snapshot(
            db,
            storage_root=settings.storage_root,
        )
    )
