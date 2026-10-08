"""Workspace, membership, and TaskSet endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from data.database import (
    Batch,
    ExternalProjectRef,
    TaskSet,
    User,
    Workspace,
    WorkspaceMember,
    get_db,
)
from data.realtime.socketio import schedule_realtime_workspace_revocation
from data.security.audit import emit_audit_event
from data.services.resource_names import (
    TASK_SET_NAME_CONFLICT,
    WORKSPACE_NAME_CONFLICT,
    conflict_detail,
    is_constraint_conflict,
    normalized_key,
    normalized_name,
)
from data.services.workspace_access import (
    accessible_workspace_ids,
    require_actor,
    require_workspace_actor,
)
from data.utils.formatting import format_api_datetime
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/workspace", tags=["工作空间"])


class WorkspaceCreateRequest(BaseModel):
    workspace_name: str = Field(min_length=1, max_length=128)
    desc: str = Field(default="", max_length=5000)


class TaskSetCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=5000)
    scene: str = Field(default="", max_length=64)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("task set name is required")
        return value


class WorkspaceMemberGrantRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    user_id: int = Field(gt=0)


def _actor_id(user: dict) -> int | None:
    raw = user.get("sub") or user.get("id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _workspace_or_404(db: Session, workspace_id: int) -> Workspace:
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace does not exist")
    return workspace


def _authorize_workspace(db: Session, user: dict, workspace_id: int) -> None:
    try:
        require_workspace_actor(db, actor_id=_actor_id(user), workspace_id=workspace_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _member_item(member: WorkspaceMember, target: User) -> dict[str, object]:
    return {
        "workspace_id": member.workspace_id,
        "user_id": target.id,
        "email": target.email,
        "role": target.role,
    }


def _external_project_item(external_project: ExternalProjectRef | None) -> dict[str, object] | None:
    if external_project is None:
        return None
    return {
        "provider": external_project.provider,
        "external_project_id": external_project.external_project_id,
        "display_name": external_project.display_name,
        "status": external_project.status,
        "synced_at": format_api_datetime(external_project.synced_at),
    }


def _task_set_item(task_set: TaskSet) -> dict[str, object]:
    return {
        "id": task_set.id,
        "workspace_id": task_set.workspace_id,
        "name": task_set.name,
        "description": task_set.description,
        "scene": task_set.scene,
        "external_project": _external_project_item(task_set.external_project),
    }


@router.post("/create")
def create_workspace(
    body: WorkspaceCreateRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    try:
        actor = require_actor(db, actor_id=_actor_id(user))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    name = normalized_name(body.workspace_name)
    existing = (
        db.query(Workspace.id)
        .filter(func.lower(func.btrim(Workspace.name)) == normalized_key(name))
        .first()
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail=conflict_detail(WORKSPACE_NAME_CONFLICT))
    workspace = Workspace(name=name, description=body.desc.strip(), creator=actor.email)
    db.add(workspace)
    try:
        db.flush()
        db.add(WorkspaceMember(workspace_id=workspace.id, user_id=actor.id))
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if is_constraint_conflict(exc, "uq_workspaces_normalized_name"):
            raise HTTPException(
                status_code=409,
                detail=conflict_detail(WORKSPACE_NAME_CONFLICT),
            ) from exc
        raise
    db.refresh(workspace)
    emit_audit_event(
        "workspace.member.grant",
        actor=user.get("email"),
        resource=f"workspace:{workspace.id}",
        detail={"workspace_id": workspace.id, "user_id": actor.id},
    )
    return success(
        {
            "id": workspace.id,
            "name": workspace.name,
            "description": workspace.description,
            "creator": workspace.creator,
        }
    )


@router.get("/page")
def page_workspace(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    keyword: str = Query("", max_length=128),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:read")
    workspace_ids = accessible_workspace_ids(db, actor_id=_actor_id(user))
    query = db.query(Workspace)
    if workspace_ids is not None:
        query = query.filter(Workspace.id.in_(workspace_ids))
    if keyword:
        query = query.filter(Workspace.name.contains(keyword))
    total = query.count()
    workspaces = (
        query.order_by(Workspace.created_at.desc(), Workspace.id.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    return success(
        {
            "list": [
                {
                    "id": workspace.id,
                    "name": workspace.name,
                    "description": workspace.description,
                    "creator": workspace.creator,
                    "task_set_count": db.query(TaskSet)
                    .filter(TaskSet.workspace_id == workspace.id)
                    .count(),
                    "batch_count": db.query(Batch)
                    .filter(Batch.workspace_id == workspace.id)
                    .count(),
                    "created_at": format_api_datetime(workspace.created_at),
                }
                for workspace in workspaces
            ],
            "total": total,
            "page": page,
            "size": size,
        }
    )


@router.get("/options")
def workspace_options(
    scope: Literal["member", "management"] = Query("member"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:read")
    actor = require_actor(db, actor_id=_actor_id(user))
    if scope == "management" and actor.role != "admin":
        raise HTTPException(status_code=403, detail="admin is required for workspace management")
    query = db.query(Workspace)
    if scope != "management":
        query = query.join(WorkspaceMember).filter(WorkspaceMember.user_id == actor.id)
    rows = query.order_by(Workspace.created_at.desc(), Workspace.id.desc()).all()
    member_ids = {
        workspace_id
        for (workspace_id,) in db.query(WorkspaceMember.workspace_id)
        .filter(WorkspaceMember.user_id == _actor_id(user))
        .all()
    }
    actor = db.get(User, _actor_id(user))
    return success(
        {
            "list": [
                {
                    "id": row.id,
                    "name": row.name,
                    "description": row.description,
                    "created_at": format_api_datetime(row.created_at),
                    "collection_access": bool(
                        actor and actor.role == "admin" and row.id in member_ids
                    ),
                }
                for row in rows
            ]
        }
    )


def create_task_set(
    body: TaskSetCreateRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    _authorize_workspace(db, user, body.workspace_id)
    name = normalized_name(body.name)
    existing = (
        db.query(TaskSet.id)
        .filter(
            TaskSet.workspace_id == body.workspace_id,
            func.lower(func.btrim(TaskSet.name)) == normalized_key(name),
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail=conflict_detail(TASK_SET_NAME_CONFLICT))
    task_set = TaskSet(
        workspace_id=body.workspace_id,
        name=name,
        description=body.description.strip(),
        scene=body.scene.strip(),
    )
    db.add(task_set)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if is_constraint_conflict(exc, "uq_task_sets_workspace_normalized_name"):
            raise HTTPException(
                status_code=409,
                detail=conflict_detail(TASK_SET_NAME_CONFLICT),
            ) from exc
        raise
    db.refresh(task_set)
    return success(_task_set_item(task_set))


def list_task_sets(
    workspace_id: int = Query(..., gt=0),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    keyword: str = Query("", max_length=128),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:read")
    _authorize_workspace(db, user, workspace_id)
    query = db.query(TaskSet).filter(TaskSet.workspace_id == workspace_id)
    if keyword:
        query = query.filter(TaskSet.name.contains(keyword))
    total = query.count()
    rows = (
        query.order_by(TaskSet.created_at.desc(), TaskSet.id.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    return success(
        {
            "list": [
                {
                    **_task_set_item(task_set),
                    "batch_count": db.query(Batch).filter(Batch.task_set_id == task_set.id).count(),
                    "created_at": format_api_datetime(task_set.created_at),
                }
                for task_set in rows
            ],
            "total": total,
            "page": page,
            "size": size,
        }
    )


def task_set_options(
    workspace_id: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:read")
    workspace_ids = accessible_workspace_ids(db, actor_id=_actor_id(user))
    query = db.query(TaskSet)
    if workspace_ids is not None:
        query = query.filter(TaskSet.workspace_id.in_(workspace_ids))
    if workspace_id is not None:
        _authorize_workspace(db, user, workspace_id)
        query = query.filter(TaskSet.workspace_id == workspace_id)
    rows = query.order_by(TaskSet.created_at.desc(), TaskSet.id.desc()).all()
    return success(
        {
            "list": [
                {
                    **_task_set_item(task_set),
                    "created_at": format_api_datetime(task_set.created_at),
                }
                for task_set in rows
            ]
        }
    )


def _require_membership_admin(db: Session, user: dict, workspace_id: int) -> Workspace:
    require_permission(user, "workspace:write")
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="admin is required for workspace membership")
    return _workspace_or_404(db, workspace_id)


@router.get("/{workspace_id}/members")
def list_workspace_members(
    workspace_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_membership_admin(db, user, workspace_id)
    rows = (
        db.query(WorkspaceMember, User)
        .join(User, User.id == WorkspaceMember.user_id)
        .filter(WorkspaceMember.workspace_id == workspace_id)
        .order_by(WorkspaceMember.id)
        .all()
    )
    return success({"list": [_member_item(member, target) for member, target in rows]})


@router.post("/{workspace_id}/members")
def grant_workspace_member(
    workspace_id: int,
    body: WorkspaceMemberGrantRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_membership_admin(db, user, workspace_id)
    target = db.get(User, body.user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user does not exist")
    member = (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == target.id)
        .one_or_none()
    )
    if member is None:
        member = WorkspaceMember(workspace_id=workspace_id, user_id=target.id)
        db.add(member)
    db.commit()
    db.refresh(member)
    emit_audit_event(
        "workspace.member.grant",
        actor=user.get("email"),
        resource=f"workspace:{workspace_id}",
        detail={"workspace_id": workspace_id, "user_id": target.id},
    )
    return success(_member_item(member, target))


@router.delete("/{workspace_id}/members/{target_user_id}")
def revoke_workspace_member(
    workspace_id: int,
    target_user_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_membership_admin(db, user, workspace_id)
    member = (
        db.query(WorkspaceMember)
        .filter(
            WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == target_user_id
        )
        .one_or_none()
    )
    if member is None:
        raise HTTPException(status_code=404, detail="workspace membership does not exist")
    db.delete(member)
    db.commit()
    emit_audit_event(
        "workspace.member.revoke",
        actor=user.get("email"),
        resource=f"workspace:{workspace_id}",
        detail={"workspace_id": workspace_id, "user_id": target_user_id},
    )
    schedule_realtime_workspace_revocation(target_user_id, workspace_id)
    return success({"workspace_id": workspace_id, "user_id": target_user_id})
