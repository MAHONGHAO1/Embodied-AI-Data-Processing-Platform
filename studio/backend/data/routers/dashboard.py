"""Tenant-scoped dashboard overview API assembled from current Episode facts."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from data.database import get_db
from data.security.audit import emit_audit_event
from data.services.dashboard_scope import DashboardScope, resolve_dashboard_scope
from data.services.dashboard_warehouse import (
    read_dashboard_overview,
    request_dashboard_refresh,
)
from data.utils.helpers import get_current_user, success

router = APIRouter(prefix="/dashboard", tags=["数据看板"])


class DashboardScopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int | None = Field(default=None, gt=0)
    task_set_id: int | None = Field(default=None, gt=0)


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub"))
    except (TypeError, ValueError):
        return None


def _authorized_scope(
    db: Session,
    *,
    user: dict,
    permission: str,
    workspace_id: int | None,
    task_set_id: int | None,
) -> DashboardScope:
    if workspace_id is None and task_set_id is not None:
        raise HTTPException(status_code=422, detail="workspace_id is required")
    try:
        return resolve_dashboard_scope(
            db,
            user=user,
            permission=permission,
            workspace_id=workspace_id,
            task_set_id=task_set_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="dashboard scope access denied") from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="dashboard scope access denied") from exc


@router.get("/overview")
def dashboard_overview(
    workspace_id: int | None = Query(default=None, gt=0),
    task_set_id: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    scope = _authorized_scope(
        db,
        user=user,
        permission="dashboard:read",
        workspace_id=workspace_id,
        task_set_id=task_set_id,
    )
    payload = read_dashboard_overview(db, scope=scope)
    emit_audit_event(
        "dashboard.overview.read",
        actor=str(user.get("email") or ""),
        resource=scope.key,
    )
    return success(payload)


@router.post("/refresh")
def dashboard_refresh(
    body: DashboardScopeRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    scope = _authorized_scope(
        db,
        user=user,
        permission="dashboard:refresh",
        workspace_id=body.workspace_id,
        task_set_id=body.task_set_id,
    )
    current = read_dashboard_overview(db, scope=scope)
    baseline_computed_at = current.get("computed_at") if current else None
    job, payload = request_dashboard_refresh(db, scope=scope, actor_id=_actor_id(user))
    emit_audit_event(
        "dashboard.refresh.enqueued",
        actor=str(user.get("email") or ""),
        resource=scope.key,
        detail={"job_id": job.id, "status": job.status, "scope_key": scope.key},
    )
    return success(
        {
            "job_id": job.id,
            "status": job.status,
            "scope_key": scope.key,
            "baseline_computed_at": baseline_computed_at,
            "payload": payload,
        }
    )
