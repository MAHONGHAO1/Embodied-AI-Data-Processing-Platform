"""Data package: business subject of collection, intake, intake review, and governance."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from data.database import Base, JsonDocument

DATA_PACKAGE_STATUSES = (
    "pending_assignment",
    "assigned",
    "pending_upload",
    "uploading",
    "parsing",
    "ingested",
    "pending_intake_review",
    "intake_approved",
    "batched",
    "governing",
    "published",
    "parse_failed",
    "voided",
)
DATA_PACKAGE_TERMINAL_STATUSES = frozenset({"parse_failed", "voided"})

_STATUS_SQL_LIST = ", ".join(f"'{status}'" for status in DATA_PACKAGE_STATUSES)


class DataPackage(Base):
    """Minimum business object of a collection allocation.

    Once allocated it is locked: reassignment is rejected by the service layer, and the database only ensures that
    both personnel references exist for an allocated package. Two effective duration fields serve dashboards and assets respectively:
    ``intake_*`` no longer changes after intake review completion, while ``governed_*`` is adjusted downwards with QC results.
    """

    __tablename__ = "data_packages"
    __table_args__ = (
        UniqueConstraint("package_uid", name="uq_data_packages_package_uid"),
        CheckConstraint(f"status IN ({_STATUS_SQL_LIST})", name="ck_data_packages_status"),
        CheckConstraint("target_duration_hours > 0", name="ck_data_packages_target_duration"),
        CheckConstraint(
            "intake_valid_duration_hours IS NULL OR intake_valid_duration_hours >= 0",
            name="ck_data_packages_intake_duration_non_negative",
        ),
        CheckConstraint(
            "governed_valid_duration_hours IS NULL OR governed_valid_duration_hours >= 0",
            name="ck_data_packages_governed_duration_non_negative",
        ),
        CheckConstraint(
            "governed_valid_duration_hours IS NULL "
            "OR intake_valid_duration_hours IS NULL "
            "OR governed_valid_duration_hours <= intake_valid_duration_hours",
            name="ck_data_packages_governed_within_intake",
        ),
        CheckConstraint(
            "(captured_duration_s IS NULL OR captured_duration_s >= 0) "
            "AND (captured_size_bytes IS NULL OR captured_size_bytes >= 0) "
            "AND (intake_valid_duration_s IS NULL OR intake_valid_duration_s >= 0) "
            "AND (intake_valid_size_bytes IS NULL OR intake_valid_size_bytes >= 0)",
            name="ck_data_packages_dashboard_facts_non_negative",
        ),
        CheckConstraint(
            "(intake_valid_duration_s IS NULL OR captured_duration_s IS NULL "
            "OR intake_valid_duration_s <= captured_duration_s) "
            "AND (intake_valid_size_bytes IS NULL OR captured_size_bytes IS NULL "
            "OR intake_valid_size_bytes <= captured_size_bytes)",
            name="ck_data_packages_dashboard_valid_within_captured",
        ),
        CheckConstraint(
            "status = 'pending_assignment' "
            "OR (responsible_collector_id IS NOT NULL AND operator_collector_id IS NOT NULL)",
            name="ck_data_packages_assigned_requires_collectors",
        ),
        Index("ix_data_packages_task_status", "collection_task_id", "status"),
        Index("ix_data_packages_workspace_status", "workspace_id", "status"),
        Index("ix_data_packages_workspace_captured", "workspace_id", "captured_started_at"),
        Index("ix_data_packages_task_captured", "collection_task_id", "captured_started_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    package_uid = Column(String(64), nullable=False)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    collection_project_id = Column(Integer, ForeignKey("collection_projects.id"), nullable=False)
    collection_task_id = Column(Integer, ForeignKey("collection_tasks.id"), nullable=False)
    status = Column(String(32), nullable=False, default="pending_assignment")
    target_duration_hours = Column(Numeric(10, 2), nullable=False)
    captured_duration_hours = Column(Numeric(10, 2), nullable=True)
    intake_valid_duration_hours = Column(Numeric(10, 2), nullable=True)
    governed_valid_duration_hours = Column(Numeric(10, 2), nullable=True)
    captured_started_at = Column(DateTime, nullable=True)
    captured_duration_s = Column(Numeric(14, 3), nullable=True)
    captured_size_bytes = Column(BigInteger, nullable=True)
    intake_valid_duration_s = Column(Numeric(14, 3), nullable=True)
    intake_valid_size_bytes = Column(BigInteger, nullable=True)
    responsible_collector_id = Column(Integer, ForeignKey("personnel_profiles.id"), nullable=True)
    operator_collector_id = Column(Integer, ForeignKey("personnel_profiles.id"), nullable=True)
    collection_device_id = Column(Integer, ForeignKey("collection_devices.id"), nullable=True)
    capture_mode = Column(String(32), nullable=True)
    qrdf_facts_json = Column(JsonDocument, nullable=False, default=dict)
    parse_error_code = Column(String(64), nullable=False, default="")
    parse_error_message = Column(String(512), nullable=False, default="")
    assigned_at = Column(DateTime, nullable=True)
    upload_completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    supplement_for_package_id = Column(
        Integer, ForeignKey("data_packages.id"), nullable=True, index=True
    )
    supplement_reason = Column(Text, nullable=False, default="")
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    task = relationship("CollectionTask")
    project = relationship("CollectionProject")
    responsible_collector = relationship(
        "PersonnelProfile", foreign_keys=[responsible_collector_id]
    )
    operator_collector = relationship("PersonnelProfile", foreign_keys=[operator_collector_id])
    device = relationship("CollectionDevice")
    created_by = relationship("User", foreign_keys=[created_by_user_id])


EPISODE_VALIDITY_STATUSES = ("valid", "intake_rejected", "qc_dropped")


class PackageIntakeReview(Base):
    """Intake review conclusion.

    Administrator responsibility, occurring after upload completion and before batch creation. A data package has only one conclusion:
    ``approved`` releases it into the batch creation pool, and ``rejected`` voids the entire package. ``is_bulk`` distinguishes between
    list-level bulk approval and individual package review for audit tracking.
    """

    __tablename__ = "package_intake_reviews"
    __table_args__ = (
        CheckConstraint(
            "verdict IN ('approved', 'rejected')", name="ck_package_intake_reviews_verdict"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    data_package_id = Column(Integer, ForeignKey("data_packages.id"), nullable=False)
    reviewer_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    verdict = Column(String(16), nullable=False)
    is_bulk = Column(Boolean, nullable=False, default=False)
    accepted_episode_ids_json = Column(JsonDocument, nullable=False, default=list)
    rejected_episode_ids_json = Column(JsonDocument, nullable=False, default=list)
    excluded_episodes_json = Column(JsonDocument, nullable=False, default=list)
    fact_attempts_json = Column(JsonDocument, nullable=False, default=dict)
    reason = Column(Text, nullable=False, default="")
    request_fingerprint = Column(String(64), nullable=True)
    episode_reasons_json = Column(JsonDocument, nullable=False, default=dict)
    source_fingerprint = Column(String(64), nullable=True)
    reviewed_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    package = relationship("DataPackage")


class PackageIntakeReviewDraft(Base):
    """Per-reviewer progress; saving it never changes the final intake facts."""

    __tablename__ = "package_intake_review_drafts"
    __table_args__ = (
        UniqueConstraint("data_package_id", "reviewer_user_id", name="uq_intake_draft_reviewer"),
        CheckConstraint("draft_version >= 1", name="ck_intake_draft_version"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    data_package_id = Column(Integer, ForeignKey("data_packages.id"), nullable=False)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    reviewer_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    draft_version = Column(Integer, nullable=False)
    source_fingerprint = Column(String(64), nullable=False)
    draft_json = Column(JsonDocument, nullable=False, default=dict)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
