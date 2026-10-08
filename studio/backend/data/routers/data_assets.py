"""Data asset catalog API (global scope, not implicitly filtered by collection workspace)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from data.database import get_db
from data.models.data_asset import DataAsset
from data.services.data_assets import get_data_asset, list_data_assets_page
from data.utils.formatting import format_api_datetime
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/data-assets", tags=["数据资产"])


def _decimal_str(value) -> str:
    return f"{value:.2f}"


def _asset_item(asset: DataAsset) -> dict[str, object]:
    return {
        "id": asset.id,
        "data_batch_id": asset.data_batch_id,
        "workspace_id": asset.workspace_id,
        "governed_valid_duration_hours": _decimal_str(asset.governed_valid_duration_hours),
        "stage_snapshot_json": asset.stage_snapshot_json or {},
        "episode_ids_json": asset.episode_ids_json or [],
        "source_json": asset.source_json or {},
        "source_snapshot_json": asset.source_snapshot_json or {},
        "source_snapshot_id": asset.source_snapshot_id or "",
        "created_at": format_api_datetime(asset.created_at),
    }


@router.get("")
def get_data_assets(
    source_workspace_id: int | None = Query(default=None, gt=0),
    limit: int | None = Query(default=None, ge=1, le=100),
    offset: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:read")
    page = list_data_assets_page(
        db, source_workspace_id=source_workspace_id, limit=limit, offset=offset
    )
    return success({**page, "items": [_asset_item(item) for item in page["items"]]})


@router.get("/{asset_id}")
def get_data_asset_detail(
    asset_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:read")
    try:
        asset = get_data_asset(db, asset_id=asset_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return success(_asset_item(asset))
