"""Authorized controlled vocabulary APIs for import task defaults."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from data.database import get_db
from data.security.audit import emit_audit_event
from data.services.task_labels import create_task_label, list_task_labels, task_label_summary
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/task-labels", tags=["采集任务词表"])


class TaskLabelCreateRequest(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=4_000)


@router.get("")
def list_task_labels_endpoint(
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    # Episode readers need the controlled vocabulary to filter and understand
    # already-ingested data; mutation remains restricted below.
    require_permission(user, "episode:read")
    labels, total = list_task_labels(db, limit=limit, offset=offset)
    return success(
        {
            "items": [task_label_summary(label) for label in labels],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@router.post("")
def create_task_label_endpoint(
    body: TaskLabelCreateRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "import:write")
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="only an admin can create task labels")
    try:
        label = create_task_label(db, key=body.key, name=body.name, description=body.description)
        db.commit()
        db.refresh(label)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    emit_audit_event(
        "task_label.create",
        actor=str(user.get("email") or ""),
        resource=f"task_label:{label.id}",
        detail={"key": label.key},
    )
    return success(task_label_summary(label))
