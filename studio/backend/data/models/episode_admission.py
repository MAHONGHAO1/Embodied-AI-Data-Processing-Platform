"""Persistent Episode-level admission validation fact model.

The admission review and batch processing APIs read this table directly to obtain admission fact status.
Workers write this fact after validating raw storage objects, allowing API processes to determine whether
an episode meets admission criteria without repeatedly querying object storage.
"""

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
    String,
    Text,
    UniqueConstraint,
    text,
)

from data.database import Base, JsonDocument


class EpisodeAdmissionFact(Base):
    __tablename__ = "episode_admission_facts"
    __table_args__ = (
        UniqueConstraint(
            "episode_id",
            "attempt",
            name="uq_episode_admission_facts_episode_attempt",
        ),
        Index(
            "uq_episode_admission_facts_current_episode",
            "episode_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index(
            "ix_episode_admission_facts_episode_current",
            "episode_id",
            "is_current",
        ),
        CheckConstraint("attempt >= 1", name="ck_episode_admission_facts_attempt"),
        CheckConstraint(
            "integrity_status IN ('pending', 'running', 'passed', 'failed', 'not_applicable')",
            name="ck_episode_admission_facts_integrity_status",
        ),
        CheckConstraint(
            "preview_status IN ('pending', 'running', 'ready', 'failed', 'not_applicable')",
            name="ck_episode_admission_facts_preview_status",
        ),
        CheckConstraint(
            "output_verification_status IN ('pending', 'running', 'verified', 'failed', 'not_applicable')",
            name="ck_episode_admission_facts_output_status",
        ),
        CheckConstraint(
            "integrity_source IN ('client', 'server')",
            name="ck_episode_admission_facts_integrity_source",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    episode_id = Column(Integer, ForeignKey("episodes.id"), nullable=False)
    source_fingerprint = Column(String(64), nullable=False, default="")
    attempt = Column(Integer, nullable=False)
    is_current = Column(Boolean, nullable=False, default=True)
    validation_policy_version = Column(String(128), nullable=False, default="")
    integrity_status = Column(String(24), nullable=False, default="pending")
    preview_status = Column(String(24), nullable=False, default="pending")
    output_verification_status = Column(String(24), nullable=False, default="pending")
    qrdf_profile = Column(String(128), nullable=False, default="")
    report_ref_json = Column(JsonDocument, nullable=False, default=dict)
    objects_json = Column(JsonDocument, nullable=False, default=list)
    integrity_source = Column(String(16), nullable=False, default="server", server_default="server")
    error_code = Column(String(64), nullable=False, default="")
    error_message = Column(Text, nullable=False, default="")
    checked_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
