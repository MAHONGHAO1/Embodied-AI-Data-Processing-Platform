"""Source record models for native LeRobot direct uploads to export buckets.

These records intentionally do not associate with collection batches or collection task sets.
They maintain an immutable manifest of export objects and directly connect to global catalog datasets
after successful execution of the validation worker.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from data.database import Base, JsonDocument

DIRECT_SOURCE_STATUSES = (
    "declared",
    "uploading",
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
)
DIRECT_OBJECT_STATUSES = ("declared", "uploading", "completed", "cancelled")

_DIRECT_SOURCE_STATUS_SQL = ", ".join(f"'{status}'" for status in DIRECT_SOURCE_STATUSES)
_DIRECT_OBJECT_STATUS_SQL = ", ".join(f"'{status}'" for status in DIRECT_OBJECT_STATUSES)


class NativeLerobotDirectSource(Base):
    """Native LeRobot data source declared for direct upload to export bucket."""

    __tablename__ = "native_lerobot_direct_sources"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_DIRECT_SOURCE_STATUS_SQL})",
            name="ck_native_lerobot_direct_sources_status",
        ),
        CheckConstraint(
            "file_count > 0",
            name="ck_native_lerobot_direct_sources_file_count",
        ),
        CheckConstraint(
            "total_size > 0",
            name="ck_native_lerobot_direct_sources_total_size",
        ),
        Index(
            "ix_native_lerobot_direct_sources_status_created",
            "status",
            "created_at",
            "id",
        ),
        Index(
            "ix_native_lerobot_direct_sources_catalog",
            "catalog_dataset_id",
            "catalog_dataset_version_id",
        ),
    )

    id = Column(String(36), primary_key=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=False, default="")
    # Collection workspace is only for provenance reference. Catalog searches and version access must never filter or isolate by this column.
    source_workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    robot_type = Column(String(64), nullable=False)
    dataset_id = Column(String(256), nullable=False)
    status = Column(String(32), nullable=False, default="declared")
    file_count = Column(Integer, nullable=False)
    total_size = Column(BigInteger, nullable=False)
    manifest_sha256 = Column(String(64), nullable=False)
    marker_ref_json = Column(JsonDocument, nullable=False, default=dict)
    catalog_dataset_id = Column(Integer, ForeignKey("catalog_datasets.id"), nullable=True)
    catalog_dataset_version_id = Column(
        Integer,
        ForeignKey("catalog_dataset_versions.id"),
        nullable=True,
    )
    validation_job_id = Column(String(36), nullable=True)
    error_code = Column(String(64), nullable=False, default="")
    error_message = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    objects = relationship(
        "NativeLerobotDirectObject",
        back_populates="source",
        cascade="all, delete-orphan",
        order_by="NativeLerobotDirectObject.path",
    )


class NativeLerobotDirectObject(Base):
    """Upload storage object record declared in direct-upload native data source."""

    __tablename__ = "native_lerobot_direct_objects"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_DIRECT_OBJECT_STATUS_SQL})",
            name="ck_native_lerobot_direct_objects_status",
        ),
        CheckConstraint(
            "size_bytes > 0",
            name="ck_native_lerobot_direct_objects_size",
        ),
        UniqueConstraint(
            "source_id",
            "path",
            name="uq_native_lerobot_direct_objects_source_path",
        ),
        UniqueConstraint(
            "object_key",
            name="uq_native_lerobot_direct_objects_key",
        ),
        Index(
            "ix_native_lerobot_direct_objects_source_status",
            "source_id",
            "status",
            "id",
        ),
    )

    id = Column(String(36), primary_key=True)
    source_id = Column(
        String(36),
        ForeignKey("native_lerobot_direct_sources.id"),
        nullable=False,
    )
    path = Column(String(1024), nullable=False)
    object_key = Column(String(1400), nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    sha256 = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, default="declared")
    upload_id = Column(String(512), nullable=False, default="")
    provider_ref_json = Column(JsonDocument, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    source = relationship("NativeLerobotDirectSource", back_populates="objects")
