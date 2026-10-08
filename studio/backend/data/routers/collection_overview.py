"""Workspace-scoped collection overview API."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from data.database import get_db
from data.services.collection_access import require_collection_workspace
from data.services.collection_overview import get_collection_overview
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/collection-overview", tags=["采集概览"])


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub"))
    except (TypeError, ValueError):
        return None


def _decimal(value: Decimal) -> str:
    return f"{value:.2f}"


@router.get("")
def collection_overview(
    workspace_id: int = Query(..., gt=0),
    collection_project_id: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:read")
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

    overview = get_collection_overview(
        db,
        workspace_id=workspace_id,
        collection_project_id=collection_project_id,
    )
    overview["target_duration_hours"] = _decimal(overview["target_duration_hours"])
    overview["intake_valid_duration_hours"] = _decimal(overview["intake_valid_duration_hours"])
    return success(overview)
