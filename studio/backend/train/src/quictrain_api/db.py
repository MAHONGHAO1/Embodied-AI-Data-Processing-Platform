from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from quictrain_core import JobState, new_id
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .settings import get_settings


@event.listens_for(Engine, "connect")
def _set_sqlite_custom_functions(dbapi_connection, connection_record):
    if hasattr(dbapi_connection, "create_function"):
        try:
            dbapi_connection.create_function(
                "btrim", 1, lambda s: s.strip() if s is not None else None, deterministic=True
            )
        except Exception:
            pass


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class UserRecord(Base):
    __tablename__ = "users"
    __allow_unmapped__ = True

    id: Mapped[Any] = mapped_column(String(48), primary_key=True)
    email: Mapped[str] = mapped_column(String(240), unique=True)
    role: Mapped[str] = mapped_column(String(32), default="viewer")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    password_hash: Mapped[str | None] = mapped_column(String(256), nullable=True)
    bootstrap_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    _display_name: str | None = None

    @property
    def display_name(self) -> str:
        if self._display_name:
            return self._display_name
        name = self.email.split("@")[0] if self.email else ""
        return name.capitalize() if name else ""

    @display_name.setter
    def display_name(self, value: str) -> None:
        self._display_name = value

    @property
    def active(self) -> bool:
        return bool(self.is_active)

    @active.setter
    def active(self, value: bool) -> None:
        self.is_active = bool(value)


class AuthSessionRecord(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(48), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProjectRecord(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(80), unique=True)


class ProjectMembershipRecord(Base):
    __tablename__ = "project_memberships"
    __table_args__ = (UniqueConstraint("project_id", "user_id", name="uq_project_membership"),)

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(48), index=True)
    role: Mapped[str] = mapped_column(String(24), default="viewer")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProjectPolicyRecord(Base):
    __tablename__ = "project_policies"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    max_concurrent_jobs: Mapped[int] = mapped_column(Integer, default=16)
    max_gpus: Mapped[int] = mapped_column(Integer, default=16)
    max_runtime_seconds: Mapped[int] = mapped_column(Integer, default=86_400)
    provider_disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class DatasetVersionRecord(Base):
    __tablename__ = "dataset_versions"
    __table_args__ = (
        UniqueConstraint("external_dataset_id", "version", name="uq_dataset_version"),
    )

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    external_dataset_id: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(160))
    version: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24), default="READY")
    format: Mapped[str] = mapped_column(String(32), default="lerobot")
    format_version: Mapped[str] = mapped_column(String(32), default="3.0")
    uri: Mapped[str] = mapped_column(Text)
    materialized_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    checksum: Mapped[str] = mapped_column(String(96))
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelVersionRecord(Base):
    __tablename__ = "model_versions"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(80), index=True)
    display_name: Mapped[str] = mapped_column(String(160))
    version: Mapped[str] = mapped_column(String(80))
    backend: Mapped[str] = mapped_column(String(80))
    maturity: Mapped[str] = mapped_column(String(24))
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON)
    schema_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    schema_hash: Mapped[str] = mapped_column(String(96))
    deprecated: Mapped[bool] = mapped_column(Boolean, default=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResourceProfileRecord(Base):
    __tablename__ = "resource_profiles"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    model_version_id: Mapped[str] = mapped_column(ForeignKey("model_versions.id"), index=True)
    recipe_id: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(120))
    gpu_models: Mapped[list[str]] = mapped_column(JSON)
    gpu_count: Mapped[int] = mapped_column(Integer)
    vram_gb_min: Mapped[int] = mapped_column(Integer)
    calibration_status: Mapped[str] = mapped_column(String(32))
    selectable: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    warning: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_id: Mapped[str] = mapped_column(String(40), default="fake")
    pool_id: Mapped[str | None] = mapped_column(String(120), nullable=True)


class JobRecord(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("project_id", "creator_id", "client_request_id", name="uq_job_request"),
    )

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    creator_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), index=True, nullable=True
    )
    client_request_id: Mapped[str] = mapped_column(String(120))
    display_name: Mapped[str] = mapped_column(String(180))
    state: Mapped[str] = mapped_column(String(32), default=JobState.VALIDATING.value, index=True)
    stage: Mapped[str] = mapped_column(String(40), default="VALIDATE")
    queue_reason: Mapped[str | None] = mapped_column(String(240), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=50)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"))
    model_version_id: Mapped[str] = mapped_column(ForeignKey("model_versions.id"))
    model_id: Mapped[str] = mapped_column(String(80))
    recipe_id: Mapped[str] = mapped_column(String(80))
    resource_profile_id: Mapped[str] = mapped_column(String(96))
    schema_hash: Mapped[str] = mapped_column(String(96))
    config_hash: Mapped[str] = mapped_column(String(96))
    schema_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    user_overrides: Mapped[dict[str, Any]] = mapped_column(JSON)
    resolved_config: Mapped[dict[str, Any]] = mapped_column(JSON)
    source_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    latest_metrics: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    failure_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    attempts: Mapped[list[AttemptRecord]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="AttemptRecord.number"
    )
    events: Mapped[list[JobEventRecord]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobEventRecord.sequence"
    )
    artifacts: Mapped[list[ArtifactRecord]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    logs: Mapped[list[JobLogRecord]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobLogRecord.sequence"
    )


class AttemptRecord(Base):
    __tablename__ = "attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "number", name="uq_attempt_number"),
        UniqueConstraint("idempotency_key", name="uq_attempt_idempotency"),
    )

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32), default="PENDING")
    provider: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    external_job_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    provider_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    mlflow_run_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    log_cursor: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped[JobRecord] = relationship(back_populates="attempts")
    logs: Mapped[list[JobLogRecord]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan", order_by="JobLogRecord.sequence"
    )


class JobLogRecord(Base):
    __tablename__ = "job_logs"
    __table_args__ = (
        UniqueConstraint("attempt_id", "source", "provider_sequence", name="uq_job_log_source"),
        UniqueConstraint("attempt_id", "sequence", name="uq_job_log_sequence"),
    )

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("attempts.id"), index=True)
    sequence: Mapped[int] = mapped_column(BigInteger)
    provider_sequence: Mapped[int] = mapped_column(BigInteger)
    timestamp: Mapped[str] = mapped_column(String(80))
    level: Mapped[str] = mapped_column(String(24), default="INFO")
    source: Mapped[str] = mapped_column(String(240))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[JobRecord] = relationship(back_populates="logs")
    attempt: Mapped[AttemptRecord] = relationship(back_populates="logs")


class JobEventRecord(Base):
    __tablename__ = "job_events"
    __table_args__ = (UniqueConstraint("job_id", "sequence", name="uq_job_event_sequence"),)

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[JobRecord] = relationship(back_populates="events")


class ArtifactRecord(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("attempts.id"))
    name: Mapped[str] = mapped_column(String(180))
    kind: Mapped[str] = mapped_column(String(64))
    uri: Mapped[str] = mapped_column(Text)
    export_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    retention_days: Mapped[int] = mapped_column(Integer, default=30)
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[JobRecord] = relationship(back_populates="artifacts")


class MaterializationAttemptRecord(Base):
    __tablename__ = "materialization_attempts"
    __table_args__ = (
        UniqueConstraint("lease_key", name="uq_materialization_lease"),
        UniqueConstraint("dataset_version_id", "number", name="uq_materialization_attempt_number"),
    )

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32), index=True)
    lease_key: Mapped[str] = mapped_column(String(160))
    source_uri: Mapped[str] = mapped_column(Text)
    target_uri: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String(96))
    bytes_copied: Mapped[int] = mapped_column(BigInteger, default=0)
    actor_id: Mapped[str] = mapped_column(String(48))
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ArtifactExportRecord(Base):
    __tablename__ = "artifact_exports"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    attempt_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    source_uri: Mapped[str] = mapped_column(Text)
    export_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    checksum: Mapped[str] = mapped_column(String(96))
    bytes_copied: Mapped[int] = mapped_column(BigInteger, default=0)
    actor_id: Mapped[str] = mapped_column(String(48))
    retention_days: Mapped[int] = mapped_column(Integer, default=14)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditEventRecord(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(48), index=True)
    action: Mapped[str] = mapped_column(String(120), index=True)
    resource_type: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[str] = mapped_column(String(80), index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def _ensure_sqlite_dev_columns(bind) -> None:
    """Add new columns on existing local SQLite DBs (create_all does not alter)."""

    if bind.dialect.name != "sqlite":
        return
    from sqlalchemy import text

    with bind.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(resource_profiles)")).fetchall()
        if not rows:
            return
        columns = {row[1] for row in rows}
        if "provider_id" not in columns:
            conn.execute(
                text(
                    "ALTER TABLE resource_profiles "
                    "ADD COLUMN provider_id VARCHAR(40) NOT NULL DEFAULT 'fake'"
                )
            )
        if "pool_id" not in columns:
            conn.execute(text("ALTER TABLE resource_profiles ADD COLUMN pool_id VARCHAR(120)"))
        user_rows = conn.execute(text("PRAGMA table_info(users)")).fetchall()
        user_columns = {row[1] for row in user_rows} if user_rows else set()
        if user_columns and "password_hash" not in user_columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN password_hash VARCHAR(256)"))


def init_database() -> None:
    settings = get_settings()
    current_engine = getattr(SessionLocal, "kw", {}).get("bind") or engine
    if settings.database_url.startswith("sqlite"):
        Base.metadata.create_all(current_engine)
        _ensure_sqlite_dev_columns(current_engine)
    with SessionLocal.begin() as session:
        # Seed test user on SQLite if needed
        if settings.database_url.startswith("sqlite") and session.get(UserRecord, 1) is None:
            session.add(
                UserRecord(
                    id=1,
                    email="admin@quicdata.com",
                    role="admin",
                    is_active=True,
                )
            )
        if (
            settings.database_url.startswith("sqlite")
            and session.get(UserRecord, "usr_demo") is None
        ):
            token_hash = None
            if settings.bootstrap_admin_token:
                import hashlib

                token_hash = hashlib.sha256(settings.bootstrap_admin_token.encode()).hexdigest()
            session.add(
                UserRecord(
                    id="usr_demo",
                    display_name="Kirito",
                    email="kirito@quicrobot.local",
                    role="admin",
                    bootstrap_token_hash=token_hash,
                )
            )
        if session.get(ProjectRecord, "prj_robot_arm") is None:
            session.add(ProjectRecord(id="prj_robot_arm", name="具身智能训练", slug="robot-arm"))
        if session.get(ProjectRecord, "prj_other") is None:
            session.add(ProjectRecord(id="prj_other", name="隔离测试项目", slug="other-project"))
        if settings.auth_mode != "studio":
            membership = session.scalar(
                select(ProjectMembershipRecord).where(
                    ProjectMembershipRecord.project_id == "prj_robot_arm",
                    ProjectMembershipRecord.user_id == "usr_demo",
                )
            )
            if membership is None:
                session.add(
                    ProjectMembershipRecord(
                        id=new_id("pjm"),
                        project_id="prj_robot_arm",
                        user_id="usr_demo",
                        role="admin",
                    )
                )
        if session.get(ProjectPolicyRecord, "prj_robot_arm") is None:
            session.add(
                ProjectPolicyRecord(
                    project_id="prj_robot_arm",
                    max_concurrent_jobs=settings.default_max_concurrent_jobs,
                    max_gpus=settings.default_max_gpus,
                    max_runtime_seconds=settings.default_max_runtime_seconds,
                    provider_disabled=False,
                )
            )
        # Local multi-user admin: email/password from env (never commit plaintext).
        # Development convenience: if auth_mode=local and password unset, seed a local-only default.
        admin_password = settings.admin_password
        if not admin_password and settings.auth_mode == "local" and settings.env == "development":
            admin_password = "QuicTrain-Dev-Local!"
        if admin_password:
            from .auth import hash_password

            admin = session.scalar(
                select(UserRecord).where(UserRecord.email == settings.admin_email)
            )
            if admin is None:
                admin = UserRecord(
                    id=new_id("usr"),
                    display_name="Admin",
                    email=settings.admin_email,
                    role="admin",
                    active=True,
                    password_hash=hash_password(admin_password),
                )
                session.add(admin)
                session.flush()
            elif not admin.password_hash:
                admin.password_hash = hash_password(admin_password)
            if settings.auth_mode != "studio":
                admin_membership = session.scalar(
                    select(ProjectMembershipRecord).where(
                        ProjectMembershipRecord.project_id == "prj_robot_arm",
                        ProjectMembershipRecord.user_id == admin.id,
                    )
                )
                if admin_membership is None:
                    session.add(
                        ProjectMembershipRecord(
                            id=new_id("pjm"),
                            project_id="prj_robot_arm",
                            user_id=admin.id,
                            role="admin",
                        )
                    )


def get_session():
    with SessionLocal() as session:
        yield session


def append_event(
    session,
    job: JobRecord,
    event_type: str,
    payload: dict[str, Any],
) -> JobEventRecord:
    sequence = max((event.sequence for event in job.events), default=0) + 1
    event = JobEventRecord(
        id=new_id("evt"),
        job_id=job.id,
        sequence=sequence,
        event_type=event_type,
        payload=payload,
    )
    job.events.append(event)
    job.updated_at = utcnow()
    return event
