"""Read-only collection device model catalog API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from data.database import get_db
from data.models.collection_config import CollectionDeviceModel
from data.services.collection_access import require_collection_workspace
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/collection-device-models", tags=["采集设备型号目录"])


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub"))
    except (TypeError, ValueError):
        return None


def _device_model_item(device_model: CollectionDeviceModel) -> dict[str, object]:
    return {
        "id": device_model.id,
        "vendor": device_model.vendor,
        "model": device_model.model,
        "device_type": device_model.device_type,
        "modalities": device_model.modalities_json,
        "is_active": device_model.is_active,
    }


@router.get("")
def list_collection_device_models(
    workspace_id: int = Query(..., gt=0),
    include_inactive: bool = Query(default=False),
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

    query = db.query(CollectionDeviceModel)
    if not include_inactive:
        query = query.filter(CollectionDeviceModel.is_active.is_(True))
    rows = query.order_by(
        CollectionDeviceModel.vendor.asc(),
        CollectionDeviceModel.model.asc(),
        CollectionDeviceModel.id.asc(),
    ).all()
    return success({"items": [_device_model_item(row) for row in rows]})
