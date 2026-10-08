"""PostgreSQL ORM for the Batch / Episode domain.

The business reset migration deliberately retains only identity and workspace
records.  This module contains no legacy Task, QRDF, or EGO compatibility
models so an unregistered legacy endpoint cannot accidentally write to the new
schema.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker, synonym

from data.config import settings


class Base(DeclarativeBase):
    pass


JsonDocument = JSON().with_variant(JSONB(), "postgresql")

JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"
JOB_STATUS_RETRY_PENDING = "retry_pending"
JOB_TERMINAL_STATUSES = frozenset({JOB_STATUS_SUCCEEDED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED})


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    role = Column(String(32), nullable=False, default="viewer")
    is_active = Column(Boolean, nullable=False, default=True)
    browser_session_epoch = Column(Integer, nullable=False, default=0)
    realtime_session_epoch = Column(Integer, nullable=False, default=0)
    must_change_password = Column(Boolean, nullable=False, default=False)
    password_changed_at = Column(DateTime, nullable=True)
    bootstrap_created_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    @property
    def display_name(self) -> str:
        return self.email.split("@")[0]

    @property
    def active(self) -> bool:
        return bool(self.is_active)


class Workspace(Base):
    __tablename__ = "workspaces"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=False, default="")
    creator = Column(String(128), nullable=False, default="")
    realtime_version = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    task_sets = relationship("TaskSet", back_populates="workspace")
    projects = synonym("task_sets")


Index("uq_workspaces_normalized_name", func.lower(func.btrim(Workspace.name)), unique=True)


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member_user"),
        Index("ix_workspace_members_user_workspace", "user_id", "workspace_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class ExternalProjectRef(Base):
    """Read-only local projection of an optional external business project."""

    __tablename__ = "external_project_refs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'unavailable')", name="ck_external_project_refs_status"
        ),
        CheckConstraint(
            "octet_length(metadata_json::text) <= 16384",
            name="ck_external_project_refs_metadata_size",
        ),
        UniqueConstraint(
            "workspace_id",
            "provider",
            "external_project_id",
            name="uq_external_project_refs_identity",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_external_project_refs_workspace_id"),
        Index("ix_external_project_refs_workspace_status", "workspace_id", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    provider = Column(String(64), nullable=False)
    external_project_id = Column(String(256), nullable=False)
    display_name = Column(String(256), nullable=False, default="")
    status = Column(String(32), nullable=False, default="active")
    synced_at = Column(DateTime, nullable=True)
    metadata_json = Column(JsonDocument, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class TaskSet(Base):
    __tablename__ = "task_sets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "external_project_ref_id"],
            ["external_project_refs.workspace_id", "external_project_refs.id"],
            name="fk_task_sets_external_project_ref_scope",
            ondelete="RESTRICT",
        ),
        Index("ix_task_sets_external_project_ref_id", "external_project_ref_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=False, default="")
    scene = Column(String(64), nullable=False, default="")
    external_project_ref_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    workspace = relationship("Workspace", back_populates="task_sets")
    external_project = relationship("ExternalProjectRef", foreign_keys=[external_project_ref_id])
    batches = relationship("Batch", back_populates="task_set")
    episodes = relationship("Episode", back_populates="task_set")
    source_imports = relationship("TaskSetSourceImport", back_populates="task_set")


Index(
    "uq_task_sets_workspace_normalized_name",
    TaskSet.workspace_id,
    func.lower(func.btrim(TaskSet.name)),
    unique=True,
)


# Legacy worker handlers can resume durable pre-migration JobRuns after an
# upgrade. These Python-only aliases never restore the removed Project API.
Project = TaskSet


class ExternalOssImportScope(Base):
    """Admin-managed allowlist for reads from an external OSS source bucket."""

    __tablename__ = "external_oss_import_scopes"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_external_oss_import_scopes_revision"),
        UniqueConstraint(
            "workspace_id", "task_set_id", "bucket", name="uq_external_oss_import_scopes_target"
        ),
        Index(
            "ix_external_oss_import_scopes_task_set_scope",
            "workspace_id",
            "task_set_id",
            "is_enabled",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    task_set_id = Column(Integer, ForeignKey("task_sets.id"), nullable=False)
    bucket = Column(String(63), nullable=False)
    prefixes_json = Column(JsonDocument, nullable=False, default=list)
    is_enabled = Column(Boolean, nullable=False, default=True)
    revision = Column(Integer, nullable=False, default=1)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    task_set = relationship("TaskSet")
    project_id = synonym("task_set_id")
    project = synonym("task_set")


class PlatformAiSetting(Base):
    """Singleton platform AI policy; secret values are encrypted envelopes."""

    __tablename__ = "platform_ai_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_platform_ai_settings_singleton"),
        CheckConstraint("revision > 0", name="ck_platform_ai_settings_revision"),
        CheckConstraint(
            "signed_url_ttl_seconds >= 60 AND signed_url_ttl_seconds <= 3600",
            name="ck_platform_ai_settings_url_ttl",
        ),
    )

    id = Column(Integer, primary_key=True, default=1)
    enabled = Column(Boolean, nullable=False, default=False)
    outbound_enabled = Column(Boolean, nullable=False, default=False)
    signed_url_delivery_enabled = Column(Boolean, nullable=False, default=False)
    public_endpoint = Column(String(512), nullable=False, default="")
    signed_url_ttl_seconds = Column(Integer, nullable=False, default=900)
    provider_user_id = Column(String(128), nullable=False, default="")
    provider_device_uuid = Column(String(128), nullable=False, default="")
    annotation_version = Column(String(128), nullable=False, default="vla-anno#A2FM#AT9J")
    api_key_envelope_json = Column(JsonDocument, nullable=True)
    app_id_envelope_json = Column(JsonDocument, nullable=True)
    revision = Column(Integer, nullable=False, default=1)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class Embodiment(Base):
    __tablename__ = "embodiments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(64), nullable=False, unique=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=False, default="")
    metadata_json = Column(JsonDocument, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class TaskLabel(Base):
    __tablename__ = "task_labels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(128), nullable=False, unique=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=False, default="")
    metadata_json = Column(JsonDocument, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class PersonnelProfile(Base):
    """A global collector identity, separate from login accounts.

    Offline imports can have incomplete or untrusted capture metadata.  The
    profile is therefore intentionally separate from ``User`` and can be
    selected later by annotation or review.  Attribution history is stored in
    ``EpisodeCollectorAttribution`` rather than mutating this record onto every
    derived Episode.
    """

    __tablename__ = "personnel_profiles"
    __table_args__ = (
        UniqueConstraint("profile_key", name="uq_personnel_profiles_profile_key"),
        CheckConstraint(
            "profile_key ~ '^[0-9]+$' AND profile_key::numeric > 0",
            name="ck_personnel_profiles_profile_key",
        ),
        Index("ix_personnel_profiles_active", "is_active"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Kept nullable as a legacy origin field; membership is authoritative.
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=True)
    name = Column(String(128), nullable=False)
    profile_key = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    metadata_json = Column(JsonDocument, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class WorkspacePersonnelProfile(Base):
    """Membership of one global collector in a workspace."""

    __tablename__ = "workspace_personnel_profiles"
    __table_args__ = (Index("ix_workspace_personnel_profiles_workspace", "workspace_id"),)

    workspace_id = Column(Integer, ForeignKey("workspaces.id"), primary_key=True)
    personnel_profile_id = Column(Integer, ForeignKey("personnel_profiles.id"), primary_key=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class CollectionDevice(Base):
    """Workspace-local primary capture device or collection station."""

    __tablename__ = "collection_devices"
    __table_args__ = (Index("ix_collection_devices_workspace_active", "workspace_id", "is_active"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    name = Column(String(128), nullable=False)
    device_type = Column(String(64), nullable=False)
    model = Column(String(128), nullable=False, default="")
    serial_number = Column(String(128), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    metadata_json = Column(JsonDocument, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


Index(
    "uq_collection_devices_workspace_normalized_serial",
    CollectionDevice.workspace_id,
    func.upper(func.btrim(CollectionDevice.serial_number)),
    unique=True,
)


class TaskSetSourceImport(Base):
    """TaskSet-level source ledger used by historical and recurring scans.

    The source locator is server-only evidence.  API projections can expose
    the safe display fields and import state, but never the OSS object key or
    any storage credential embedded in the locator.
    """

    __tablename__ = "task_set_source_imports"
    __table_args__ = (
        CheckConstraint(
            "status IN ('discovered', 'importing', 'imported', 'failed')",
            name="ck_task_set_source_imports_status",
        ),
        UniqueConstraint(
            "task_set_id", "source_fingerprint", name="uq_task_set_source_imports_scope_fingerprint"
        ),
        Index(
            "ix_task_set_source_imports_task_set_status_ended",
            "task_set_id",
            "status",
            "captured_ended_at",
        ),
        Index("ix_task_set_source_imports_import_session", "import_session_id"),
        Index(
            "uq_task_set_source_imports_capture_episode_id",
            "task_set_id",
            "source_kind",
            "display_name",
            unique=True,
            postgresql_where=text("source_kind = 'capture_oss'"),
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_set_id = Column(Integer, ForeignKey("task_sets.id"), nullable=False)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=True)
    import_session_id = Column(String(36), ForeignKey("import_sessions.id"), nullable=True)
    source_episode_id = Column(Integer, ForeignKey("episodes.id"), nullable=True)
    source_fingerprint = Column(String(64), nullable=False)
    source_kind = Column(String(32), nullable=False, default="ego_oss")
    display_name = Column(String(256), nullable=False, default="")
    source_locator_json = Column(JsonDocument, nullable=False, default=dict)
    status = Column(String(32), nullable=False, default="discovered")
    captured_ended_at = Column(DateTime, nullable=True)
    error_code = Column(String(64), nullable=False, default="")
    error_message = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    task_set = relationship("TaskSet", back_populates="source_imports")
    project_id = synonym("task_set_id")
    project = synonym("task_set")
    batch = relationship("Batch")
    import_session = relationship("ImportSession")
    source_episode = relationship("Episode")


ProjectSourceImport = TaskSetSourceImport


class Batch(Base):
    __tablename__ = "batches"
    __table_args__ = (
        CheckConstraint("batch_type IN ('ego', 'teleop', 'lerobot')", name="ck_batches_type"),
        CheckConstraint(
            "status IN ('created', 'importing', 'processing', 'ready', 'partial_failed', 'failed', 'cancelled')",
            name="ck_batches_status",
        ),
        Index("ix_batches_workspace_task_set_created", "workspace_id", "task_set_id", "created_at"),
        Index("ix_batches_task_set_status", "task_set_id", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    task_set_id = Column(Integer, ForeignKey("task_sets.id"), nullable=False)
    name = Column(String(256), nullable=False)
    batch_type = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False, default="created")
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    metadata_json = Column(JsonDocument, nullable=False, default=dict)
    realtime_version = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    task_set = relationship("TaskSet", back_populates="batches")
    project_id = synonym("task_set_id")
    project = synonym("task_set")
    logs = relationship("BatchLog", back_populates="batch", order_by="BatchLog.created_at")
    import_sessions = relationship("ImportSession", back_populates="batch")
    episodes = relationship("Episode", back_populates="batch")
    native_lerobot_datasets = relationship(
        "NativeLerobotDataset",
        back_populates="batch",
        order_by="NativeLerobotDataset.id",
    )
    native_lerobot_import_sessions = relationship(
        "NativeLerobotImportSession",
        back_populates="batch",
        order_by="NativeLerobotImportSession.created_at",
    )


class BatchLog(Base):
    __tablename__ = "batch_logs"
    __table_args__ = (Index("ix_batch_logs_batch_created", "batch_id", "created_at"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    action = Column(String(64), nullable=False)
    from_status = Column(String(32), nullable=False, default="")
    to_status = Column(String(32), nullable=False, default="")
    actor_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    note = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="logs")


class NativeLerobotDataset(Base):
    """A platform-owned native LeRobot copy, never a QRDF DatasetRevision.

    ``source_oss_uri`` remains audit-only.  ``oss_uri`` is the platform target
    and is deliberately nullable while an historical record awaits explicit
    reauthorization and backfill.  The row intentionally has no relationship
    to ImportSession, Episode, WorkItem, or DatasetItem: native LeRobot content
    does not enter the QRDF workflow merely because it is catalogued here.
    The database requires its batch and optional native import session to share
    the same LeRobot workspace/task-set scope.
    """

    __tablename__ = "native_lerobot_datasets"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'archived')", name="ck_native_lerobot_datasets_status"
        ),
        CheckConstraint("file_count >= 0", name="ck_native_lerobot_datasets_file_count"),
        CheckConstraint("total_size >= 0", name="ck_native_lerobot_datasets_total_size"),
        CheckConstraint(
            "source_oss_uri LIKE 'oss://%'",
            name="ck_native_lerobot_datasets_source_oss_uri",
        ),
        CheckConstraint(
            "oss_uri IS NULL OR oss_uri LIKE 'oss://%'",
            name="ck_native_lerobot_datasets_oss_uri",
        ),
        CheckConstraint(
            "copy_status IN ('queued', 'running', 'succeeded', 'failed', 'backfill_pending')",
            name="ck_native_lerobot_datasets_copy_status",
        ),
        CheckConstraint(
            "(source_scope_id IS NULL) = (source_scope_revision IS NULL)",
            name="ck_native_lerobot_datasets_source_scope_snapshot",
        ),
        UniqueConstraint(
            "workspace_id",
            "source_oss_uri",
            "marker_sha256",
            name="uq_native_lerobot_datasets_workspace_source_marker",
        ),
        UniqueConstraint(
            "workspace_id",
            "oss_uri",
            name="uq_native_lerobot_datasets_workspace_oss_uri",
        ),
        Index(
            "ix_native_lerobot_datasets_scope_created",
            "workspace_id",
            "task_set_id",
            "created_at",
        ),
        Index("ix_native_lerobot_datasets_batch", "batch_id", "id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    task_set_id = Column(Integer, ForeignKey("task_sets.id"), nullable=False)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    import_session_id = Column(
        String(36),
        ForeignKey("native_lerobot_import_sessions.id"),
        nullable=True,
    )
    name = Column(String(256), nullable=False)
    description = Column(Text, nullable=False, default="")
    source_oss_uri = Column(String(1024), nullable=False)
    source_scope_id = Column(Integer, ForeignKey("external_oss_import_scopes.id"), nullable=True)
    source_scope_revision = Column(Integer, nullable=True)
    oss_uri = Column(String(1024), nullable=True)
    robot_type = Column(String(64), nullable=False)
    dataset_id = Column(String(256), nullable=False)
    file_count = Column(Integer, nullable=False)
    total_size = Column(BigInteger, nullable=False)
    completed_at = Column(DateTime, nullable=False)
    manifest_sha256 = Column(String(64), nullable=False)
    marker_sha256 = Column(String(64), nullable=False)
    objects_json = Column(JsonDocument, nullable=False, default=list)
    status = Column(String(16), nullable=False, default="active")
    copy_status = Column(String(24), nullable=False, default="queued")
    last_copy_job_id = Column(String(36), nullable=True)
    copy_started_at = Column(DateTime, nullable=True)
    copy_finished_at = Column(DateTime, nullable=True)
    copy_error_code = Column(String(64), nullable=False, default="")
    copy_error_message = Column(Text, nullable=False, default="")
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    batch = relationship("Batch", back_populates="native_lerobot_datasets")
    import_session = relationship(
        "NativeLerobotImportSession", back_populates="native_lerobot_datasets"
    )
    task_set = relationship("TaskSet")
    source_scope = relationship("ExternalOssImportScope")
    bundles = relationship(
        "NativeLerobotBundle",
        back_populates="native_lerobot_dataset",
        order_by="NativeLerobotBundle.created_at",
    )
    project_id = synonym("task_set_id")
    project = synonym("task_set")


class NativeLerobotBundle(Base):
    """A bounded, temporary ZIP derived only from a successful platform copy."""

    __tablename__ = "native_lerobot_bundles"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'expired')",
            name="ck_native_lerobot_bundles_status",
        ),
        CheckConstraint(
            "bundle_uri IS NULL OR bundle_uri LIKE 'oss://%'",
            name="ck_native_lerobot_bundles_bundle_uri",
        ),
        UniqueConstraint(
            "native_lerobot_dataset_id",
            "marker_sha256",
            name="uq_native_lerobot_bundles_dataset_marker",
        ),
        Index(
            "ix_native_lerobot_bundles_expiry",
            "status",
            "expires_at",
            "id",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    native_lerobot_dataset_id = Column(
        Integer,
        ForeignKey("native_lerobot_datasets.id"),
        nullable=False,
    )
    marker_sha256 = Column(String(64), nullable=False)
    status = Column(String(24), nullable=False, default="queued")
    job_id = Column(String(36), nullable=True)
    bundle_uri = Column(String(1024), nullable=True)
    expires_at = Column(DateTime, nullable=True)
    error_code = Column(String(64), nullable=False, default="")
    error_message = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    native_lerobot_dataset = relationship("NativeLerobotDataset", back_populates="bundles")


class NativeLerobotScanSnapshot(Base):
    __tablename__ = "native_lerobot_scan_snapshots"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'superseded')",
            name="ck_native_lerobot_scan_snapshots_status",
        ),
        CheckConstraint(
            "candidate_count >= 0 AND valid_count >= 0 AND invalid_count >= 0",
            name="ck_native_lerobot_scan_snapshots_counts",
        ),
        Index(
            "ix_native_lerobot_scan_snapshots_scope_created",
            "workspace_id",
            "task_set_id",
            "created_at",
        ),
        Index("ix_native_lerobot_scan_snapshots_status", "status", "created_at", "id"),
        Index(
            "uq_native_lerobot_scan_snapshots_current",
            "workspace_id",
            "task_set_id",
            unique=True,
            postgresql_where=text("is_current AND status = 'succeeded'"),
        ),
    )

    id = Column(String(36), primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    task_set_id = Column(Integer, ForeignKey("task_sets.id"), nullable=False)
    requested_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    job_id = Column(String(36), nullable=True)
    status = Column(String(32), nullable=False, default="queued")
    is_current = Column(Boolean, nullable=False, default=False)
    candidate_count = Column(Integer, nullable=False, default=0)
    valid_count = Column(Integer, nullable=False, default=0)
    invalid_count = Column(Integer, nullable=False, default=0)
    error_code = Column(String(64), nullable=False, default="")
    realtime_version = Column(Integer, nullable=False, default=0)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    workspace = relationship("Workspace")
    task_set = relationship("TaskSet")
    requested_by_user = relationship("User")
    candidates = relationship(
        "NativeLerobotScanCandidate",
        back_populates="scan_snapshot",
        order_by="NativeLerobotScanCandidate.created_at",
    )
    import_sessions = relationship("NativeLerobotImportSession", back_populates="scan_snapshot")


class NativeLerobotImportSession(Base):
    """A LeRobot-only import audit record with database-enforced batch and snapshot scope."""

    __tablename__ = "native_lerobot_import_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'submitting', 'completed', 'completed_with_skips', 'failed', 'cancelled')",
            name="ck_native_lerobot_import_sessions_status",
        ),
        CheckConstraint(
            "selected_count >= 0 AND registered_count >= 0 AND skipped_count >= 0",
            name="ck_native_lerobot_import_sessions_counts",
        ),
        Index("ix_native_lerobot_import_sessions_batch_created", "batch_id", "created_at"),
        Index("ix_native_lerobot_import_sessions_snapshot", "scan_snapshot_id", "created_at"),
        Index(
            "ix_native_lerobot_import_sessions_scope_status",
            "workspace_id",
            "task_set_id",
            "status",
        ),
        UniqueConstraint(
            "submit_request_id", name="uq_native_lerobot_import_sessions_submit_request"
        ),
    )

    id = Column(String(36), primary_key=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    task_set_id = Column(Integer, ForeignKey("task_sets.id"), nullable=False)
    scan_snapshot_id = Column(
        String(36), ForeignKey("native_lerobot_scan_snapshots.id"), nullable=True
    )
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    status = Column(String(32), nullable=False, default="draft")
    submit_request_id = Column(String(36), nullable=True)
    selected_count = Column(Integer, nullable=False, default=0)
    registered_count = Column(Integer, nullable=False, default=0)
    skipped_count = Column(Integer, nullable=False, default=0)
    error_code = Column(String(64), nullable=False, default="")
    realtime_version = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    batch = relationship("Batch", back_populates="native_lerobot_import_sessions")
    workspace = relationship("Workspace")
    task_set = relationship("TaskSet")
    scan_snapshot = relationship("NativeLerobotScanSnapshot", back_populates="import_sessions")
    created_by_user = relationship("User")
    selections = relationship(
        "NativeLerobotImportSelection",
        back_populates="import_session",
        order_by="NativeLerobotImportSelection.created_at",
    )
    native_lerobot_datasets = relationship("NativeLerobotDataset", back_populates="import_session")


class NativeLerobotScanCandidate(Base):
    __tablename__ = "native_lerobot_scan_candidates"
    __table_args__ = (
        CheckConstraint(
            "status IN ('valid', 'invalid')",
            name="ck_native_lerobot_scan_candidates_status",
        ),
        CheckConstraint("file_count >= 0", name="ck_native_lerobot_scan_candidates_file_count"),
        CheckConstraint("total_size >= 0", name="ck_native_lerobot_scan_candidates_total_size"),
        UniqueConstraint(
            "scan_snapshot_id",
            "marker_sha256",
            name="uq_native_lerobot_scan_candidates_snapshot_marker",
        ),
        Index(
            "ix_native_lerobot_scan_candidates_snapshot_status", "scan_snapshot_id", "status", "id"
        ),
    )

    id = Column(String(36), primary_key=True)
    scan_snapshot_id = Column(
        String(36), ForeignKey("native_lerobot_scan_snapshots.id"), nullable=False
    )
    source_scope_id = Column(Integer, ForeignKey("external_oss_import_scopes.id"), nullable=True)
    source_scope_revision = Column(Integer, nullable=True)
    source_bucket = Column(String(128), nullable=False, default="")
    marker_key = Column(String(1024), nullable=False, default="")
    robot_type = Column(String(64), nullable=False)
    dataset_id = Column(String(256), nullable=False)
    file_count = Column(Integer, nullable=False)
    total_size = Column(BigInteger, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    manifest_sha256 = Column(String(64), nullable=False, default="")
    marker_sha256 = Column(String(64), nullable=False)
    locator_json = Column(JsonDocument, nullable=False, default=dict)
    status = Column(String(16), nullable=False, default="valid")
    error_code = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    scan_snapshot = relationship("NativeLerobotScanSnapshot", back_populates="candidates")
    source_scope = relationship("ExternalOssImportScope")
    selections = relationship("NativeLerobotImportSelection", back_populates="scan_candidate")


class NativeLerobotImportSelection(Base):
    """Immutable session/candidate result constrained to one scope and source identity."""

    __tablename__ = "native_lerobot_import_selections"
    __table_args__ = (
        CheckConstraint(
            "result_status IN ('registered', 'skipped_duplicate', 'skipped_stale', 'skipped_invalid', 'skipped_forbidden')",
            name="ck_native_lerobot_import_selections_result_status",
        ),
        UniqueConstraint(
            "import_session_id",
            "scan_candidate_id",
            name="uq_native_lerobot_import_selections_session_candidate",
        ),
        Index("ix_native_lerobot_import_selections_session", "import_session_id", "created_at"),
        Index("ix_native_lerobot_import_selections_candidate", "scan_candidate_id", "created_at"),
    )

    id = Column(String(36), primary_key=True)
    import_session_id = Column(
        String(36), ForeignKey("native_lerobot_import_sessions.id"), nullable=False
    )
    scan_candidate_id = Column(
        String(36), ForeignKey("native_lerobot_scan_candidates.id"), nullable=False
    )
    native_lerobot_dataset_id = Column(
        Integer, ForeignKey("native_lerobot_datasets.id"), nullable=True
    )
    result_status = Column(String(32), nullable=False)
    error_code = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    import_session = relationship("NativeLerobotImportSession", back_populates="selections")
    scan_candidate = relationship("NativeLerobotScanCandidate", back_populates="selections")
    native_lerobot_dataset = relationship("NativeLerobotDataset")


class ImportSession(Base):
    __tablename__ = "import_sessions"
    __table_args__ = (
        CheckConstraint(
            "import_type IN ('chunked_upload', 'oss_scan', 'filesystem_scan', 'duance_episode')",
            name="ck_import_sessions_type",
        ),
        CheckConstraint(
            "status IN ('init', 'uploading', 'uploaded', 'parsing', 'succeeded', 'failed', 'cancelled', 'superseded')",
            name="ck_import_sessions_status",
        ),
        Index("ix_import_sessions_batch_created", "batch_id", "created_at"),
        Index("ix_import_sessions_status", "status"),
        Index("ix_import_sessions_retention", "status", "updated_at", "id"),
    )

    id = Column(String(36), primary_key=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    import_type = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False, default="init")
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    # The user-selected default copied into every source Episode parsed from
    # this import.  It remains nullable at the database layer only so an UAT
    # upgrade can preserve historical rows; all new sessions are validated by
    # the import-session service.
    task_label_id = Column(Integer, ForeignKey("task_labels.id"), nullable=True)
    default_collector_profile_id = Column(
        Integer, ForeignKey("personnel_profiles.id"), nullable=True
    )
    default_collection_device_id = Column(
        Integer, ForeignKey("collection_devices.id"), nullable=True
    )
    original_name = Column(String(256), nullable=False, default="")
    source_fingerprint = Column(String(64), nullable=False, default="")
    result_json = Column(JsonDocument, nullable=False, default=dict)
    # Server-owned scan state. Continuation tokens must never reach browser
    # projections because they expose provider listing topology.
    source_date_from = Column(Date, nullable=True)
    source_date_to_exclusive = Column(Date, nullable=True)
    scan_cursor_json = Column(JsonDocument, nullable=False, default=dict)
    retention_until = Column(DateTime, nullable=True)
    realtime_version = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    batch = relationship("Batch", back_populates="import_sessions")
    task_label = relationship("TaskLabel")
    default_collector_profile = relationship("PersonnelProfile")
    default_collection_device = relationship("CollectionDevice")
    attempts = relationship("ImportAttempt", back_populates="import_session")
    candidates = relationship("ImportCandidate", back_populates="import_session")
    episodes = relationship("Episode", back_populates="import_session")
    artifacts = relationship("EpisodeArtifact", back_populates="import_session")


class ImportAttempt(Base):
    __tablename__ = "import_attempts"
    __table_args__ = (
        UniqueConstraint("attempt_token", name="uq_import_attempts_token"),
        Index("ix_import_attempts_session_status", "import_session_id", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    import_session_id = Column(String(36), ForeignKey("import_sessions.id"), nullable=False)
    attempt_token = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, default="active")
    result_json = Column(JsonDocument, nullable=False, default=dict)
    error_code = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    import_session = relationship("ImportSession", back_populates="attempts")


class ImportCandidate(Base):
    """A server-discovered source selectable by one import session only.

    ``locator_json`` remains internal because it can contain an external OSS
    key or a server-local inbox identity. Browser projections expose only the
    opaque candidate id and human-safe filename/size fields.
    """

    __tablename__ = "import_candidates"
    __table_args__ = (
        CheckConstraint(
            "candidate_type IN ('oss_object', 'filesystem_file', 'ego_episode_oss', 'capture_episode_oss')",
            name="ck_import_candidates_type",
        ),
        CheckConstraint(
            "status IN ('discovered', 'consumed', 'rejected')",
            name="ck_import_candidates_status",
        ),
        CheckConstraint(
            "source_group_status IN ('valid', 'missing', 'invalid', 'legacy')",
            name="ck_import_candidates_source_group_status",
        ),
        CheckConstraint(
            "collector_hint_status IN ('valid', 'missing', 'invalid')",
            name="ck_import_candidates_collector_hint_status",
        ),
        UniqueConstraint(
            "import_session_id",
            "source_fingerprint",
            name="uq_import_candidates_session_fingerprint",
        ),
        Index("ix_import_candidates_session_status", "import_session_id", "status"),
        Index(
            "ix_import_candidates_session_source_group",
            "import_session_id",
            "source_group_key",
            "id",
        ),
        Index(
            "ix_import_candidates_session_source_group_status",
            "import_session_id",
            "source_group_status",
            "id",
        ),
    )

    id = Column(String(36), primary_key=True)
    import_session_id = Column(String(36), ForeignKey("import_sessions.id"), nullable=False)
    task_set_source_import_id = Column(
        Integer, ForeignKey("task_set_source_imports.id"), nullable=True
    )
    candidate_type = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False, default="discovered")
    original_name = Column(String(256), nullable=False)
    size_bytes = Column(BigInteger, nullable=False, default=0)
    source_fingerprint = Column(String(64), nullable=False)
    locator_json = Column(JsonDocument, nullable=False, default=dict)
    source_group_key = Column(String(80), nullable=True)
    source_group_name = Column(String(256), nullable=True)
    source_group_status = Column(String(16), nullable=False, default="missing")
    reported_collector_identifier = Column(String(128), nullable=True)
    collector_hint_status = Column(String(16), nullable=False, default="missing")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    import_session = relationship("ImportSession", back_populates="candidates")
    task_set_source_import = relationship("TaskSetSourceImport")
    project_source_import_id = synonym("task_set_source_import_id")
    project_source_import = synonym("task_set_source_import")


class Episode(Base):
    __tablename__ = "episodes"
    __table_args__ = (
        CheckConstraint("kind IN ('source', 'derived')", name="ck_episodes_kind"),
        CheckConstraint(
            "(kind = 'source' AND parent_episode_id IS NULL AND source_start_ns IS NULL AND source_end_ns IS NULL) "
            "OR (kind = 'derived' AND parent_episode_id IS NOT NULL AND source_start_ns IS NOT NULL "
            "AND source_end_ns IS NOT NULL AND source_start_ns < source_end_ns)",
            name="ck_episodes_derivation_shape",
        ),
        UniqueConstraint("episode_uid", name="uq_episodes_uid"),
        UniqueConstraint(
            "parent_episode_id",
            "derivation_version",
            "source_start_ns",
            "source_end_ns",
            name="uq_episodes_derived_interval",
        ),
        Index("ix_episodes_batch_status", "batch_id", "workflow_status"),
        Index(
            "ix_episodes_workspace_task_set_created", "workspace_id", "task_set_id", "created_at"
        ),
        Index("ix_episodes_scope_created_id", "workspace_id", "task_set_id", "created_at", "id"),
        Index("ix_episodes_workspace_updated_id", "workspace_id", "updated_at", "id"),
        Index("ix_episodes_updated_id", "updated_at", "id"),
        Index(
            "ix_episodes_filter_dimensions",
            "modality",
            "embodiment_id",
            "task_label_id",
            "review_status",
        ),
        Index(
            "ix_episodes_workspace_source_group", "workspace_id", "reported_source_group_key", "id"
        ),
        CheckConstraint(
            "reported_source_group_status IN ('valid', 'missing', 'invalid', 'legacy')",
            name="ck_episodes_reported_source_group_status",
        ),
        CheckConstraint(
            "validity_status IN ('valid', 'intake_rejected', 'qc_dropped')",
            name="ck_episodes_validity_status",
        ),
        CheckConstraint(
            "(data_package_id IS NOT NULL AND task_set_id IS NULL AND batch_id IS NULL) "
            "OR (data_package_id IS NULL AND task_set_id IS NOT NULL AND batch_id IS NOT NULL)",
            name="ck_episodes_collection_or_legacy_scope",
        ),
        Index("ix_episodes_data_package_id", "data_package_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    episode_uid = Column(String(64), nullable=False)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    task_set_id = Column(Integer, ForeignKey("task_sets.id"), nullable=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=True)
    import_session_id = Column(String(36), ForeignKey("import_sessions.id"), nullable=True)
    kind = Column(String(16), nullable=False)
    parent_episode_id = Column(Integer, ForeignKey("episodes.id"), nullable=True)
    derivation_version = Column(Integer, nullable=False, default=0)
    modality = Column(String(32), nullable=False)
    embodiment_id = Column(Integer, ForeignKey("embodiments.id"), nullable=True)
    task_label_id = Column(Integer, ForeignKey("task_labels.id"), nullable=True)
    scene = Column(String(64), nullable=False, default="")
    source_fingerprint = Column(String(64), nullable=False, default="")
    source_start_ns = Column(BigInteger, nullable=True)
    source_end_ns = Column(BigInteger, nullable=True)
    workflow_status = Column(String(32), nullable=False, default="discovered")
    quality_status = Column(String(32), nullable=False, default="pending")
    annotation_status = Column(String(32), nullable=False, default="pending")
    review_status = Column(String(32), nullable=False, default="pending")
    validity_status = Column(String(32), nullable=False, default="valid")
    data_package_id = Column(Integer, ForeignKey("data_packages.id"), nullable=True)
    task_language = Column(Text, nullable=False, default="")
    metadata_json = Column(JsonDocument, nullable=False, default=dict)
    reported_source_group_key = Column(String(80), nullable=True)
    reported_source_group_name = Column(String(256), nullable=True)
    reported_source_group_status = Column(String(16), nullable=False, default="missing")
    realtime_version = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    task_set = relationship("TaskSet", back_populates="episodes")
    project_id = synonym("task_set_id")
    project = synonym("task_set")
    batch = relationship("Batch", back_populates="episodes")
    import_session = relationship("ImportSession", back_populates="episodes")
    task_label = relationship("TaskLabel")
    parent = relationship("Episode", remote_side=[id], back_populates="derived_episodes")
    derived_episodes = relationship("Episode", back_populates="parent", order_by="Episode.id")
    artifacts = relationship("EpisodeArtifact", back_populates="episode")
    annotations = relationship("EpisodeAnnotation", back_populates="episode")
    reviews = relationship("EpisodeReview", back_populates="episode")
    collector_attributions = relationship(
        "EpisodeCollectorAttribution",
        back_populates="root_source_episode",
        order_by="EpisodeCollectorAttribution.id",
    )
    device_attributions = relationship(
        "EpisodeDeviceAttribution",
        back_populates="root_source_episode",
        order_by="EpisodeDeviceAttribution.id",
    )


class EpisodeArtifact(Base):
    __tablename__ = "episode_artifacts"
    __table_args__ = (
        CheckConstraint(
            "episode_id IS NOT NULL OR import_session_id IS NOT NULL",
            name="ck_episode_artifacts_owner",
        ),
        CheckConstraint(
            "artifact_type IN ('import_original', 'raw_source', 'process_preview', 'official_qrdf', 'lerobot_shard')",
            name="ck_episode_artifacts_type",
        ),
        CheckConstraint(
            "storage_role IN ('raw', 'process', 'official', 'export')",
            name="ck_episode_artifacts_storage_role",
        ),
        CheckConstraint(
            "retention_policy IN ('permanent', 'temporary', 'manual_cleanup')",
            name="ck_episode_artifacts_retention_policy",
        ),
        UniqueConstraint("storage_uri", name="uq_episode_artifacts_storage_uri"),
        Index("ix_episode_artifacts_episode_type", "episode_id", "artifact_type"),
        Index("ix_episode_artifacts_import_type", "import_session_id", "artifact_type"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    episode_id = Column(Integer, ForeignKey("episodes.id"), nullable=True)
    import_session_id = Column(String(36), ForeignKey("import_sessions.id"), nullable=True)
    artifact_type = Column(String(32), nullable=False)
    storage_role = Column(String(16), nullable=False)
    storage_uri = Column(String(1024), nullable=False)
    checksum_sha256 = Column(String(64), nullable=False, default="")
    size_bytes = Column(BigInteger, nullable=False, default=0)
    manifest_hash = Column(String(64), nullable=False, default="")
    retention_policy = Column(String(16), nullable=False, default="permanent")
    retention_until = Column(DateTime, nullable=True)
    metadata_json = Column(JsonDocument, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    episode = relationship("Episode", back_populates="artifacts")
    import_session = relationship("ImportSession", back_populates="artifacts")
    operations = relationship("ArtifactOperation", back_populates="artifact")


class ArtifactOperation(Base):
    """Durable intent for one external object write.

    The row is committed before a worker talks to OSS or the local mirror. A
    retry can therefore distinguish an unfinished write from an untracked
    object without ever deleting a Batch or Episode prefix.
    """

    __tablename__ = "artifact_operations"
    __table_args__ = (
        CheckConstraint(
            "operation_kind IN ('import_original_publish', 'raw_source_publish', "
            "'process_preview_publish', 'official_publish')",
            name="ck_artifact_operations_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'published', 'cleanup_pending', 'cleaned')",
            name="ck_artifact_operations_status",
        ),
        UniqueConstraint(
            "artifact_id", "operation_kind", name="uq_artifact_operations_artifact_kind"
        ),
        Index("ix_artifact_operations_status_updated", "status", "updated_at"),
        Index("ix_artifact_operations_job", "job_id"),
    )

    id = Column(String(36), primary_key=True)
    artifact_id = Column(Integer, ForeignKey("episode_artifacts.id"), nullable=False)
    job_id = Column(String(36), ForeignKey("job_runs.id"), nullable=True)
    operation_kind = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, default="pending")
    target_uri = Column(String(1024), nullable=False)
    checksum_sha256 = Column(String(64), nullable=False, default="")
    size_bytes = Column(BigInteger, nullable=False, default=0)
    manifest_json = Column(JsonDocument, nullable=False, default=dict)
    error_code = Column(String(64), nullable=False, default="")
    error_message = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    artifact = relationship("EpisodeArtifact", back_populates="operations")


class EpisodeAnnotation(Base):
    __tablename__ = "episode_annotations"
    __table_args__ = (
        UniqueConstraint("episode_id", "version", name="uq_episode_annotations_version"),
        UniqueConstraint(
            "work_item_id", "draft_version", name="uq_episode_annotations_work_item_draft"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    episode_id = Column(Integer, ForeignKey("episodes.id"), nullable=False)
    work_item_id = Column(Integer, ForeignKey("work_items.id"), nullable=True)
    draft_version = Column(Integer, nullable=True)
    version = Column(Integer, nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    payload_json = Column(JsonDocument, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    episode = relationship("Episode", back_populates="annotations")


class EpisodeReview(Base):
    __tablename__ = "episode_reviews"
    __table_args__ = (
        CheckConstraint("decision IN ('accepted', 'rejected')", name="ck_episode_reviews_decision"),
        Index("ix_episode_reviews_episode_created", "episode_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    episode_id = Column(Integer, ForeignKey("episodes.id"), nullable=False)
    annotation_version = Column(Integer, nullable=True)
    review_target_kind = Column(String(32), nullable=True)
    review_work_item_id = Column(Integer, ForeignKey("work_items.id"), nullable=True)
    decision = Column(String(16), nullable=False)
    auditor_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    note = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    episode = relationship("Episode", back_populates="reviews")


class EpisodeCollectorAttribution(Base):
    """Append-only collector attribution for a source Episode lineage.

    ``collector_profile_id = NULL`` is the explicit ``Unknown`` choice.  The
    latest confirmed row is projected to source and derived Episode views;
    derived rows do not duplicate collector metadata.
    """

    __tablename__ = "episode_collector_attributions"
    __table_args__ = (
        CheckConstraint(
            "source IN ('machine_reported', 'offline_declared', 'offline_curated', 'online_verified')",
            name="ck_episode_collector_attributions_source",
        ),
        CheckConstraint(
            "match_status IN ('matched', 'unknown', 'unmatched')",
            name="ck_episode_collector_attributions_match_status",
        ),
        Index(
            "ix_episode_collector_attributions_source_created",
            "root_source_episode_id",
            "created_at",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    root_source_episode_id = Column(Integer, ForeignKey("episodes.id"), nullable=False)
    collector_profile_id = Column(Integer, ForeignKey("personnel_profiles.id"), nullable=True)
    source = Column(String(32), nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    annotation_version = Column(Integer, nullable=True)
    reported_identifier = Column(String(128), nullable=True)
    match_status = Column(String(16), nullable=False, default="unknown")
    note = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    root_source_episode = relationship("Episode", back_populates="collector_attributions")
    collector_profile = relationship("PersonnelProfile")


class EpisodeDeviceAttribution(Base):
    """Append-only primary device attribution for a source Episode lineage."""

    __tablename__ = "episode_device_attributions"
    __table_args__ = (
        CheckConstraint(
            "source IN ('machine_reported', 'offline_declared', 'offline_curated', 'online_verified')",
            name="ck_episode_device_attributions_source",
        ),
        CheckConstraint(
            "match_status IN ('matched', 'unknown', 'unmatched')",
            name="ck_episode_device_attributions_match_status",
        ),
        Index(
            "ix_episode_device_attributions_source_created", "root_source_episode_id", "created_at"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    root_source_episode_id = Column(Integer, ForeignKey("episodes.id"), nullable=False)
    collection_device_id = Column(Integer, ForeignKey("collection_devices.id"), nullable=True)
    source = Column(String(32), nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    annotation_version = Column(Integer, nullable=True)
    reported_identifier = Column(String(128), nullable=True)
    match_status = Column(String(16), nullable=False, default="unknown")
    note = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    root_source_episode = relationship("Episode", back_populates="device_attributions")
    collection_device = relationship("CollectionDevice")


class PublishedEpisode(Base):
    __tablename__ = "published_episodes"
    __table_args__ = (
        UniqueConstraint("episode_id", name="uq_published_episodes_episode"),
        UniqueConstraint(
            "episode_id", "official_artifact_id", name="uq_published_episodes_artifact"
        ),
        Index("ix_published_episodes_episode_published", "episode_id", "published_at"),
        Index("ix_published_episodes_published_id", "published_at", "id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    episode_id = Column(Integer, ForeignKey("episodes.id"), nullable=False)
    official_artifact_id = Column(Integer, ForeignKey("episode_artifacts.id"), nullable=False)
    publisher_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    output_profile = Column(String(32), nullable=False)
    manifest_hash = Column(String(64), nullable=False)
    published_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Dataset(Base):
    __tablename__ = "datasets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    revisions = relationship("DatasetRevision", back_populates="dataset")


Index(
    "uq_datasets_workspace_normalized_name",
    Dataset.workspace_id,
    func.lower(func.btrim(Dataset.name)),
    unique=True,
)
Index("ix_datasets_workspace_created_id", Dataset.workspace_id, Dataset.created_at, Dataset.id)


class DatasetRevision(Base):
    __tablename__ = "dataset_revisions"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_dataset_revisions_version"),
        UniqueConstraint("dataset_id", "version", name="uq_dataset_revisions_version"),
        UniqueConstraint(
            "dataset_id", "client_request_id", name="uq_dataset_revisions_client_request"
        ),
        Index("ix_dataset_revisions_workspace_created", "workspace_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=False)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    version = Column(Integer, nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    client_request_id = Column(String(36), nullable=True)
    request_fingerprint = Column(String(64), nullable=True)
    filter_json = Column(JsonDocument, nullable=False, default=dict)
    manifest_hash = Column(String(64), nullable=False)
    episode_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    retired_at = Column(DateTime, nullable=True)

    dataset = relationship("Dataset", back_populates="revisions")
    items = relationship("DatasetItem", back_populates="revision", order_by="DatasetItem.position")


class DatasetItem(Base):
    __tablename__ = "dataset_episode_items"
    __table_args__ = (
        CheckConstraint("position >= 0", name="ck_dataset_episode_items_position"),
        CheckConstraint(
            "item_kind IN ('episode', 'sample')",
            name="ck_dataset_episode_items_kind",
        ),
        CheckConstraint(
            "(effective_start_ns IS NULL AND effective_end_ns IS NULL) "
            "OR (effective_start_ns IS NOT NULL AND effective_end_ns IS NOT NULL "
            "AND effective_start_ns < effective_end_ns)",
            name="ck_dataset_episode_items_effective_range",
        ),
        UniqueConstraint("revision_id", "position", name="uq_dataset_episode_items_position"),
        Index("ix_dataset_episode_items_revision_split", "revision_id", "split"),
        Index("ix_dataset_episode_items_source_split", "revision_id", "source_episode_id", "split"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    revision_id = Column(Integer, ForeignKey("dataset_revisions.id"), nullable=False)
    published_episode_id = Column(Integer, ForeignKey("published_episodes.id"), nullable=False)
    position = Column(Integer, nullable=False)
    split = Column(String(32), nullable=False, default="train")
    source_snapshot_hash = Column(String(64), nullable=False)
    item_kind = Column(String(16), nullable=False, default="episode")
    sample_id = Column(String(128), nullable=True)
    annotation_revision_id = Column(Integer, nullable=True)
    annotation_segment_id = Column(String(128), nullable=True)
    source_episode_id = Column(String(128), nullable=True)
    source_fingerprint = Column(String(64), nullable=False, default="")
    core_start_ns = Column(BigInteger, nullable=True)
    core_end_ns = Column(BigInteger, nullable=True)
    effective_start_ns = Column(BigInteger, nullable=True)
    effective_end_ns = Column(BigInteger, nullable=True)
    pre_roll_s = Column(Float, nullable=False, default=0.0)
    post_roll_s = Column(Float, nullable=False, default=0.0)
    task_text = Column(Text, nullable=False, default="")
    outcome = Column(Text, nullable=False, default="")
    modality_signature = Column(String(128), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    revision = relationship("DatasetRevision", back_populates="items")

    @property
    def episode_id(self) -> int:
        """Compatibility alias for callers that still use the old name."""
        return int(self.published_episode_id)


# Source compatibility for modules that imported the pre-reset class name.
DatasetEpisodeItem = DatasetItem


class JobRun(Base):
    __tablename__ = "job_runs"
    __table_args__ = (
        CheckConstraint(
            "resource_type IN ('batch', 'episode', 'import_session', 'artifact', "
            "'dataset_revision', 'platform', 'native_lerobot_dataset', 'native_lerobot_bundle', "
            "'native_lerobot_scan_snapshot', 'native_lerobot_direct_source')",
            name="ck_job_runs_resource_type",
        ),
        UniqueConstraint("idempotency_key", name="uq_job_runs_idempotency_key"),
        Index("ix_job_runs_resource", "resource_type", "resource_id"),
        Index("ix_job_runs_status_queue", "status", "queue"),
        Index(
            "ix_job_runs_recovery_scan",
            "status",
            "recovery_dispatch_expires_at",
            "created_at",
            "id",
        ),
        Index("ix_job_runs_terminal_retention", "status", "finished_at", "id"),
        Index(
            "ix_job_runs_resource_latest",
            "kind",
            "resource_type",
            "resource_id",
            "created_at",
            "id",
        ),
    )

    id = Column(String(36), primary_key=True)
    kind = Column(String(64), nullable=False)
    resource_type = Column(String(32), nullable=False)
    resource_id = Column(String(128), nullable=False)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=True)
    task_set_id = Column(Integer, ForeignKey("task_sets.id"), nullable=True)
    project_id = synonym("task_set_id")
    idempotency_key = Column(String(255), nullable=False)
    queue = Column(String(32), nullable=False)
    actor_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    status = Column(String(32), nullable=False, default="queued")
    phase = Column(String(64), nullable=False, default="queued")
    progress_percent = Column(Integer, nullable=False, default=0)
    detail_json = Column(JsonDocument, nullable=False, default=dict)
    result_json = Column(JsonDocument, nullable=False, default=dict)
    error_code = Column(String(64), nullable=False, default="")
    error_message = Column(Text, nullable=False, default="")
    retry_count = Column(Integer, nullable=False, default=0)
    attempt_count = Column(Integer, nullable=False, default=0)
    lease_worker_id = Column(String(128), nullable=False, default="")
    lease_token = Column(String(64), nullable=False, default="")
    lease_expires_at = Column(DateTime, nullable=True)
    recovery_dispatch_token = Column(String(64), nullable=False, default="")
    recovery_dispatch_expires_at = Column(DateTime, nullable=True)
    recovery_dispatched_at = Column(DateTime, nullable=True)
    realtime_version = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class JobQueueSlot(Base):
    __tablename__ = "job_queue_slots"
    __table_args__ = (
        UniqueConstraint("queue", "slot_number", name="uq_job_queue_slot"),
        UniqueConstraint("job_id", name="uq_job_queue_slots_job_id"),
        Index("ix_job_queue_slots_queue", "queue"),
        Index("ix_job_queue_slots_lease_expires_at", "lease_expires_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    queue = Column(String(32), nullable=False)
    slot_number = Column(Integer, nullable=False)
    job_id = Column(String(36), ForeignKey("job_runs.id"), nullable=True)
    worker_id = Column(String(128), nullable=False, default="")
    lease_expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class WorkItem(Base):
    __tablename__ = "work_items"
    __table_args__ = (
        CheckConstraint(
            "batch_id IS NOT NULL OR episode_id IS NOT NULL", name="ck_work_items_subject"
        ),
        CheckConstraint(
            "review_target_kind IS NULL OR review_target_kind IN ('cut', 'annotation')",
            name="ck_work_items_review_target_kind",
        ),
        UniqueConstraint(
            "review_of_work_item_id",
            "generation",
            name="uq_work_items_review_attempt_generation",
        ),
        Index("ix_work_items_queue", "workspace_id", "kind", "status", "assignee_user_id"),
        Index(
            "ix_work_items_workspace_kind_updated_id", "workspace_id", "kind", "updated_at", "id"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=True)
    episode_id = Column(Integer, ForeignKey("episodes.id"), nullable=True)
    kind = Column(String(32), nullable=False)
    generation = Column(Integer, nullable=False, default=1)
    status = Column(String(32), nullable=False, default="pending")
    assignee_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    version = Column(Integer, nullable=False, default=1)
    draft_json = Column(JsonDocument, nullable=False, default=dict)
    draft_version = Column(Integer, nullable=False, default=0)
    review_target_kind = Column(String(32), nullable=True)
    review_of_work_item_id = Column(Integer, ForeignKey("work_items.id"), nullable=True)
    realtime_version = Column(Integer, nullable=False, default=0)
    note = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class RealtimeEvent(Base):
    """An idempotent, post-commit event for user-safe realtime projections."""

    __tablename__ = "realtime_events"
    __table_args__ = (
        CheckConstraint("resource_version > 0", name="ck_realtime_events_resource_version"),
        UniqueConstraint(
            "resource_type",
            "resource_id",
            "resource_version",
            name="uq_realtime_events_resource_version",
        ),
        Index("ix_realtime_events_pending", "published_at", "next_attempt_at", "created_at"),
        Index(
            "ix_realtime_events_published_retention",
            "published_at",
            "event_id",
            postgresql_where=text("published_at IS NOT NULL"),
        ),
    )

    event_id = Column(String(36), primary_key=True)
    resource_type = Column(String(32), nullable=False)
    resource_id = Column(String(128), nullable=False)
    resource_version = Column(Integer, nullable=False)
    event_name = Column(String(64), nullable=False)
    safe_payload = Column(JsonDocument, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    published_at = Column(DateTime, nullable=True)
    next_attempt_at = Column(DateTime, nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=False, default="")


class SecurityAuditEvent(Base):
    """Append-only, sanitized security audit facts."""

    __tablename__ = "security_audit_events"
    __table_args__ = (
        Index("ix_security_audit_events_workspace_occurred", "workspace_id", "occurred_at"),
        Index("ix_security_audit_events_actor_occurred", "actor_id", "occurred_at"),
        Index("ix_security_audit_events_action_occurred", "action", "occurred_at"),
        Index("ix_security_audit_events_resource", "resource_type", "resource_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    action = Column(String(64), nullable=False)
    actor_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=True)
    resource_type = Column(String(64), nullable=True)
    resource_id = Column(String(128), nullable=True)
    result = Column(String(32), nullable=False)
    ip_hash = Column(String(64), nullable=False, default="")
    user_agent_hash = Column(String(64), nullable=False, default="")
    occurred_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    detail_json = Column(JsonDocument, nullable=False, default=dict)
    correlation_id = Column(String(64), nullable=False, default="")


class DwdEpisodeFact(Base):
    """Episode-grain warehouse detail row for dashboard ETL."""

    __tablename__ = "dwd_episode_fact"
    __table_args__ = (
        CheckConstraint("kind IN ('source', 'derived')", name="ck_dwd_episode_fact_kind"),
        CheckConstraint(
            "pipeline_stage IN ('intake', 'collected', 'separated', 'annotated', 'stored')",
            name="ck_dwd_episode_fact_pipeline_stage",
        ),
        CheckConstraint(
            "duration_bucket IN ('lt_30s', 'bt_30_60s', 'gt_60s', 'unknown')",
            name="ck_dwd_episode_fact_duration_bucket",
        ),
        Index("ix_dwd_episode_fact_stage_bucket", "pipeline_stage", "duration_bucket"),
        Index("ix_dwd_episode_fact_countable_collected", "is_countable", "collected_on"),
        Index("ix_dwd_episode_fact_device_id", "device_id"),
        Index("ix_dwd_episode_fact_qrdf_baseline", "is_qrdf_baseline"),
        Index("ix_dwd_episode_fact_scope", "workspace_id", "task_set_id"),
        Index("ix_dwd_episode_fact_source_updated", "source_updated_at", "episode_id"),
    )

    episode_id = Column(Integer, primary_key=True)
    episode_uid = Column(String(64), nullable=False)
    kind = Column(String(16), nullable=False)
    workspace_id = Column(Integer, nullable=False)
    # Collection-package Episodes do not belong to the legacy task-set/batch
    # graph. Keep those identifiers nullable so the warehouse preserves their
    # package facts without assigning them to an unrelated legacy project.
    task_set_id = Column(Integer, nullable=True)
    batch_id = Column(Integer, nullable=True)
    scene = Column(String(64), nullable=False, default="")
    modality = Column(String(32), nullable=False)
    embodiment_id = Column(Integer, nullable=True)
    task_label_id = Column(Integer, nullable=True)
    device_id = Column(Integer, nullable=True)
    device_name = Column(String(128), nullable=False, default="")
    workflow_status = Column(String(32), nullable=False)
    quality_status = Column(String(32), nullable=False)
    annotation_status = Column(String(32), nullable=False)
    review_status = Column(String(32), nullable=False)
    pipeline_stage = Column(String(32), nullable=False)
    # Package states are warehouse projections only; terminal Episodes have no
    # queue key, even when their captured duration remains in the funnel.
    annotation_eligible = Column(Boolean, nullable=False, default=False)
    queue_key = Column(String(32), nullable=True)
    intake_valid_duration_s = Column(Float, nullable=True)
    annotation_effective_duration_ns = Column(BigInteger, nullable=True)
    projection_version = Column(Integer, nullable=False, default=1)
    source_revision = Column(String(32), nullable=True)
    duration_s = Column(Float, nullable=True)
    duration_bucket = Column(String(16), nullable=False)
    is_countable = Column(Boolean, nullable=False, default=False)
    is_qrdf_baseline = Column(Boolean, nullable=False, default=False)
    collected_on = Column(Date, nullable=True)
    created_at = Column(DateTime, nullable=False)
    published_at = Column(DateTime, nullable=True)
    source_updated_at = Column(DateTime, nullable=False)
    etl_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class DwsEpisode5m(Base):
    """Five-minute warehouse aggregates for dashboard rollups."""

    __tablename__ = "dws_episode_5m"
    __table_args__ = (
        CheckConstraint(
            "grain IN ('global', 'pipeline_duration', 'device', 'collect_daily', 'queue')",
            name="ck_dws_episode_5m_grain",
        ),
        UniqueConstraint(
            "scope_key",
            "bucket_start",
            "grain",
            "dim_key",
            "dim_value",
            name="uq_dws_episode_5m_bucket_grain_dim",
        ),
        Index("ix_dws_episode_5m_scope_bucket_grain", "scope_key", "bucket_start", "grain"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    scope_key = Column(String(64), nullable=False)
    workspace_id = Column(Integer, nullable=True)
    task_set_id = Column(Integer, nullable=True)
    bucket_start = Column(DateTime, nullable=False)
    grain = Column(String(32), nullable=False)
    dim_key = Column(String(64), nullable=False, default="")
    dim_value = Column(String(128), nullable=False, default="")
    episode_count = Column(Integer, nullable=False, default=0)
    duration_s_sum = Column(Float, nullable=True)
    qrdf_count = Column(Integer, nullable=True)
    annotated_count = Column(Integer, nullable=True)
    eligible_annotation_count = Column(Integer, nullable=True)
    etl_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AdsDashboardSnapshot(Base):
    """Latest ADS payload for one explicit dashboard scope and time bucket."""

    __tablename__ = "ads_dashboard_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "scope_key",
            "source_bucket_start",
            name="uq_ads_dashboard_snapshot_scope_bucket",
        ),
        Index(
            "ix_ads_dashboard_snapshot_scope_computed",
            "scope_key",
            "computed_at",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    scope_key = Column(String(64), nullable=False)
    workspace_id = Column(Integer, nullable=True)
    task_set_id = Column(Integer, nullable=True)
    payload_json = Column(JsonDocument, nullable=False, default=dict)
    computed_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    source_bucket_start = Column(DateTime, nullable=False)
    etl_job_id = Column(String(36), nullable=False, default="")


class AdsDailyKpi(Base):
    """Daily KPI close values used for vs-yesterday deltas."""

    __tablename__ = "ads_daily_kpi"
    __table_args__ = (
        UniqueConstraint("scope_key", "stat_date", name="uq_ads_daily_kpi_scope_date"),
        Index("ix_ads_daily_kpi_scope_date", "scope_key", "stat_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    scope_key = Column(String(64), nullable=False)
    workspace_id = Column(Integer, nullable=True)
    task_set_id = Column(Integer, nullable=True)
    stat_date = Column(Date, nullable=False)
    total_episodes = Column(Integer, nullable=False, default=0)
    total_duration_s = Column(Float, nullable=False, default=0.0)
    collected_today = Column(Integer, nullable=False, default=0)
    annotation_completed = Column(Integer, nullable=False, default=0)
    annotation_completion_rate = Column(Float, nullable=False, default=0.0)
    qrdf_baseline_count = Column(Integer, nullable=False, default=0)
    computed_at = Column(DateTime, nullable=False, default=datetime.utcnow)


engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_db() -> None:
    """Compatibility helper for isolated tooling; deployed services use Alembic."""
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Import collection domain models at the end of file so Base.metadata and mapper string
# references resolve without callers importing them individually. Placed at the end because these modules depend on Base above.
from data.models import (  # noqa: E402,F401
    annotation_work,
    catalog_dataset,
    collection_config,
    collection_core,
    collection_upload,
    data_asset,
    data_batch,
    data_package,
    episode_admission,
    native_lerobot_direct,
)
