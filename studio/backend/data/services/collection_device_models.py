"""Collection device model catalog seed operations."""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from data.models.collection_config import CollectionDeviceModel

DEFAULT_DEVICE_MODELS = (
    {
        "vendor": "Unitree",
        "model": "G1",
        "device_type": "humanoid",
        "modalities_json": ["rgb", "depth"],
    },
    {
        "vendor": "Custom",
        "model": "EGO-Rig",
        "device_type": "ego",
        "modalities_json": ["rgb"],
    },
)


def ensure_default_device_models(db: Session) -> list[CollectionDeviceModel]:
    """Insert missing built-in device models without duplicating normalized names."""
    inserted: list[CollectionDeviceModel] = []
    for preset in DEFAULT_DEVICE_MODELS:
        vendor = str(preset["vendor"]).strip()
        model = str(preset["model"]).strip()
        exists = (
            db.query(CollectionDeviceModel.id)
            .filter(
                func.lower(func.btrim(CollectionDeviceModel.vendor)) == vendor.lower(),
                func.lower(func.btrim(CollectionDeviceModel.model)) == model.lower(),
            )
            .first()
        )
        if exists is not None:
            continue
        device_model = CollectionDeviceModel(
            vendor=vendor,
            model=model,
            device_type=preset["device_type"],
            modalities_json=list(preset["modalities_json"]),
            is_active=True,
        )
        db.add(device_model)
        db.flush()
        inserted.append(device_model)
    return inserted
