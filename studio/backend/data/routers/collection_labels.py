"""Platform collection label dictionary API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from data.database import get_db
from data.models.collection_config import COLLECTION_LABEL_CATEGORIES, CollectionLabel
from data.security.audit import emit_audit_event
from data.services.collection_access import require_collection_workspace
from data.services.collection_labels import (
    create_collection_label as create_collection_label_record,
)
from data.services.collection_labels import (
    deactivate_collection_label as deactivate_collection_label_record,
)
from data.services.resource_names import is_constraint_conflict
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/collection-labels", tags=["采集标签字典"])

_LABEL_NAME_CONSTRAINT = "uq_collection_labels_category_normalized_name"


class CollectionLabelCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    category: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=128)
    description: str = ""


class CollectionLabelDeactivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub"))
    except (TypeError, ValueError):
        return None


def _label_item(label: CollectionLabel) -> dict[str, object]:
    return {
        "id": label.id,
        "category": label.category,
        "name": label.name,
        "description": label.description,
        "is_active": label.is_active,
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


@router.get("")
def list_collection_labels(
    workspace_id: int = Query(..., gt=0),
    category: str | None = Query(default=None),
    include_inactive: bool = Query(default=False),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:read")
    _require_workspace(db, user=user, workspace_id=workspace_id)
    if category is not None and category not in COLLECTION_LABEL_CATEGORIES:
        raise HTTPException(status_code=422, detail=f"unsupported label category: {category}")
    query = db.query(CollectionLabel)
    if category is not None:
        query = query.filter(CollectionLabel.category == category)
    if not include_inactive:
        query = query.filter(CollectionLabel.is_active.is_(True))
    labels = query.order_by(
        CollectionLabel.category.asc(),
        CollectionLabel.name.asc(),
        CollectionLabel.id.asc(),
    ).all()
    return success({"items": [_label_item(label) for label in labels]})


@router.post("")
def create_collection_label(
    body: CollectionLabelCreateRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    try:
        label = create_collection_label_record(
            db,
            category=body.category,
            name=body.name,
            description=body.description,
        )
        db.commit()
        db.refresh(label)
    except IntegrityError as exc:
        db.rollback()
        if is_constraint_conflict(exc, _LABEL_NAME_CONSTRAINT):
            raise HTTPException(
                status_code=409,
                detail="collection label name already exists in this category",
            ) from exc
        raise
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    emit_audit_event(
        "collection.label.create",
        actor=str(user.get("email") or ""),
        resource=f"collection_label:{label.id}",
        detail={"workspace_id": body.workspace_id, "category": label.category},
    )
    return success(_label_item(label))


@router.post("/{label_id}/deactivate")
def deactivate_collection_label(
    label_id: int,
    body: CollectionLabelDeactivateRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    try:
        label = deactivate_collection_label_record(db, label_id=label_id)
        db.commit()
        db.refresh(label)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    emit_audit_event(
        "collection.label.deactivate",
        actor=str(user.get("email") or ""),
        resource=f"collection_label:{label.id}",
        detail={"workspace_id": body.workspace_id, "category": label.category},
    )
    return success(_label_item(label))
