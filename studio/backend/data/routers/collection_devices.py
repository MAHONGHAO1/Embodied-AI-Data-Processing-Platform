"""Workspace-scoped offline collection device directory."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from data.database import CollectionDevice, get_db
from data.security.audit import emit_audit_event
from data.services.resource_names import (
    DEVICE_SERIAL_CONFLICT,
    conflict_detail,
    is_constraint_conflict,
    normalized_serial,
)
from data.services.workspace_access import require_workspace_actor
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/collection-devices", tags=["采集设备目录"])


class CollectionDeviceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=128)
    device_type: str = Field(min_length=1, max_length=64)
    model: str = Field(default="", max_length=128)
    serial_number: str = Field(min_length=1, max_length=128)


class CollectionDeviceUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    device_type: str | None = Field(default=None, min_length=1, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    serial_number: str | None = Field(default=None, min_length=1, max_length=128)
    is_active: bool | None = None


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub"))
    except (TypeError, ValueError):
        return None


def collection_device_item(device: CollectionDevice) -> dict[str, object]:
    return {
        "id": device.id,
        "name": device.name,
        "device_type": device.device_type,
        "model": device.model,
        "serial_number": device.serial_number,
        "is_active": device.is_active,
    }


def _required_text(value: str, *, field: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise HTTPException(status_code=422, detail=f"{field} is required")
    return normalized


def _serial_number(value: str) -> str:
    return normalized_serial(_required_text(value, field="serial_number", maximum=128))


def _ensure_serial_available(
    db: Session,
    *,
    workspace_id: int,
    serial_number: str,
    exclude_device_id: int | None = None,
) -> None:
    query = db.query(CollectionDevice.id).filter(
        CollectionDevice.workspace_id == workspace_id,
        func.upper(func.btrim(CollectionDevice.serial_number)) == serial_number,
    )
    if exclude_device_id is not None:
        query = query.filter(CollectionDevice.id != exclude_device_id)
    if query.first() is not None:
        raise HTTPException(status_code=409, detail=conflict_detail(DEVICE_SERIAL_CONFLICT))


@router.get("")
def list_collection_devices(
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
    query = db.query(CollectionDevice).filter(CollectionDevice.workspace_id == workspace_id)
    if not include_inactive:
        query = query.filter(CollectionDevice.is_active.is_(True))
    rows = query.order_by(CollectionDevice.name.asc(), CollectionDevice.id.asc()).all()
    return success({"items": [collection_device_item(row) for row in rows]})


@router.post("")
def create_collection_device(
    body: CollectionDeviceCreateRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    try:
        require_workspace_actor(db, actor_id=_actor_id(user), workspace_id=body.workspace_id)
        serial_number = _serial_number(body.serial_number)
        _ensure_serial_available(db, workspace_id=body.workspace_id, serial_number=serial_number)
        device = CollectionDevice(
            workspace_id=body.workspace_id,
            name=_required_text(body.name, field="name", maximum=128),
            device_type=_required_text(body.device_type, field="device_type", maximum=64),
            model=body.model.strip(),
            serial_number=serial_number,
            is_active=True,
        )
        db.add(device)
        db.commit()
        db.refresh(device)
    except IntegrityError as exc:
        db.rollback()
        if is_constraint_conflict(exc, "uq_collection_devices_workspace_normalized_serial"):
            raise HTTPException(
                status_code=409,
                detail=conflict_detail(DEVICE_SERIAL_CONFLICT),
            ) from exc
        raise
    except PermissionError as exc:
        db.rollback()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    emit_audit_event(
        "collection_device.create",
        actor=str(user.get("email") or ""),
        resource=f"collection_device:{device.id}",
        detail={"workspace_id": device.workspace_id},
    )
    return success(collection_device_item(device))


@router.patch("/{device_id}")
def update_collection_device(
    device_id: int,
    body: CollectionDeviceUpdateRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    device = db.get(CollectionDevice, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="collection device does not exist")
    if not body.model_fields_set:
        raise HTTPException(status_code=422, detail="at least one device field is required")
    try:
        require_workspace_actor(db, actor_id=_actor_id(user), workspace_id=device.workspace_id)
        if "name" in body.model_fields_set and body.name is not None:
            device.name = _required_text(body.name, field="name", maximum=128)
        if "device_type" in body.model_fields_set and body.device_type is not None:
            device.device_type = _required_text(body.device_type, field="device_type", maximum=64)
        if "model" in body.model_fields_set and body.model is not None:
            device.model = body.model.strip()
        if "serial_number" in body.model_fields_set and body.serial_number is not None:
            serial_number = _serial_number(body.serial_number)
            _ensure_serial_available(
                db,
                workspace_id=device.workspace_id,
                serial_number=serial_number,
                exclude_device_id=device.id,
            )
            device.serial_number = serial_number
        if "is_active" in body.model_fields_set and body.is_active is not None:
            device.is_active = body.is_active
        db.commit()
        db.refresh(device)
    except IntegrityError as exc:
        db.rollback()
        if is_constraint_conflict(exc, "uq_collection_devices_workspace_normalized_serial"):
            raise HTTPException(
                status_code=409,
                detail=conflict_detail(DEVICE_SERIAL_CONFLICT),
            ) from exc
        raise
    except PermissionError as exc:
        db.rollback()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    emit_audit_event(
        "collection_device.update",
        actor=str(user.get("email") or ""),
        resource=f"collection_device:{device.id}",
        detail={"workspace_id": device.workspace_id, "is_active": device.is_active},
    )
    return success(collection_device_item(device))
