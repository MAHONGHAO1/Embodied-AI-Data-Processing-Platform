"""Collection upload session: technical session, does not create business data packages."""

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
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from data.database import Base, JsonDocument

COLLECTION_UPLOAD_SESSION_STATUSES = (
    "init",
    "uploading",
    "uploaded",
    "parsing",
    "succeeded",
    "failed",
    "cancelled",
)
COLLECTION_UPLOAD_MODES = ("duance_sdk", "chunked", "oss_multipart", "authorized_import")

_STATUS_SQL = ", ".join(f"'{s}'" for s in COLLECTION_UPLOAD_SESSION_STATUSES)
_MODE_SQL = ", ".join(f"'{s}'" for s in COLLECTION_UPLOAD_MODES)


class CollectionUploadSession(Base):
    __tablename__ = "collection_upload_sessions"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUS_SQL})", name="ck_collection_upload_sessions_status"),
        CheckConstraint(f"upload_mode IN ({_MODE_SQL})", name="ck_collection_upload_sessions_mode"),
        Index("ix_collection_upload_sessions_workspace_created", "workspace_id", "created_at"),
        Index("ix_collection_upload_sessions_project_status", "collection_project_id", "status"),
    )

    id = Column(String(36), primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    collection_project_id = Column(Integer, ForeignKey("collection_projects.id"), nullable=False)
    status = Column(String(32), nullable=False, default="init")
    upload_mode = Column(String(32), nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    result_json = Column(JsonDocument, nullable=False, default=dict)
    error_code = Column(String(64), nullable=False, default="")
    error_message = Column(String(512), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    package_links = relationship(
        "CollectionUploadSessionPackage",
        back_populates="upload_session",
        cascade="all, delete-orphan",
    )


class CollectionUploadSessionPackage(Base):
    __tablename__ = "collection_upload_session_packages"
    __table_args__ = (
        UniqueConstraint(
            "upload_session_id",
            "data_package_id",
            name="uq_collection_upload_session_packages_pair",
        ),
        Index("ix_collection_upload_session_packages_package", "data_package_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    upload_session_id = Column(
        String(36), ForeignKey("collection_upload_sessions.id"), nullable=False
    )
    data_package_id = Column(Integer, ForeignKey("data_packages.id"), nullable=False)
    package_uid = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    upload_session = relationship("CollectionUploadSession", back_populates="package_links")
    package = relationship("DataPackage")
