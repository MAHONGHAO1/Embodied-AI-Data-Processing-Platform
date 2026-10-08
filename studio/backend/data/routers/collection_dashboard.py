"""Workspace-scoped collection dashboard endpoint."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from data.database import Workspace, get_db
from data.services.collection_access import require_collection_workspace
from data.services.collection_dashboard import (
    DashboardFilterError,
    capacity_board,
    csv_filename,
    data_board,
    data_board_csv,
    efficiency_board,
    resolve_filters,
)
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/collection-dashboard", tags=["采集概览看板"])


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub"))
    except (TypeError, ValueError):
        return None


@router.get("/data")
def collection_data_board(
    workspace_id: int = Query(..., gt=0),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    project_ids: list[int] = Query(default=[]),
    task_ids: list[int] = Query(default=[]),
    scene_label_ids: list[int] = Query(default=[]),
    purpose_label_ids: list[int] = Query(default=[]),
    granularity: str = Query(default="day"),
    basis: str = Query(default="valid"),
    tz: str = Query(default="Asia/Shanghai"),
    format: str = Query(default="json"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:read")
    if db.get(Workspace, workspace_id) is None:
        raise HTTPException(status_code=404, detail="workspace does not exist")
    try:
        require_collection_workspace(db, actor_id=_actor_id(user), workspace_id=workspace_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if format not in {"json", "csv"}:
        raise HTTPException(status_code=422, detail="format must be json or csv")
    try:
        filters = resolve_filters(
            db,
            workspace_id=workspace_id,
            start_date=start_date,
            end_date=end_date,
            project_ids=project_ids,
            task_ids=task_ids,
            scene_label_ids=scene_label_ids,
            purpose_label_ids=purpose_label_ids,
            granularity=granularity,
            basis=basis,
            tz=tz,
        )
    except DashboardFilterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    payload = data_board(db, filters)
    if format == "csv":
        return Response(
            content=data_board_csv(payload).encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{csv_filename(filters)}"'},
        )
    return success(payload)


def _resolve_board_filters(
    db: Session,
    *,
    workspace_id: int,
    start_date: date | None,
    end_date: date | None,
    project_ids: list[int],
    task_ids: list[int],
    scene_label_ids: list[int],
    purpose_label_ids: list[int],
    granularity: str,
    basis: str,
    tz: str,
):
    try:
        return resolve_filters(
            db,
            workspace_id=workspace_id,
            start_date=start_date,
            end_date=end_date,
            project_ids=project_ids,
            task_ids=task_ids,
            scene_label_ids=scene_label_ids,
            purpose_label_ids=purpose_label_ids,
            granularity=granularity,
            basis=basis,
            tz=tz,
        )
    except DashboardFilterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _check_board_access(db: Session, user: dict, workspace_id: int) -> None:
    require_permission(user, "episode:read")
    if db.get(Workspace, workspace_id) is None:
        raise HTTPException(status_code=404, detail="workspace does not exist")
    try:
        require_collection_workspace(db, actor_id=_actor_id(user), workspace_id=workspace_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/capacity")
def collection_capacity_board(
    workspace_id: int = Query(..., gt=0),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    project_ids: list[int] = Query(default=[]),
    task_ids: list[int] = Query(default=[]),
    scene_label_ids: list[int] = Query(default=[]),
    purpose_label_ids: list[int] = Query(default=[]),
    granularity: str = Query(default="day"),
    basis: str = Query(default="valid"),
    tz: str = Query(default="Asia/Shanghai"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _check_board_access(db, user, workspace_id)
    filters = _resolve_board_filters(
        db,
        workspace_id=workspace_id,
        start_date=start_date,
        end_date=end_date,
        project_ids=project_ids,
        task_ids=task_ids,
        scene_label_ids=scene_label_ids,
        purpose_label_ids=purpose_label_ids,
        granularity=granularity,
        basis=basis,
        tz=tz,
    )
    return success(capacity_board(db, filters))


@router.get("/efficiency")
def collection_efficiency_board(
    workspace_id: int = Query(..., gt=0),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    project_ids: list[int] = Query(default=[]),
    task_ids: list[int] = Query(default=[]),
    scene_label_ids: list[int] = Query(default=[]),
    purpose_label_ids: list[int] = Query(default=[]),
    granularity: str = Query(default="day"),
    basis: str = Query(default="valid"),
    tz: str = Query(default="Asia/Shanghai"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _check_board_access(db, user, workspace_id)
    filters = _resolve_board_filters(
        db,
        workspace_id=workspace_id,
        start_date=start_date,
        end_date=end_date,
        project_ids=project_ids,
        task_ids=task_ids,
        scene_label_ids=scene_label_ids,
        purpose_label_ids=purpose_label_ids,
        granularity=granularity,
        basis=basis,
        tz=tz,
    )
    return success(efficiency_board(db, filters))
