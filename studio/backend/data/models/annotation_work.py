"""Annotation and review work items (collection domain, does not reuse legacy work_items)."""

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
)
from sqlalchemy.orm import relationship

from data.database import Base, JsonDocument

ANNOTATION_WORK_STATUSES = (
    "assigned",
    "in_progress",
    "submitted",
    "returned",
    "done",
)
REVIEW_WORK_STATUSES = (
    "assigned",
    "in_progress",
    "approved",
    "returned",
    "done",
)

_ANN_STATUS_SQL = ", ".join(f"'{s}'" for s in ANNOTATION_WORK_STATUSES)
_REV_STATUS_SQL = ", ".join(f"'{s}'" for s in REVIEW_WORK_STATUSES)
MATERIALIZATION_PROVENANCE_STATES = ("known", "uncertain")
_MATERIALIZATION_PROVENANCE_SQL = ", ".join(
    f"'{state}'" for state in MATERIALIZATION_PROVENANCE_STATES
)


class AnnotationWorkItem(Base):
    """Annotation task at the package level; unique per batch and package."""

    __tablename__ = "annotation_work_items"
    __table_args__ = (
        UniqueConstraint(
            "data_batch_id",
            "data_package_id",
            name="uq_annotation_work_items_batch_package",
        ),
        CheckConstraint(
            f"status IN ({_ANN_STATUS_SQL})",
            name="ck_annotation_work_items_status",
        ),
        CheckConstraint("generation >= 1", name="ck_annotation_work_items_generation"),
        CheckConstraint("draft_version >= 0", name="ck_annotation_work_items_draft_version"),
        CheckConstraint(
            f"materialization_provenance_state IN ({_MATERIALIZATION_PROVENANCE_SQL})",
            name="ck_annotation_work_items_materialization_provenance",
        ),
        Index("ix_annotation_work_items_workspace_assignee", "workspace_id", "assignee_user_id"),
        Index("ix_annotation_work_items_batch", "data_batch_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    data_batch_id = Column(Integer, ForeignKey("data_batches.id"), nullable=False)
    data_package_id = Column(Integer, ForeignKey("data_packages.id"), nullable=False)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    assignee_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status = Column(String(32), nullable=False, default="assigned")
    draft_json = Column(JsonDocument, nullable=False, default=dict)
    episode_members_json = Column(JsonDocument, nullable=False, default=list)
    # Immutable provenance of revisions written by this package work item.
    # EpisodeAnnotation has only legacy WorkItem provenance, so this is the
    # durable association for package-scoped annotation submissions.
    materialized_annotation_versions_json = Column(JsonDocument, nullable=False, default=list)
    # ``uncertain`` is reserved for rows that predate package provenance.  It
    # must never be assigned to new work items.
    materialization_provenance_state = Column(String(16), nullable=False, default="known")
    draft_version = Column(Integer, nullable=False, default=0)
    generation = Column(Integer, nullable=False, default=1)
    current_submission_id = Column(
        Integer,
        ForeignKey(
            "annotation_submissions.id", use_alter=True, name="fk_annotation_current_submission"
        ),
        nullable=True,
    )
    reassign_reason = Column(Text, nullable=False, default="")
    return_reason = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    review_item = relationship(
        "ReviewWorkItem",
        back_populates="annotation_item",
        uselist=False,
    )
    batch = relationship("DataBatch", foreign_keys=[data_batch_id])
    package = relationship("DataPackage", foreign_keys=[data_package_id])
    assignee = relationship("User", foreign_keys=[assignee_user_id])
    workspace = relationship("Workspace", foreign_keys=[workspace_id], viewonly=True)


class ReviewWorkItem(Base):
    """Single-review task: one-to-one attachment to an annotation work item."""

    __tablename__ = "review_work_items"
    __table_args__ = (
        UniqueConstraint(
            "annotation_work_item_id",
            name="uq_review_work_items_annotation",
        ),
        CheckConstraint(
            f"status IN ({_REV_STATUS_SQL})",
            name="ck_review_work_items_status",
        ),
        CheckConstraint("generation >= 1", name="ck_review_work_items_generation"),
        Index("ix_review_work_items_workspace_assignee", "workspace_id", "assignee_user_id"),
        Index("ix_review_work_items_batch", "data_batch_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    annotation_work_item_id = Column(
        Integer, ForeignKey("annotation_work_items.id"), nullable=False
    )
    data_batch_id = Column(Integer, ForeignKey("data_batches.id"), nullable=False)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    assignee_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status = Column(String(32), nullable=False, default="assigned")
    reason = Column(Text, nullable=False, default="")
    generation = Column(Integer, nullable=False, default=1)
    submission_id = Column(Integer, ForeignKey("annotation_submissions.id"), nullable=True)
    reassign_reason = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    annotation_item = relationship("AnnotationWorkItem", back_populates="review_item")
    submission = relationship("AnnotationSubmission", foreign_keys=[submission_id])
    batch = relationship("DataBatch", foreign_keys=[data_batch_id])
    assignee = relationship("User", foreign_keys=[assignee_user_id])
    workspace = relationship("Workspace", foreign_keys=[workspace_id], viewonly=True)


class AnnotationSubmission(Base):
    """Append-only, source-bound package submission, independent of mutable drafts."""

    __tablename__ = "annotation_submissions"
    __table_args__ = (
        UniqueConstraint(
            "annotation_work_item_id",
            "generation",
            "draft_version",
            name="uq_annotation_submission_version",
        ),
        CheckConstraint(
            "generation >= 1 AND draft_version >= 0", name="ck_annotation_submission_version"
        ),
        Index("ix_annotation_submissions_item", "annotation_work_item_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    annotation_work_item_id = Column(
        Integer, ForeignKey("annotation_work_items.id"), nullable=False
    )
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    submitted_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    generation = Column(Integer, nullable=False)
    draft_version = Column(Integer, nullable=False)
    episodes_json = Column(JsonDocument, nullable=False)
    draft_json = Column(JsonDocument, nullable=False)
    source_json = Column(JsonDocument, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AnnotationSubmissionReview(Base):
    """An immutable decision on one exact submission; resubmits retain history."""

    __tablename__ = "annotation_submission_reviews"
    __table_args__ = (
        UniqueConstraint("submission_id", name="uq_annotation_submission_review"),
        CheckConstraint(
            "decision IN ('approved', 'returned')", name="ck_annotation_submission_review_decision"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    submission_id = Column(Integer, ForeignKey("annotation_submissions.id"), nullable=False)
    review_work_item_id = Column(Integer, ForeignKey("review_work_items.id"), nullable=False)
    reviewer_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    generation = Column(Integer, nullable=False)
    decision = Column(String(16), nullable=False)
    reason = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
