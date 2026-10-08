"""Data collection configuration domain: business label dictionary and device model catalog."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    func,
)

from data.database import Base, JsonDocument

COLLECTION_LABEL_CATEGORIES = ("scene", "purpose", "training", "project", "modality")


class CollectionLabel(Base):
    """Business labels maintained in collection configuration.

    Unrelated in semantics to the legacy ``TaskLabel`` (action vocabulary) and intentionally does not reuse the same table.
    Labels that are already referenced are only allowed to be deactivated; thus there is no deletion path, only ``is_active``.
    """

    __tablename__ = "collection_labels"
    __table_args__ = (
        CheckConstraint(
            "category IN ('scene', 'purpose', 'training', 'project', 'modality')",
            name="ck_collection_labels_category",
        ),
        Index("ix_collection_labels_category_active", "category", "is_active"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    category = Column(String(32), nullable=False)
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=False, default="")
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


Index(
    "uq_collection_labels_category_normalized_name",
    CollectionLabel.category,
    func.lower(func.btrim(CollectionLabel.name)),
    unique=True,
)


class CollectionDeviceModel(Base):
    """Collection device model catalog.

    Currently initialized by preset data and maintained in the background, with the front-end creation entry disabled.
    Device instances (``CollectionDevice``) are still registered individually by SN; the model catalog only standardizes selectable options.
    """

    __tablename__ = "collection_device_models"

    id = Column(Integer, primary_key=True, autoincrement=True)
    vendor = Column(String(128), nullable=False)
    model = Column(String(128), nullable=False)
    device_type = Column(String(64), nullable=False)
    modalities_json = Column(JsonDocument, nullable=False, default=list)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


Index(
    "uq_collection_device_models_vendor_model",
    func.lower(func.btrim(CollectionDeviceModel.vendor)),
    func.lower(func.btrim(CollectionDeviceModel.model)),
    unique=True,
)
