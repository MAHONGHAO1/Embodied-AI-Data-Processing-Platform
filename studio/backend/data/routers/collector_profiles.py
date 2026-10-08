"""Workspace-scoped collector directory for offline Episode attribution."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from data.database import PersonnelProfile, WorkspacePersonnelProfile, get_db
from data.security.audit import emit_audit_event
from data.services.collector_profiles import (
    CollectorProfileKeyConflict,
    CollectorProfileKeySpaceExhausted,
    add_workspace_membership,
    collector_profile_item,
    list_available_collector_profiles,
    remove_workspace_membership,
    require_collector_profile_admin,
)
from data.services.collector_profiles import (
    create_collector_profile as create_collector_profile_record,
)
from data.services.resource_names import normalized_name
from data.services.workspace_access import require_workspace_actor
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/collector-profiles", tags=["采集员目录"])


class CollectorProfileCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=128)
    profile_key: str | None = Field(default=None, pattern=r"^[1-9][0-9]*$")


class CollectorProfileMembershipGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    personnel_profile_id: int = Field(gt=0)


class CollectorProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    is_active: bool | None = None


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub"))
    except (TypeError, ValueError):
        return None


@router.get("")
def list_collector_profiles(
    workspace_id: int = Query(..., gt=0),
    include_inactive: bool = Query(default=False),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:read")
    try:
        require_workspace_actor(db, actor_id=_actor_id(user), workspace_id=workspace_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    query = (
        db.query(PersonnelProfile)
        .join(
            WorkspacePersonnelProfile,
            WorkspacePersonnelProfile.personnel_profile_id == PersonnelProfile.id,
        )
        .filter(WorkspacePersonnelProfile.workspace_id == workspace_id)
    )
    if not include_inactive:
        query = query.filter(PersonnelProfile.is_active.is_(True))
    rows = query.order_by(PersonnelProfile.name.asc(), PersonnelProfile.profile_key.asc()).all()
    return success({"items": [collector_profile_item(row) for row in rows]})


@router.get("/available")
def list_available_collectors(
    workspace_id: int = Query(..., gt=0),
    query: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:read")
    try:
        require_workspace_actor(db, actor_id=_actor_id(user), workspace_id=workspace_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    rows = list_available_collector_profiles(db, workspace_id=workspace_id, query=query)
    return success({"items": [collector_profile_item(row) for row in rows]})


@router.post("/memberships")
def grant_collector_membership(
    body: CollectorProfileMembershipGrantRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    try:
        require_workspace_actor(db, actor_id=_actor_id(user), workspace_id=body.workspace_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    profile = db.get(PersonnelProfile, body.personnel_profile_id)
    if profile is None or not profile.is_active:
        raise HTTPException(
            status_code=404, detail="collector profile does not exist or is inactive"
        )

    add_workspace_membership(db, workspace_id=body.workspace_id, profile_id=profile.id)
    db.commit()

    emit_audit_event(
        "collector_profile.membership.grant",
        actor=str(user.get("email") or ""),
        resource=f"collector_profile:{profile.id}",
        detail={"workspace_id": body.workspace_id, "profile_id": profile.id},
    )
    return success(collector_profile_item(profile))


@router.delete("/{profile_id}/memberships")
def revoke_collector_membership(
    profile_id: int,
    workspace_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    try:
        require_workspace_actor(db, actor_id=_actor_id(user), workspace_id=workspace_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    removed = remove_workspace_membership(db, workspace_id=workspace_id, profile_id=profile_id)
    if not removed:
        raise HTTPException(status_code=404, detail="collector is not a member of this workspace")
    db.commit()

    emit_audit_event(
        "collector_profile.membership.revoke",
        actor=str(user.get("email") or ""),
        resource=f"collector_profile:{profile_id}",
        detail={"workspace_id": workspace_id, "profile_id": profile_id},
    )
    return success({"workspace_id": workspace_id, "profile_id": profile_id})


@router.post("")
def create_collector_profile(
    body: CollectorProfileCreateRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    try:
        require_workspace_actor(db, actor_id=_actor_id(user), workspace_id=body.workspace_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="collector profile name is required")
    try:
        profile = create_collector_profile_record(
            db,
            workspace_id=body.workspace_id,
            name=name,
            profile_key=body.profile_key,
        )
        db.commit()
    except CollectorProfileKeyConflict as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="collector profile number already exists",
        ) from exc
    except CollectorProfileKeySpaceExhausted as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(profile)
    emit_audit_event(
        "collector_profile.create",
        actor=str(user.get("email") or ""),
        resource=f"collector_profile:{profile.id}",
        detail={"workspace_id": body.workspace_id},
    )
    return success(collector_profile_item(profile))


@router.patch("/{profile_id}")
def update_collector_profile(
    profile_id: int,
    body: CollectorProfileUpdateRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    try:
        require_collector_profile_admin(db, actor_id=_actor_id(user))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    profile = db.get(PersonnelProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="collector profile does not exist")
    if not body.model_fields_set:
        raise HTTPException(status_code=422, detail="at least one collector field is required")
    try:
        if "name" in body.model_fields_set and body.name is not None:
            profile.name = normalized_name(body.name)
        if "is_active" in body.model_fields_set and body.is_active is not None:
            profile.is_active = body.is_active
        db.commit()
        db.refresh(profile)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    emit_audit_event(
        "collector_profile.update",
        actor=str(user.get("email") or ""),
        resource=f"collector_profile:{profile.id}",
        detail={"scope": "global", "is_active": profile.is_active},
    )
    return success(collector_profile_item(profile))
