"""Data batch: grouping unit for governance, annotation, and review."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
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
    func,
)
from sqlalchemy.orm import relationship

from data.database import Base, JsonDocument

DATA_BATCH_STATUSES = (
    "open",
    "governing",
    "annotating",
    "reviewing",
    "publishing",
    "published",
    "no_publishable_asset",
    "failed",
)
GOVERNANCE_STAGES = ("integrity", "quality", "compliance")
GOVERNANCE_STAGE_STATUSES = ("queued", "running", "passed", "skipped", "failed")
GOVERNANCE_RUN_STATUSES = ("queued", "running", "passed", "failed")

_BATCH_STATUS_SQL = ", ".join(f"'{s}'" for s in DATA_BATCH_STATUSES)
_STAGE_SQL = ", ".join(f"'{s}'" for s in GOVERNANCE_STAGES)
_STAGE_STATUS_SQL = ", ".join(f"'{s}'" for s in GOVERNANCE_STAGE_STATUSES)
_RUN_STATUS_SQL = ", ".join(f"'{s}'" for s in GOVERNANCE_RUN_STATUSES)


class DataBatch(Base):
    """Governance grouping created from packages that passed intake review and have not been batched.

    Once created, the manifest, pipeline, and labels are all locked; modifications require creating a new data batch.
    The review mode is currently fixed to single review, with ``review_mode`` reserved for double-review expansion.
    """

    __tablename__ = "data_batches"
    __table_args__ = (
        CheckConstraint("review_mode IN ('single', 'dual')", name="ck_data_batches_review_mode"),
        CheckConstraint(
            f"status IN ({_BATCH_STATUS_SQL})",
            name="ck_data_batches_status",
        ),
        Index("ix_data_batches_workspace_created", "workspace_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    name = Column(String(128), nullable=False)
    status = Column(String(32), nullable=False, default="open")
    integrity_check_enabled = Column(Boolean, nullable=False, default=False)
    quality_check_enabled = Column(Boolean, nullable=False, default=False)
    compliance_check_enabled = Column(Boolean, nullable=False, default=False)
    annotation_enabled = Column(Boolean, nullable=False, default=False)
    review_mode = Column(String(16), nullable=False, default="single")
    annotator_user_ids_json = Column(JsonDocument, nullable=False, default=list)
    reviewer_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    assignment_snapshot_json = Column(JsonDocument, nullable=False, default=dict)
    episode_count = Column(Integer, nullable=False, default=0)
    valid_duration_hours = Column(Numeric(10, 2), nullable=False, default=0)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    packages = relationship("DataBatchPackage", back_populates="batch")
    episodes = relationship("DataBatchEpisode", back_populates="batch")
    labels = relationship("DataBatchLabel", back_populates="batch")
    governance_run = relationship(
        "DataBatchGovernanceRun",
        back_populates="batch",
        uselist=False,
    )


Index(
    "uq_data_batches_workspace_normalized_name",
    DataBatch.workspace_id,
    func.lower(func.btrim(DataBatch.name)),
    unique=True,
)


class DataBatchPackage(Base):
    """Association between data batches and data packages.

    A package is a review container and may contribute different Episode
    attempts to more than one immutable data batch.
    """

    __tablename__ = "data_batch_packages"
    __table_args__ = (
        UniqueConstraint(
            "data_batch_id",
            "data_package_id",
            name="uq_data_batch_packages_batch_package",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    data_batch_id = Column(Integer, ForeignKey("data_batches.id"), nullable=False)
    data_package_id = Column(Integer, ForeignKey("data_packages.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    batch = relationship("DataBatch", back_populates="packages")
    package = relationship("DataPackage")


class DataBatchEpisode(Base):
    """Immutable Episode membership and duration snapshot for one batch."""

    __tablename__ = "data_batch_episodes"
    __table_args__ = (
        UniqueConstraint("episode_id", name="uq_data_batch_episodes_episode"),
        UniqueConstraint(
            "data_batch_id",
            "episode_id",
            name="uq_data_batch_episodes_batch_episode",
        ),
        Index("ix_data_batch_episodes_batch", "data_batch_id", "episode_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    data_batch_id = Column(Integer, ForeignKey("data_batches.id"), nullable=False)
    episode_id = Column(Integer, ForeignKey("episodes.id"), nullable=False)
    data_package_id = Column(Integer, ForeignKey("data_packages.id"), nullable=False)
    duration_hours = Column(Numeric(10, 2), nullable=False, default=0)
    admission_attempt = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    batch = relationship("DataBatch", back_populates="episodes")
    episode = relationship("Episode")
    package = relationship("DataPackage")


class DataBatchLabel(Base):
    """Frozen snapshot association of business labels at batch creation."""

    __tablename__ = "data_batch_labels"

    data_batch_id = Column(Integer, ForeignKey("data_batches.id"), primary_key=True)
    collection_label_id = Column(Integer, ForeignKey("collection_labels.id"), primary_key=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    batch = relationship("DataBatch", back_populates="labels")
    label = relationship("CollectionLabel")


class DataBatchGovernanceRun(Base):
    """At most one governance run record per data batch."""

    __tablename__ = "data_batch_governance_runs"
    __table_args__ = (
        UniqueConstraint("data_batch_id", name="uq_data_batch_governance_runs_batch"),
        CheckConstraint(
            f"status IN ({_RUN_STATUS_SQL})",
            name="ck_data_batch_governance_runs_status",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    data_batch_id = Column(Integer, ForeignKey("data_batches.id"), nullable=False)
    status = Column(String(32), nullable=False, default="queued")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    batch = relationship("DataBatch", back_populates="governance_run")
    stages = relationship(
        "DataBatchStageRun",
        back_populates="governance_run",
        cascade="all, delete-orphan",
    )


class DataBatchStageRun(Base):
    """Governance stage execution record: integrity / quality / compliance."""

    __tablename__ = "data_batch_stage_runs"
    __table_args__ = (
        UniqueConstraint("run_id", "stage", name="uq_data_batch_stage_runs_run_stage"),
        CheckConstraint(f"stage IN ({_STAGE_SQL})", name="ck_data_batch_stage_runs_stage"),
        CheckConstraint(
            f"status IN ({_STAGE_STATUS_SQL})",
            name="ck_data_batch_stage_runs_status",
        ),
        CheckConstraint("attempt >= 1", name="ck_data_batch_stage_runs_attempt"),
        Index("ix_data_batch_stage_runs_run", "run_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("data_batch_governance_runs.id"), nullable=False)
    stage = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False, default="queued")
    attempt = Column(Integer, nullable=False, default=1)
    result_json = Column(JsonDocument, nullable=False, default=dict)
    error_message = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    governance_run = relationship("DataBatchGovernanceRun", back_populates="stages")
