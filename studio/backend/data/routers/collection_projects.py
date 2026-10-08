"""Workspace-scoped collection project management API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from data.database import get_db
from data.models.collection_core import CollectionProject
from data.security.audit import emit_audit_event
from data.services.collection_access import require_collection_workspace
from data.services.collection_projects import (
    ArchivedCollectionProjectError,
)
from data.services.collection_projects import (
    archive_collection_project as archive_collection_project_record,
)
from data.services.collection_projects import (
    create_collection_project as create_collection_project_record,
)
from data.services.collection_projects import (
    update_collection_project as update_collection_project_record,
)
from data.services.resource_names import is_constraint_conflict
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/collection-projects", tags=["采集项目"])

_PROJECT_NAME_CONSTRAINT = "uq_collection_projects_workspace_normalized_name"
_PROJECT_STATUSES = {"enabled", "archived"}


class CollectionProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    owner_user_id: int | None = Field(default=None, gt=0)


class CollectionProjectUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    owner_user_id: int | None = Field(default=None, gt=0)


class CollectionProjectArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub"))
    except (TypeError, ValueError):
        return None


def _project_item(project: CollectionProject) -> dict[str, object]:
    return {
        "id": project.id,
        "workspace_id": project.workspace_id,
        "name": project.name,
        "description": project.description,
        "owner_user_id": project.owner_user_id,
        "status": project.status,
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
    }


def _require_workspace(db: Session, *, user: dict, workspace_id: int) -> None:
    try:
        require_collection_workspace(
            db,
            actor_id=_actor_id(user),
            workspace_id=workspace_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _raise_integrity_error(exc: IntegrityError) -> None:
    if is_constraint_conflict(exc, _PROJECT_NAME_CONSTRAINT):
        raise HTTPException(
            status_code=409,
            detail="collection project name already exists in this workspace",
        ) from exc
    raise exc


@router.get("")
def list_collection_projects(
    workspace_id: int = Query(..., gt=0),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:read")
    _require_workspace(db, user=user, workspace_id=workspace_id)
    if status is not None and status not in _PROJECT_STATUSES:
        raise HTTPException(status_code=422, detail=f"unsupported project status: {status}")
    query = db.query(CollectionProject).filter(CollectionProject.workspace_id == workspace_id)
    if status is not None:
        query = query.filter(CollectionProject.status == status)
    projects = query.order_by(
        CollectionProject.created_at.desc(),
        CollectionProject.id.desc(),
    ).all()
    return success({"items": [_project_item(project) for project in projects]})


@router.post("")
def create_collection_project(
    body: CollectionProjectCreateRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    try:
        project = create_collection_project_record(
            db,
            workspace_id=body.workspace_id,
            name=body.name,
            description=body.description,
            owner_user_id=body.owner_user_id,
        )
        db.commit()
        db.refresh(project)
    except IntegrityError as exc:
        db.rollback()
        _raise_integrity_error(exc)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    emit_audit_event(
        "collection.project.create",
        actor=str(user.get("email") or ""),
        resource=f"collection_project:{project.id}",
        detail={"workspace_id": body.workspace_id},
    )
    return success(_project_item(project))


@router.patch("/{project_id}")
def update_collection_project(
    project_id: int,
    body: CollectionProjectUpdateRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    changes = body.model_dump(exclude={"workspace_id"}, exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="at least one project field is required")
    try:
        project = update_collection_project_record(
            db,
            project_id=project_id,
            workspace_id=body.workspace_id,
            changes=changes,
        )
        db.commit()
        db.refresh(project)
    except IntegrityError as exc:
        db.rollback()
        _raise_integrity_error(exc)
    except ArchivedCollectionProjectError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return success(_project_item(project))


@router.post("/{project_id}/archive")
def archive_collection_project(
    project_id: int,
    body: CollectionProjectArchiveRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    try:
        project = archive_collection_project_record(
            db,
            project_id=project_id,
            workspace_id=body.workspace_id,
        )
        db.commit()
        db.refresh(project)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    emit_audit_event(
        "collection.project.archive",
        actor=str(user.get("email") or ""),
        resource=f"collection_project:{project.id}",
        detail={"workspace_id": body.workspace_id},
    )
    return success(_project_item(project))
