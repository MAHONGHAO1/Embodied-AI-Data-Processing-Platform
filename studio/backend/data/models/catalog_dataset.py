"""Catalog dataset: versioned asset manifests, exports, and direct LeRobot imports (does not reuse legacy datasets)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import relationship

from data.database import Base, JsonDocument

CATALOG_DATASET_STATUSES = ("active", "archived")
CATALOG_SOURCE_KINDS = ("qrdf_assets", "lerobot_direct")
CATALOG_VERSION_STATUSES = ("active", "archived")
CATALOG_EXPORT_FORMATS = ("qrdf_0_2", "lerobot_3_0")
CATALOG_EXPORT_STATUSES = ("queued", "running", "succeeded", "failed")

_DATASET_STATUS_SQL = ", ".join(f"'{s}'" for s in CATALOG_DATASET_STATUSES)
_SOURCE_KIND_SQL = ", ".join(f"'{s}'" for s in CATALOG_SOURCE_KINDS)
_VERSION_STATUS_SQL = ", ".join(f"'{s}'" for s in CATALOG_VERSION_STATUSES)
_EXPORT_FORMAT_SQL = ", ".join(f"'{s}'" for s in CATALOG_EXPORT_FORMATS)
_EXPORT_STATUS_SQL = ", ".join(f"'{s}'" for s in CATALOG_EXPORT_STATUSES)


class CatalogDataset(Base):
    """Global catalog dataset (separated from legacy episode-level ``datasets`` table)."""

    __tablename__ = "catalog_datasets"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_DATASET_STATUS_SQL})",
            name="ck_catalog_datasets_status",
        ),
        CheckConstraint(
            f"source_kind IN ({_SOURCE_KIND_SQL})",
            name="ck_catalog_datasets_source_kind",
        ),
        Index("ix_catalog_datasets_created", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=False, default="")
    status = Column(String(32), nullable=False, default="active")
    source_kind = Column(String(32), nullable=False, default="qrdf_assets")
    # Optional attach point for LeRobot direct imports (legacy Native LeRobot).
    native_lerobot_dataset_id = Column(
        Integer, ForeignKey("native_lerobot_datasets.id"), nullable=True
    )
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    versions = relationship(
        "CatalogDatasetVersion",
        back_populates="dataset",
        order_by="CatalogDatasetVersion.version",
    )


class CatalogDatasetVersion(Base):
    """Immutable version: asset manifest cannot be modified in place once created."""

    __tablename__ = "catalog_dataset_versions"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_VERSION_STATUS_SQL})",
            name="ck_catalog_dataset_versions_status",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_catalog_dataset_versions_version",
        ),
        UniqueConstraint(
            "dataset_id",
            "version",
            name="uq_catalog_dataset_versions_dataset_version",
        ),
        Index(
            "ix_catalog_dataset_versions_dataset_created",
            "dataset_id",
            "created_at",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    dataset_id = Column(Integer, ForeignKey("catalog_datasets.id"), nullable=False)
    version = Column(Integer, nullable=False)
    status = Column(String(32), nullable=False, default="active")
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    source_snapshot_json = Column(JsonDocument, nullable=False, default=dict)
    source_snapshot_id = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    dataset = relationship("CatalogDataset", back_populates="versions")
    assets = relationship(
        "CatalogDatasetVersionAsset",
        back_populates="version",
        order_by="CatalogDatasetVersionAsset.position",
        cascade="all, delete-orphan",
    )
    exports = relationship(
        "CatalogDatasetExport",
        back_populates="version",
        cascade="all, delete-orphan",
    )


class CatalogDatasetVersionAsset(Base):
    """Asset manifest item within a version; assets cannot be duplicated within the same version."""

    __tablename__ = "catalog_dataset_version_assets"
    __table_args__ = (
        UniqueConstraint(
            "version_id",
            "data_asset_id",
            name="uq_catalog_dataset_version_assets_pair",
        ),
        UniqueConstraint(
            "version_id",
            "position",
            name="uq_catalog_dataset_version_assets_position",
        ),
        Index(
            "ix_catalog_dataset_version_assets_asset",
            "data_asset_id",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    version_id = Column(Integer, ForeignKey("catalog_dataset_versions.id"), nullable=False)
    data_asset_id = Column(Integer, ForeignKey("data_assets.id"), nullable=False)
    asset_snapshot_id = Column(String(64), nullable=False, default="")
    position = Column(Integer, nullable=False)

    version = relationship("CatalogDatasetVersion", back_populates="assets")


class CatalogDatasetExport(Base):
    """Version export record; its existence is treated as a reference, preventing physical deletion of the version."""

    __tablename__ = "catalog_dataset_exports"
    __table_args__ = (
        CheckConstraint(
            f"format IN ({_EXPORT_FORMAT_SQL})",
            name="ck_catalog_dataset_exports_format",
        ),
        CheckConstraint(
            f"status IN ({_EXPORT_STATUS_SQL})",
            name="ck_catalog_dataset_exports_status",
        ),
        Index(
            "ix_catalog_dataset_exports_version_created",
            "version_id",
            "created_at",
        ),
        CheckConstraint("attempt >= 1", name="ck_catalog_dataset_exports_attempt"),
        CheckConstraint(
            "length(idempotency_key) > 0", name="ck_catalog_dataset_exports_idempotency"
        ),
        CheckConstraint(
            "length(input_snapshot_id) > 0", name="ck_catalog_dataset_exports_snapshot"
        ),
        Index(
            "uq_catalog_dataset_exports_idempotency",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key <> ''"),
            sqlite_where=text("idempotency_key <> ''"),
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    version_id = Column(Integer, ForeignKey("catalog_dataset_versions.id"), nullable=False)
    format = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False, default="queued")
    checksum = Column(String(128), nullable=False, default="")
    detail_json = Column(JsonDocument, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    idempotency_key = Column(String(255), nullable=False, default="")
    input_snapshot_id = Column(String(64), nullable=False, default="")
    input_snapshot_json = Column(JsonDocument, nullable=False, default=dict)
    attempt = Column(Integer, nullable=False, default=1)
    output_prefix = Column(String(512), nullable=False, default="")
    output_plan_json = Column(JsonDocument, nullable=False, default=dict)
    size_bytes = Column(Integer, nullable=True)
    sha256 = Column(String(64), nullable=True)
    manifest_json = Column(JsonDocument, nullable=False, default=dict)
    oss_uri = Column(String(1024), nullable=True)
    error_code = Column(String(64), nullable=False, default="")
    error_message = Column(Text, nullable=False, default="")
    cleanup_json = Column(JsonDocument, nullable=False, default=dict)

    version = relationship("CatalogDatasetVersion", back_populates="exports")
