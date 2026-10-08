from __future__ import annotations

import json
import logging
import mimetypes
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from quictrain_config import CONFIG_MODELS
from quictrain_core import JobState, new_id
from quictrain_core.domain import StateTransitionError
from quictrain_core.version import __version__ as QUICTRAIN_VERSION
from quictrain_model_specs import MODEL_REGISTRY, DatasetVersion, compatibility_issues, get_model
from quictrain_scheduler import Scheduler, request_export, request_materialization
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .alerts import emit_alert, sls_reservation_status
from .auth import (
    enforce_actor,
    hash_password,
    identity_status,
    issue_session,
    resolve_actor,
    revoke_session_token,
    verify_password,
)
from .db import (
    ArtifactExportRecord,
    ArtifactRecord,
    AttemptRecord,
    AuditEventRecord,
    DatasetVersionRecord,
    JobLogRecord,
    JobRecord,
    MaterializationAttemptRecord,
    ModelVersionRecord,
    ProjectMembershipRecord,
    ProjectRecord,
    ResourceProfileRecord,
    SessionLocal,
    UserRecord,
    append_event,
    get_session,
    init_database,
    utcnow,
)
from .dsw_ops import (
    dsw_mode_status,
    get_dsw_instance,
    list_dsw_instances,
    start_dsw_instance,
    stop_dsw_instance,
    verify_dsw_instances,
)
from .errors import ServiceError
from .ops import capacity_snapshot, get_or_create_policy, idle_resource_stop_policy
from .pools import (
    match_resource_profile,
    pool_capacity_map,
    pool_resource_id_map,
    resolve_pool_region,
)
from .runtime import get_artifact_client, get_provider
from .schemas import (
    DatasetRegisterRequest,
    DatasetUpsertEnvelope,
    JobCreateRequest,
    JobValidationRequest,
    LoginRequest,
    MembershipWrite,
    ProjectPolicyPatch,
)
from .service import (
    create_job,
    dataset_from_record,
    local_runtime_injection,
    register_dataset_version,
    retry_job,
    seed_catalog,
    selectable_resource_profiles,
    serialize_job,
    transition_job,
    validate_job,
)
from .settings import get_settings

LOGGER = logging.getLogger(__name__)
settings = get_settings()
_scheduler_stop = threading.Event()


def _scheduler_loop() -> None:
    from .runtime import build_provider_registry

    scheduler = None
    while not _scheduler_stop.is_set():
        try:
            if scheduler is None:
                default_provider, providers = build_provider_registry()
                scheduler = Scheduler(
                    SessionLocal,
                    default_provider,
                    providers=providers,
                    artifact_root=settings.artifact_root,
                    artifact_client=get_artifact_client(),
                    cpfs_data_source_id=settings.cpfs_data_source_id,
                    cpfs_vpc_data_source_id=settings.cpfs_vpc_data_source_id,
                    cpfs_vpc_mount_target=settings.cpfs_vpc_mount_target,
                    cpfs_root_uri=settings.cpfs_root_uri,
                    cpfs_mount_path=settings.cpfs_mount_path,
                    cpfs_workspace_dir=settings.cpfs_workspace_dir,
                    cpfs_pi05_pretrained_dir=settings.cpfs_pi05_pretrained_dir,
                    dlc_user_vpc=settings.resolved_dlc_user_vpc(),
                    oss_cpfs_mirror_prefix=settings.oss_cpfs_mirror_prefix,
                )
            worked = scheduler.run_once()
        except Exception:
            LOGGER.exception("QuicTrain scheduler iteration failed; retrying")
            scheduler = None
            worked = False
        _scheduler_stop.wait(0.35 if worked else settings.scheduler_poll_seconds)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_database()
    with SessionLocal() as session:
        seed_catalog(session)
    thread = None
    if settings.embedded_scheduler:
        _scheduler_stop.clear()
        thread = threading.Thread(target=_scheduler_loop, daemon=True, name="quictrain-scheduler")
        thread.start()
    yield
    _scheduler_stop.set()
    if thread:
        thread.join(timeout=2)


app = FastAPI(
    title="QuicTrain API",
    version=QUICTRAIN_VERSION,
    description="Reproducible robotics training control plane",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-Id") or new_id("trc")
    request.state.trace_id = trace_id
    response = await call_next(request)
    response.headers["X-Trace-Id"] = trace_id
    return response


@app.exception_handler(ServiceError)
async def service_error_handler(request: Request, exc: ServiceError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "field": exc.field,
                "details": exc.details,
                "trace_id": request.state.trace_id,
                "retryable": exc.retryable,
            }
        },
    )


@app.exception_handler(StateTransitionError)
async def transition_error_handler(request: Request, exc: StateTransitionError):
    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "code": "ILLEGAL_STATE_TRANSITION",
                "message": str(exc),
                "field": "state",
                "details": {},
                "trace_id": request.state.trace_id,
                "retryable": False,
            }
        },
    )


@app.get("/health")
def health(session: Session = Depends(get_session)) -> dict[str, Any]:
    session.execute(select(1))
    return {
        "status": "healthy",
        "version": QUICTRAIN_VERSION,
        "environment": settings.env,
        "database": "connected",
        "provider": settings.provider,
        "scheduler": "embedded" if settings.embedded_scheduler else "external",
        "artifact_root": settings.artifact_root,
        "identity": identity_status(),
        "sls": sls_reservation_status(),
        "time": datetime.now(UTC).isoformat(),
    }


@app.get("/api/v1/ops/status")
def ops_status() -> dict[str, Any]:
    return {
        "capacity": capacity_snapshot(),
        "identity": identity_status(),
        "sls": sls_reservation_status(),
        "idle_resource_stop": idle_resource_stop_policy(),
        "dsw": dsw_mode_status(),
        "preferred_training_provider": "aliyun_dlc",
        "alerts_webhook_configured": bool(get_settings().alerts_webhook_url),
        "evidence_reservations": {
            "cloud_materialization": "docs/evidence/cloud/materialization.md",
            "cloud_export": "docs/evidence/cloud/export.md",
            "pg_restore_drill": "docs/evidence/backup/RESTORE_DRILL.md",
            "dlc_fault_drill": "docs/evidence/cloud/dlc_fault_drill.md",
            "sbom": "docs/evidence/sbom/",
            "alerts": "docs/evidence/ops/ALERT_EVIDENCE.md",
        },
    }


@app.get("/api/v1/ops/dsw/instances")
def ops_dsw_list(
    family: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    actor = resolve_actor(
        session,
        authorization=authorization,
        actor_header=x_quic_actor,
        project_id=get_settings().default_project_id,
    )
    actor.require_project_role("operator")
    return list_dsw_instances(family=family)


@app.get("/api/v1/ops/dsw/instances/{instance_id}")
def ops_dsw_get(
    instance_id: str,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    actor = resolve_actor(
        session,
        authorization=authorization,
        actor_header=x_quic_actor,
        project_id=get_settings().default_project_id,
    )
    actor.require_project_role("operator")
    return get_dsw_instance(instance_id)


@app.post("/api/v1/ops/dsw/instances/{instance_id}/start")
def ops_dsw_start(
    instance_id: str,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    actor = resolve_actor(
        session,
        authorization=authorization,
        actor_header=x_quic_actor,
        project_id=get_settings().default_project_id,
    )
    actor.require_project_role("admin")
    result = start_dsw_instance(instance_id)
    session.add(
        AuditEventRecord(
            id=new_id("aud"),
            actor_id=actor.user_id,
            action="dsw.start",
            resource_type="dsw",
            resource_id=instance_id,
        )
    )
    session.commit()
    return result


@app.post("/api/v1/ops/dsw/instances/{instance_id}/stop")
def ops_dsw_stop(
    instance_id: str,
    force: bool = Query(default=False),
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    actor = resolve_actor(
        session,
        authorization=authorization,
        actor_header=x_quic_actor,
        project_id=get_settings().default_project_id,
    )
    actor.require_project_role("admin")
    result = stop_dsw_instance(instance_id, force=force)
    session.add(
        AuditEventRecord(
            id=new_id("aud"),
            actor_id=actor.user_id,
            action="dsw.stop",
            resource_type="dsw",
            resource_id=instance_id,
        )
    )
    session.commit()
    return result


@app.post("/api/v1/ops/dsw/verify")
def ops_dsw_verify(
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
    family: str | None = Query(default=None),
) -> dict[str, Any]:
    actor = resolve_actor(
        session,
        authorization=authorization,
        actor_header=x_quic_actor,
        project_id=get_settings().default_project_id,
    )
    actor.require_project_role("operator")
    ids = None
    if family:
        from .dsw_ops import parse_known_dsw_instances

        ids = [
            item["instance_id"]
            for item in parse_known_dsw_instances()
            if item["family"] == family.lower()
        ]
    return verify_dsw_instances(ids)


@app.post("/api/v1/auth/login")
def auth_login(payload: LoginRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
    email = payload.email.strip().lower()
    user = session.scalar(select(UserRecord).where(UserRecord.email == email))
    if user is None:
        # Case-insensitive fallback for legacy mixed-case emails.
        user = session.scalar(select(UserRecord).where(func.lower(UserRecord.email) == email))
    if user is None or not user.active or not verify_password(payload.password, user.password_hash):
        raise ServiceError("INVALID_CREDENTIALS", "邮箱或密码不正确。", status_code=401)
    raw, record = issue_session(session, user)
    memberships = session.scalars(
        select(ProjectMembershipRecord).where(ProjectMembershipRecord.user_id == user.id)
    ).all()
    default_project = get_settings().default_project_id
    project_ids = [m.project_id for m in memberships]
    project_id = (
        default_project
        if default_project in project_ids
        else (project_ids[0] if project_ids else default_project)
    )
    session.add(
        AuditEventRecord(
            id=new_id("aud"),
            actor_id=user.id,
            action="auth.login",
            resource_type="user",
            resource_id=user.id,
            details={"session_id": record.id},
        )
    )
    session.commit()
    return {
        "access_token": raw,
        "token_type": "bearer",
        "expires_at": record.expires_at.isoformat(),
        "user": {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "role": user.role,
        },
        "project_id": project_id,
        "projects": [{"project_id": m.project_id, "role": m.role} for m in memberships],
    }


@app.post("/api/v1/auth/logout")
def auth_logout(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    if authorization and authorization.lower().startswith("bearer "):
        revoke_session_token(session, authorization.split(" ", 1)[1].strip())
        session.commit()
    return {"status": "ok"}


@app.get("/api/v1/auth/me")
def auth_me(
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    project_id = get_settings().default_project_id
    actor = resolve_actor(
        session,
        authorization=authorization,
        actor_header=x_quic_actor,
        project_id=project_id,
    )
    if get_settings().auth_mode == "studio":
        projects = [{"project_id": project_id, "role": actor.project_role or "viewer"}]
    else:
        memberships = session.scalars(
            select(ProjectMembershipRecord).where(ProjectMembershipRecord.user_id == actor.user_id)
        ).all()
        projects = [{"project_id": m.project_id, "role": m.role} for m in memberships]
    return {
        "user": {
            "id": actor.user_id,
            "email": actor.email,
            "display_name": actor.display_name,
            "role": actor.global_role,
        },
        "project_id": project_id,
        "project_role": actor.project_role,
        "projects": projects,
        "identity": identity_status(),
    }


@app.post("/api/v1/ops/alerts/test")
def ops_alert_test(
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    actor = resolve_actor(
        session,
        authorization=authorization,
        actor_header=x_quic_actor,
        project_id=get_settings().default_project_id,
    )
    actor.require_project_role("admin")
    return emit_alert(
        severity="info",
        title="quictrain_alert_test",
        details={"actor_id": actor.user_id},
    )


@app.get("/api/v1/dashboard")
def dashboard(
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    x_quic_project: str | None = Header(default=None, alias="X-Quic-Project"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    project_id = x_quic_project or get_settings().default_project_id
    enforce_actor(
        session,
        project_id=project_id,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="viewer",
    )
    counts = dict(
        session.execute(
            select(JobRecord.state, func.count())
            .where(JobRecord.project_id == project_id)
            .group_by(JobRecord.state)
        ).all()
    )
    recent = session.scalars(
        select(JobRecord)
        .where(JobRecord.project_id == project_id)
        .order_by(JobRecord.created_at.desc())
        .limit(6)
    ).all()
    capacity = capacity_snapshot()
    return {
        "jobs": {
            "running": counts.get(JobState.RUNNING.value, 0),
            "queued": counts.get(JobState.QUEUED.value, 0)
            + counts.get(JobState.PROVISIONING.value, 0),
            "failed": counts.get(JobState.FAILED.value, 0),
            "succeeded": counts.get(JobState.SUCCEEDED.value, 0),
        },
        "gpu_pools": capacity["pools"],
        "capacity": capacity,
        "recent_jobs": [serialize_job(job, session) for job in recent],
        "integrations": [
            {"name": "QuicData", "status": "healthy", "detail": "不可变数据版本目录"},
            {
                "name": "PAI-DLC",
                "status": "simulated" if settings.provider == "fake" else "healthy",
                "detail": settings.provider,
            },
            {"name": "MLflow", "status": "configured", "detail": settings.public_mlflow_url},
            {"name": "OSS", "status": "configured", "detail": "artifact manifest 模式"},
        ],
    }


@app.get("/api/v1/datasets")
def list_datasets(
    status: str | None = None,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    x_quic_project: str | None = Header(default=None, alias="X-Quic-Project"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    project_id = x_quic_project or get_settings().default_project_id
    enforce_actor(
        session,
        project_id=project_id,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="viewer",
    )
    statement = (
        select(DatasetVersionRecord)
        .where(DatasetVersionRecord.project_id == project_id)
        .order_by(DatasetVersionRecord.created_at.desc())
    )
    if status:
        statement = statement.where(DatasetVersionRecord.status == status)
    items = session.scalars(statement).all()
    payloads: list[dict[str, Any]] = []
    for item in items:
        payload = dict(item.manifest)
        payload["status"] = item.status
        if item.materialized_uri:
            payload["materialized_uri"] = item.materialized_uri
        if item.uri and not payload.get("uri"):
            payload["uri"] = item.uri
        payloads.append(payload)
    return {"items": payloads, "next_cursor": None}


@app.post("/api/v1/datasets")
def register_dataset(
    request: DatasetRegisterRequest,
    response: Response,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    x_quic_project: str | None = Header(default=None, alias="X-Quic-Project"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Register an OSS/CPFS dataset version for training mounts (operator UI)."""
    project_id = x_quic_project or get_settings().default_project_id
    actor = enforce_actor(
        session,
        project_id=project_id,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="operator",
    )
    record, created = register_dataset_version(
        session,
        project_id=project_id,
        actor_id=actor.user_id,
        request=request,
    )
    materialization: dict[str, Any] | None = None
    if created and request.request_materialization:
        attempt = request_materialization(session, record, actor_id=actor.user_id)
        materialization = {
            "materialization_id": attempt.id,
            "state": attempt.state,
            "lease_key": attempt.lease_key,
            "target_uri": attempt.target_uri,
        }
    session.commit()
    response.status_code = 201 if created else 200
    return {
        "dataset_version_id": record.id,
        "created": created,
        "status": record.status,
        "uri": record.uri,
        "checksum": record.checksum,
        "materialization": materialization,
    }


@app.get("/api/v1/datasets/{dataset_version_id}")
def dataset_detail(
    dataset_version_id: str,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    record = session.get(DatasetVersionRecord, dataset_version_id)
    if record is None:
        raise ServiceError("DATASET_NOT_FOUND", "数据版本不存在。", status_code=404)
    enforce_actor(
        session,
        project_id=record.project_id,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="viewer",
    )
    result = dict(record.manifest)
    result["compatibility"] = {
        model.id: compatibility_issues(dataset_from_record(record), model)
        for model in MODEL_REGISTRY.values()
    }
    return result


@app.get("/api/v1/datasets/{dataset_version_id}/compatibility")
def dataset_compatibility(
    dataset_version_id: str,
    model_version_id: str,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    record = session.get(DatasetVersionRecord, dataset_version_id)
    if record is None:
        raise ServiceError("DATASET_NOT_FOUND", "数据版本不存在。", status_code=404)
    enforce_actor(
        session,
        project_id=record.project_id,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="viewer",
    )
    try:
        model = get_model(model_version_id)
    except KeyError as exc:
        raise ServiceError("MODEL_NOT_FOUND", "模型版本不存在。", status_code=404) from exc
    issues = compatibility_issues(dataset_from_record(record), model)
    return {
        "compatible": not any(item["severity"] == "BLOCKER" for item in issues),
        "issues": issues,
    }


@app.post("/api/v1/integrations/data-platform/dataset-versions:upsert")
def upsert_dataset(
    envelope: DatasetUpsertEnvelope,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    x_quic_project: str | None = Header(default=None, alias="X-Quic-Project"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if idempotency_key != envelope.event_id:
        raise ServiceError("EVENT_ID_MISMATCH", "Idempotency-Key 与 event_id 不一致。")
    project_id = x_quic_project or get_settings().default_project_id
    actor = enforce_actor(
        session,
        project_id=project_id,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="operator",
    )
    payload = envelope.dataset_version
    required = (
        "external_dataset_id",
        "external_version_id",
        "display_name",
        "uri",
        "checksum",
        "episodes",
        "frames",
        "duration_hours",
        "fps",
        "robot_type",
        "camera_keys",
        "action_dim",
        "state_dim",
    )
    missing = [key for key in required if key not in payload or payload[key] is None]
    if missing:
        raise ServiceError(
            "DATASET_VERSION_INVALID",
            "数据版本缺少必填字段。",
            field=missing[0],
            status_code=400,
            details={"missing": missing},
        )
    external_id = str(payload["external_version_id"])
    record = session.scalar(
        select(DatasetVersionRecord).where(
            DatasetVersionRecord.external_dataset_id == payload["external_dataset_id"],
            DatasetVersionRecord.version == external_id,
        )
    )
    if record:
        if record.project_id != project_id:
            raise ServiceError(
                "DATASET_VERSION_PROJECT_CONFLICT",
                "数据集标识和版本已被其他项目使用，请选择当前项目的独立标识。",
                status_code=409,
            )
        if record.checksum != payload["checksum"]:
            raise ServiceError(
                "IMMUTABLE_VERSION_CONFLICT", "不可变数据版本 checksum 冲突。", status_code=409
            )
        return {"dataset_version_id": record.id, "created": False}
    try:
        validated = DatasetVersion.model_validate(
            {
                "id": new_id("dsv"),
                "dataset_id": payload["external_dataset_id"],
                "name": payload["display_name"],
                "version": external_id,
                "status": payload.get("status", "REGISTERED"),
                "format": payload.get("format", "lerobot"),
                "format_version": payload.get("format_version", "3.0"),
                "uri": payload["uri"],
                "checksum": payload["checksum"],
                "repo_id": payload.get("repo_id"),
                "revision": payload.get("revision"),
                "episodes": payload["episodes"],
                "frames": payload["frames"],
                "duration_hours": payload["duration_hours"],
                "fps": payload["fps"],
                "robot_type": payload["robot_type"],
                "camera_keys": payload["camera_keys"],
                "action_dim": payload["action_dim"],
                "state_dim": payload["state_dim"],
                "language_tasks": payload.get("language_tasks", True),
            }
        )
    except Exception as exc:
        raise ServiceError(
            "DATASET_VERSION_INVALID",
            "数据版本字段校验失败。",
            status_code=400,
            details={"error": str(exc)},
        ) from exc
    record = DatasetVersionRecord(
        id=validated.id,
        project_id=project_id,
        external_dataset_id=validated.dataset_id,
        name=validated.name,
        version=validated.version,
        status=validated.status,
        format=validated.format,
        format_version=validated.format_version,
        uri=validated.uri,
        checksum=validated.checksum,
        manifest=validated.model_dump(mode="json"),
    )
    session.add(record)
    session.add(
        AuditEventRecord(
            id=new_id("aud"),
            actor_id=actor.user_id,
            action="dataset.upsert",
            resource_type="dataset_version",
            resource_id=record.id,
            details={"event_id": envelope.event_id},
        )
    )
    session.commit()
    return {"dataset_version_id": record.id, "created": True}


@app.get("/api/v1/models")
def list_models(
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    x_quic_project: str | None = Header(default=None, alias="X-Quic-Project"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    project_id = x_quic_project or get_settings().default_project_id
    enforce_actor(
        session,
        project_id=project_id,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="viewer",
    )
    records = session.scalars(
        select(ModelVersionRecord)
        .where(ModelVersionRecord.deprecated.is_(False))
        .order_by(ModelVersionRecord.display_name)
    ).all()
    return {"items": [model_summary(record) for record in records], "next_cursor": None}


@app.get("/api/v1/models/{model_version_id}")
def model_detail(
    model_version_id: str,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    x_quic_project: str | None = Header(default=None, alias="X-Quic-Project"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    project_id = x_quic_project or get_settings().default_project_id
    enforce_actor(
        session,
        project_id=project_id,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="viewer",
    )
    record = session.get(ModelVersionRecord, model_version_id)
    if record is None:
        try:
            model = get_model(model_version_id)
        except KeyError as exc:
            raise ServiceError("MODEL_NOT_FOUND", "模型版本不存在。", status_code=404) from exc
        record = session.get(ModelVersionRecord, model.version_id)
    assert record is not None
    return {**model_summary(record), "manifest": record.manifest}


@app.get("/api/v1/models/{model_version_id}/recipes/{recipe_id}/schema")
def model_schema(
    model_version_id: str,
    recipe_id: str,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    x_quic_project: str | None = Header(default=None, alias="X-Quic-Project"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    project_id = x_quic_project or get_settings().default_project_id
    enforce_actor(
        session,
        project_id=project_id,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="viewer",
    )
    try:
        model = get_model(model_version_id)
    except KeyError as exc:
        raise ServiceError("MODEL_NOT_FOUND", "模型版本不存在。", status_code=404) from exc
    recipe = next((item for item in model.recipes if item.id == recipe_id), None)
    if recipe is None:
        raise ServiceError("RECIPE_NOT_FOUND", "Recipe 不存在。", status_code=404)
    settings = get_settings()
    profiles = selectable_resource_profiles(model.id, recipe.resource_profiles)
    injection = local_runtime_injection() or {}
    config_cls = CONFIG_MODELS[model.id]
    default_config = config_cls().model_dump(mode="json")
    return {
        "model_version_id": model.version_id,
        "recipe_id": recipe.id,
        "schema_version": "1.0.0",
        "schema_hash": model.schema_hash,
        "json_schema": model.schema,
        "defaults": default_config,
        "provider": settings.provider,
        "artifact_root": settings.artifact_root,
        "image_digest": model.image_digest,
        "runtime_injection": injection,
        "ui": {
            "groups": [
                {"id": "training", "title": "训练", "order": 10},
                {"id": "optimizer", "title": "优化器", "order": 20},
                {"id": "policy", "title": "策略", "order": 30},
            ]
        },
        "resource_profiles": [item.model_dump(mode="json") for item in profiles],
    }


@app.get("/api/v1/resources")
def list_resources(
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    x_quic_project: str | None = Header(default=None, alias="X-Quic-Project"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    project_id = x_quic_project or get_settings().default_project_id
    enforce_actor(
        session,
        project_id=project_id,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="viewer",
    )
    profiles = session.scalars(
        select(ResourceProfileRecord)
        .where(ResourceProfileRecord.enabled.is_(True))
        .order_by(ResourceProfileRecord.name)
    ).all()
    capacity = capacity_snapshot()
    resource_ids = pool_resource_id_map()
    capacities = pool_capacity_map()
    serialized = []
    for item in profiles:
        match = match_resource_profile(
            profile_id=item.id,
            provider_id=item.provider_id or "fake",
            pool_id=item.pool_id,
            gpu_count=item.gpu_count,
            selectable=bool(item.selectable),
            gpu_models=list(item.gpu_models or []),
        )
        serialized.append(
            {
                "id": item.id,
                "model_version_id": item.model_version_id,
                "recipe_id": item.recipe_id,
                "name": item.name,
                "gpu_models": item.gpu_models,
                "gpu_count": item.gpu_count,
                "vram_gb_min": item.vram_gb_min,
                "calibration_status": item.calibration_status,
                "selectable": item.selectable,
                "enabled": item.enabled,
                "warning": item.warning,
                "provider_id": item.provider_id,
                "pool_id": item.pool_id,
                "resource_id": match.get("resource_id"),
                "region": match.get("region"),
                "pool_capacity_gpus": match.get("pool_capacity_gpus"),
                "match": match,
            }
        )
    return {
        "pools": capacity["pools"],
        "capacity": capacity,
        "pool_bindings": [
            {
                "pool_id": pool_id,
                "resource_id": resource_id,
                "region": resolve_pool_region(pool_id),
                "capacity_gpus": capacities.get(pool_id),
            }
            for pool_id, resource_id in sorted(resource_ids.items())
        ],
        "profiles": serialized,
    }


@app.post("/api/v1/resources/match")
def match_resources(
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    x_quic_project: str | None = Header(default=None, alias="X-Quic-Project"),
    profile_id: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Validate Profile ↔ pool ↔ ResourceId binding (resource page matching)."""

    project_id = x_quic_project or get_settings().default_project_id
    enforce_actor(
        session,
        project_id=project_id,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="viewer",
    )
    query = select(ResourceProfileRecord).where(ResourceProfileRecord.enabled.is_(True))
    if profile_id:
        query = query.where(ResourceProfileRecord.id == profile_id)
    rows = session.scalars(query.order_by(ResourceProfileRecord.name)).all()
    if profile_id and not rows:
        raise ServiceError("RESOURCE_PROFILE_NOT_FOUND", "资源 Profile 不存在。", status_code=404)
    items = [
        match_resource_profile(
            profile_id=item.id,
            provider_id=item.provider_id or "fake",
            pool_id=item.pool_id,
            gpu_count=item.gpu_count,
            selectable=bool(item.selectable),
            gpu_models=list(item.gpu_models or []),
        )
        for item in rows
    ]
    return {
        "ok": all(item["ok"] for item in items if item.get("status") != "NOT_SELECTABLE"),
        "matched": sum(1 for item in items if item["ok"]),
        "total": len(items),
        "items": items,
        "pool_bindings": [
            {
                "pool_id": pool_id,
                "resource_id": resource_id,
                "region": resolve_pool_region(pool_id),
            }
            for pool_id, resource_id in sorted(pool_resource_id_map().items())
        ],
    }


@app.post("/api/v1/jobs/validate")
def validate_job_endpoint(
    request: JobValidationRequest,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    enforce_actor(
        session,
        project_id=request.project_id,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="viewer",
    )
    return validate_job(session, request)


@app.post("/api/v1/jobs", status_code=202)
def create_job_endpoint(
    request: JobCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if idempotency_key and idempotency_key != request.client_request_id:
        raise ServiceError("IDEMPOTENCY_KEY_MISMATCH", "幂等键与 client_request_id 不一致。")
    actor = resolve_actor(
        session,
        authorization=authorization,
        actor_header=x_quic_actor,
        project_id=request.project_id,
    )
    actor.require_project_role("operator")
    job = create_job(session, request, creator_id=actor.user_id)
    return {
        "job_id": job.id,
        "state": job.state,
        "created_at": job.created_at.isoformat(),
        "links": {"self": f"/api/v1/jobs/{job.id}"},
    }


@app.get("/api/v1/jobs")
def list_jobs(
    status: list[str] | None = Query(default=None),
    model_id: str | None = None,
    dataset_version_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    x_quic_project: str | None = Header(default=None, alias="X-Quic-Project"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    project_id = x_quic_project or get_settings().default_project_id
    actor = resolve_actor(
        session,
        authorization=authorization,
        actor_header=x_quic_actor,
        project_id=project_id,
    )
    actor.require_project_role("viewer")
    statement = (
        select(JobRecord)
        .where(JobRecord.project_id == project_id)
        .order_by(JobRecord.created_at.desc())
        .limit(limit)
    )
    if status:
        statement = statement.where(JobRecord.state.in_(status))
    if model_id:
        statement = statement.where(JobRecord.model_id == model_id)
    if dataset_version_id:
        statement = statement.where(JobRecord.dataset_version_id == dataset_version_id)
    jobs = session.scalars(statement).all()
    return {"items": [serialize_job(job, session) for job in jobs], "next_cursor": None}


@app.get("/api/v1/jobs/{job_id}")
def job_detail(
    job_id: str,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    job = session.get(JobRecord, job_id)
    if job is None:
        raise ServiceError("JOB_NOT_FOUND", "任务不存在。", status_code=404)
    actor = resolve_actor(
        session,
        authorization=authorization,
        actor_header=x_quic_actor,
        project_id=job.project_id,
    )
    actor.require_project_role("viewer")
    return serialize_job(job, session)


@app.post("/api/v1/jobs/{job_id}/cancel", status_code=202)
def cancel_job(
    job_id: str,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    job = session.get(JobRecord, job_id)
    if job is None:
        raise ServiceError("JOB_NOT_FOUND", "任务不存在。", status_code=404)
    actor = resolve_actor(
        session,
        authorization=authorization,
        actor_header=x_quic_actor,
        project_id=job.project_id,
    )
    actor.require_project_role("operator")
    attempt = job.attempts[-1] if job.attempts else None
    if job.state == JobState.QUEUED.value:
        if attempt is not None:
            attempt.state = "CANCELLED"
            attempt.finished_at = utcnow()
        transition_job(session, job, JobState.CANCELLED, "job.cancelled")
    elif job.state in {
        JobState.SUBMITTING.value,
        JobState.ORPHANED.value,
        JobState.PROVISIONING.value,
        JobState.RUNNING.value,
        JobState.CANCEL_REQUESTED.value,
    }:
        # Best-effort: stop the cloud job immediately when user cancels in UI,
        # then leave Scheduler reconciliation idempotent for CANCELLED.
        transition_job(session, job, JobState.CANCEL_REQUESTED, "job.cancel_requested")
        if attempt is not None and attempt.external_job_id:
            try:
                provider = get_provider()
                provider_job = provider.cancel(attempt.external_job_id)
                attempt.provider_state = provider_job.raw_status
                attempt.provider_payload = {
                    **(attempt.provider_payload or {}),
                    "reason_code": provider_job.reason_code,
                    "reason_message": provider_job.message,
                }
            except Exception as exc:  # noqa: BLE001 — surface via audit; Scheduler retries
                LOGGER.exception("immediate cancel failed for %s", job.id)
                append_event(
                    session,
                    job,
                    "job.warning",
                    {"message": f"immediate provider cancel failed: {exc}"},
                )
            attempt.state = "CANCELLED"
            attempt.finished_at = utcnow()
            transition_job(session, job, JobState.CANCELLED, "job.cancelled")
        # If not yet submitted, Scheduler will no-op on CANCEL_REQUESTED without external id.
    elif job.state not in {JobState.CANCELLED.value}:
        raise ServiceError("JOB_NOT_CANCELLABLE", "当前状态不能取消。", status_code=409)
    session.add(
        AuditEventRecord(
            id=new_id("aud"),
            actor_id=actor.user_id,
            action="job.cancel",
            resource_type="job",
            resource_id=job.id,
        )
    )
    session.commit()
    return {"job_id": job.id, "state": job.state}


@app.post("/api/v1/jobs/{job_id}/retry", status_code=202)
def retry_job_endpoint(
    job_id: str,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    job = session.get(JobRecord, job_id)
    if job is None:
        raise ServiceError("JOB_NOT_FOUND", "任务不存在。", status_code=404)
    actor = enforce_actor(
        session,
        project_id=job.project_id,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="operator",
    )
    retry_job(session, job, actor_id=actor.user_id)
    return {"job_id": job.id, "state": job.state}


@app.post("/api/v1/jobs/{job_id}/clone", status_code=202)
def clone_job(
    job_id: str,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    source = session.get(JobRecord, job_id)
    if source is None:
        raise ServiceError("JOB_NOT_FOUND", "任务不存在。", status_code=404)
    actor = enforce_actor(
        session,
        project_id=source.project_id,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="operator",
    )
    request = JobCreateRequest(
        project_id=source.project_id,
        dataset_version_id=source.dataset_version_id,
        model_version_id=source.model_version_id,
        recipe_id=source.recipe_id,
        config_overrides=source.user_overrides,
        resource_selection={"mode": "MANUAL", "profile": source.resource_profile_id},
        client_request_id=new_id("clone"),
        display_name=f"{source.display_name}-clone",
    )
    cloned = create_job(session, request, creator_id=actor.user_id)
    cloned.parent_job_id = source.id
    session.commit()
    return {"job_id": cloned.id, "state": cloned.state, "parent_job_id": source.id}


@app.get("/api/v1/jobs/{job_id}/events")
def job_events(
    job_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    job = session.get(JobRecord, job_id)
    if job is None:
        raise ServiceError("JOB_NOT_FOUND", "任务不存在。", status_code=404)
    enforce_actor(
        session,
        project_id=job.project_id,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="viewer",
    )
    last_id = int(request.headers.get("Last-Event-ID", "0"))

    def stream():
        cursor = last_id
        for _ in range(20):
            with SessionLocal() as event_session:
                current = event_session.get(JobRecord, job_id)
                if current is None:
                    return
                pending = [event for event in current.events if event.sequence > cursor]
                for event in pending:
                    cursor = event.sequence
                    yield (
                        f"id: {event.sequence}\n"
                        f"event: {event.event_type}\n"
                        f"data: {json.dumps(event.payload, ensure_ascii=False)}\n\n"
                    )
                if current.state in {
                    JobState.SUCCEEDED.value,
                    JobState.FAILED.value,
                    JobState.CANCELLED.value,
                }:
                    return
            yield ": keepalive\n\n"
            time.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/v1/jobs/{job_id}/logs")
def job_logs(
    job_id: str,
    cursor: int = 0,
    limit: int = Query(default=1000, ge=1, le=5000),
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    job = session.get(JobRecord, job_id)
    if job is None:
        raise ServiceError("JOB_NOT_FOUND", "任务不存在。", status_code=404)
    enforce_actor(
        session,
        project_id=job.project_id,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="viewer",
    )
    attempt = job.attempts[-1] if job.attempts else None
    if attempt is None:
        return {"lines": [], "next_cursor": cursor, "eof": False, "degraded": False}
    lines = list(
        session.scalars(
            select(JobLogRecord)
            .where(
                JobLogRecord.attempt_id == attempt.id,
                JobLogRecord.sequence > cursor,
            )
            .order_by(JobLogRecord.sequence.asc())
            .limit(limit)
        )
    )
    if (
        not lines
        and not attempt.logs
        and job.state
        in {
            JobState.SUCCEEDED.value,
            JobState.FAILED.value,
            JobState.CANCELLED.value,
        }
    ):
        log_artifact = next(
            (
                item
                for item in job.artifacts
                if item.attempt_id == attempt.id
                and item.name.endswith(".log")
                and item.size_bytes <= PREVIEW_MAX_BYTES
            ),
            None,
        )
        access_uri = _artifact_access_uri(log_artifact, session) if log_artifact else None
        artifact_client = get_artifact_client() if access_uri else None
        if log_artifact and access_uri and artifact_client:
            try:
                archived = (
                    artifact_client.read_bytes(access_uri, max_bytes=PREVIEW_MAX_BYTES)
                    .decode("utf-8", errors="replace")
                    .splitlines()
                )
                selected = archived[cursor : cursor + limit]
                return {
                    "lines": [
                        {
                            "sequence": index + 1,
                            "timestamp": log_artifact.created_at.isoformat(),
                            "level": "INFO",
                            "source": f"artifact/{log_artifact.name}",
                            "message": message,
                        }
                        for index, message in enumerate(selected, start=cursor)
                    ],
                    "next_cursor": cursor + len(selected),
                    "eof": cursor + len(selected) >= len(archived),
                    "degraded": True,
                }
            except (FileNotFoundError, OSError):
                pass
    next_cursor = lines[-1].sequence if lines else cursor
    return {
        "lines": [
            {
                "sequence": line.sequence,
                "timestamp": line.timestamp,
                "level": line.level,
                "source": line.source,
                "message": line.message,
            }
            for line in lines
        ],
        "next_cursor": next_cursor,
        "eof": (
            job.state in {JobState.SUCCEEDED.value, JobState.FAILED.value, JobState.CANCELLED.value}
            and len(lines) < limit
        ),
        "degraded": False,
    }


def _available_artifact(artifact_id: str, session: Session) -> ArtifactRecord:
    artifact = session.get(ArtifactRecord, artifact_id)
    if artifact is None or not artifact.available:
        raise ServiceError("ARTIFACT_NOT_FOUND", "产物不存在或不可下载。", status_code=404)
    return artifact


PREVIEW_EXTENSIONS = {".json", ".yaml", ".yml", ".txt", ".log", ".toml", ".md", ".csv"}
PREVIEW_MAX_BYTES = 2 * 1024 * 1024


def _artifact_access_uri(artifact: ArtifactRecord, session: Session) -> str | None:
    export_uri = getattr(artifact, "export_uri", None)
    if export_uri:
        return export_uri
    if artifact.uri.startswith("oss://"):
        return artifact.uri
    suffix = "." + artifact.name.rsplit(".", 1)[-1].lower() if "." in artifact.name else ""
    if suffix not in PREVIEW_EXTENSIONS or artifact.size_bytes > PREVIEW_MAX_BYTES:
        return None
    attempt = session.get(AttemptRecord, artifact.attempt_id)
    payload = attempt.provider_payload if attempt is not None else {}
    artifact_prefix = str(payload.get("artifact_prefix") or "").rstrip("/")
    control_prefix = str(payload.get("artifact_control_prefix") or "").rstrip("/")
    if not artifact_prefix or not control_prefix or not control_prefix.startswith("oss://"):
        return None
    if not artifact.uri.startswith(f"{artifact_prefix}/"):
        return None
    relative = artifact.uri.removeprefix(f"{artifact_prefix}/")
    return f"{control_prefix}/{relative}"


def _signed_artifact_url(artifact: ArtifactRecord, session: Session) -> str:
    access_uri = _artifact_access_uri(artifact, session)
    if access_uri is None:
        raise ServiceError(
            "ARTIFACT_EXPORT_REQUIRED",
            "该产物保存在 CPFS；请先创建 OSS 导出任务后再从浏览器下载。",
            status_code=409,
            details={
                "uri": artifact.uri,
                "export_hint": f"POST /api/v1/artifacts/{artifact.id}/export",
            },
        )
    if access_uri.startswith("file://"):
        # Fake/dev exports stay on local disk; never stream checkpoint bytes via the API.
        return (
            f"https://local-export.invalid/exports/{artifact.id}"
            f"?name={artifact.name}&sha256={artifact.sha256}"
        )
    artifact_client = get_artifact_client()
    if artifact_client is None:
        raise ServiceError(
            "ARTIFACT_STORE_UNAVAILABLE",
            "OSS 产物存储未配置。",
            status_code=503,
            retryable=True,
        )
    return artifact_client.presign_get(
        access_uri,
        expires_seconds=900,
        download_name=artifact.name,
    )


def _require_artifact_project_access(
    artifact: ArtifactRecord,
    session: Session,
    *,
    authorization: str | None,
    actor_header: str | None,
    minimum_role: str,
):
    job = session.get(JobRecord, artifact.job_id)
    if job is None:
        raise ServiceError("JOB_NOT_FOUND", "任务不存在。", status_code=404)
    return enforce_actor(
        session,
        project_id=job.project_id,
        authorization=authorization,
        actor_header=actor_header,
        minimum_role=minimum_role,
    )


@app.get("/api/v1/artifacts/{artifact_id}/download")
def artifact_download(
    artifact_id: str,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Redirect a normal browser navigation to a short-lived attachment URL."""
    artifact = _available_artifact(artifact_id, session)
    _require_artifact_project_access(
        artifact,
        session,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="viewer",
    )
    return RedirectResponse(_signed_artifact_url(artifact, session), status_code=307)


@app.post("/api/v1/artifacts/{artifact_id}/download-url")
def artifact_download_url(
    artifact_id: str,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Compatibility endpoint for API clients; browsers should use /download."""
    artifact = _available_artifact(artifact_id, session)
    _require_artifact_project_access(
        artifact,
        session,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="viewer",
    )
    expires_at = datetime.now(UTC) + timedelta(minutes=15)
    return {
        "url": _signed_artifact_url(artifact, session),
        "expires_at": expires_at.isoformat(),
        "sha256": artifact.sha256,
    }


@app.get("/api/v1/artifacts/{artifact_id}/preview")
def artifact_preview(
    artifact_id: str,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    artifact = _available_artifact(artifact_id, session)
    _require_artifact_project_access(
        artifact,
        session,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="viewer",
    )
    suffix = "." + artifact.name.rsplit(".", 1)[-1].lower() if "." in artifact.name else ""
    if suffix not in PREVIEW_EXTENSIONS:
        raise ServiceError(
            "ARTIFACT_PREVIEW_UNSUPPORTED",
            "该文件类型不支持在线预览，请直接下载。",
            status_code=415,
        )
    if artifact.size_bytes > PREVIEW_MAX_BYTES:
        raise ServiceError(
            "ARTIFACT_PREVIEW_TOO_LARGE",
            "文本文件超过 2 MiB 预览上限，请直接下载。",
            status_code=413,
        )
    access_uri = _artifact_access_uri(artifact, session)
    artifact_client = get_artifact_client()
    if artifact_client is None or access_uri is None:
        raise ServiceError("ARTIFACT_STORE_UNAVAILABLE", "产物预览存储未配置。", status_code=503)
    try:
        content = artifact_client.read_bytes(access_uri, max_bytes=PREVIEW_MAX_BYTES).decode(
            "utf-8"
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise ServiceError(
            "ARTIFACT_PREVIEW_INVALID", "文件不是可预览的 UTF-8 文本。", status_code=422
        ) from exc
    if suffix == ".json":
        try:
            content = json.dumps(json.loads(content), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            pass
    return {
        "name": artifact.name,
        "content": content,
        "content_type": mimetypes.guess_type(artifact.name)[0] or "text/plain",
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
    }


@app.post("/api/v1/datasets/{dataset_version_id}/materializations", status_code=202)
def materialize_dataset(
    dataset_version_id: str,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    record = session.get(DatasetVersionRecord, dataset_version_id)
    if record is None:
        raise ServiceError("DATASET_NOT_FOUND", "数据版本不存在。", status_code=404)
    actor = resolve_actor(
        session,
        authorization=authorization,
        actor_header=x_quic_actor,
        project_id=record.project_id,
    )
    actor.require_project_role("operator")
    attempt = request_materialization(session, record, actor_id=actor.user_id)
    session.commit()
    return {
        "materialization_id": attempt.id,
        "dataset_version_id": record.id,
        "state": attempt.state,
        "lease_key": attempt.lease_key,
        "target_uri": attempt.target_uri,
    }


@app.get("/api/v1/datasets/{dataset_version_id}/materializations")
def list_materializations(
    dataset_version_id: str,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    record = session.get(DatasetVersionRecord, dataset_version_id)
    if record is None:
        raise ServiceError("DATASET_NOT_FOUND", "数据版本不存在。", status_code=404)
    enforce_actor(
        session,
        project_id=record.project_id,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="viewer",
    )
    items = session.scalars(
        select(MaterializationAttemptRecord)
        .where(MaterializationAttemptRecord.dataset_version_id == dataset_version_id)
        .order_by(MaterializationAttemptRecord.number.desc())
    ).all()
    return {
        "items": [
            {
                "id": item.id,
                "number": item.number,
                "state": item.state,
                "lease_key": item.lease_key,
                "source_uri": item.source_uri,
                "target_uri": item.target_uri,
                "checksum": item.checksum,
                "bytes_copied": item.bytes_copied,
                "actor_id": item.actor_id,
                "error_message": item.error_message,
                "created_at": item.created_at.isoformat(),
                "finished_at": item.finished_at.isoformat() if item.finished_at else None,
            }
            for item in items
        ]
    }


@app.post("/api/v1/artifacts/{artifact_id}/export", status_code=202)
def export_artifact(
    artifact_id: str,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    artifact = _available_artifact(artifact_id, session)
    job = session.get(JobRecord, artifact.job_id)
    if job is None:
        raise ServiceError("JOB_NOT_FOUND", "任务不存在。", status_code=404)
    actor = resolve_actor(
        session,
        authorization=authorization,
        actor_header=x_quic_actor,
        project_id=job.project_id,
    )
    actor.require_project_role("operator")
    export = request_export(session, artifact, actor_id=actor.user_id, job_id=job.id)
    session.commit()
    return {
        "export_id": export.id,
        "artifact_id": artifact.id,
        "state": export.state,
        "source_uri": export.source_uri,
    }


@app.get("/api/v1/artifacts/{artifact_id}/exports")
def list_artifact_exports(
    artifact_id: str,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    artifact = session.get(ArtifactRecord, artifact_id)
    if artifact is None:
        raise ServiceError("ARTIFACT_NOT_FOUND", "产物不存在或不可下载。", status_code=404)
    _require_artifact_project_access(
        artifact,
        session,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="viewer",
    )
    items = session.scalars(
        select(ArtifactExportRecord)
        .where(ArtifactExportRecord.artifact_id == artifact_id)
        .order_by(ArtifactExportRecord.created_at.desc())
    ).all()
    return {
        "items": [
            {
                "id": item.id,
                "state": item.state,
                "export_uri": item.export_uri,
                "bytes_copied": item.bytes_copied,
                "checksum": item.checksum,
                "actor_id": item.actor_id,
                "retention_days": item.retention_days,
                "error_message": item.error_message,
                "created_at": item.created_at.isoformat(),
                "finished_at": item.finished_at.isoformat() if item.finished_at else None,
            }
            for item in items
        ]
    }


@app.get("/api/v1/projects/{project_id}/policy")
def get_project_policy(
    project_id: str,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    enforce_actor(
        session,
        project_id=project_id,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="viewer",
    )
    policy = get_or_create_policy(session, project_id)
    session.commit()
    return {
        "project_id": policy.project_id,
        "max_concurrent_jobs": policy.max_concurrent_jobs,
        "max_gpus": policy.max_gpus,
        "max_runtime_seconds": policy.max_runtime_seconds,
        "provider_disabled": policy.provider_disabled,
        "updated_at": policy.updated_at.isoformat(),
    }


@app.patch("/api/v1/admin/projects/{project_id}/policy")
def patch_project_policy(
    project_id: str,
    payload: ProjectPolicyPatch,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    actor = enforce_actor(
        session,
        project_id=project_id,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="admin",
    )
    policy = get_or_create_policy(session, project_id)
    updates = payload.model_dump(exclude_none=True)
    for key, value in updates.items():
        setattr(policy, key, value)
    session.add(
        AuditEventRecord(
            id=new_id("aud"),
            actor_id=actor.user_id,
            action="project.policy.update",
            resource_type="project",
            resource_id=project_id,
            details=updates,
        )
    )
    session.commit()
    return {
        "project_id": policy.project_id,
        "max_concurrent_jobs": policy.max_concurrent_jobs,
        "max_gpus": policy.max_gpus,
        "max_runtime_seconds": policy.max_runtime_seconds,
        "provider_disabled": policy.provider_disabled,
        "updated_at": policy.updated_at.isoformat(),
    }


@app.get("/api/v1/projects/{project_id}/members")
def list_project_members(
    project_id: str,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if session.get(ProjectRecord, project_id) is None:
        raise ServiceError("PROJECT_NOT_FOUND", "项目不存在。", status_code=404)
    enforce_actor(
        session,
        project_id=project_id,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="viewer",
    )
    if get_settings().auth_mode == "studio":
        users = session.scalars(select(UserRecord).order_by(UserRecord.id.asc())).all()
        items = []
        for u in users:
            items.append(
                {
                    "membership_id": f"mem_{u.id}",
                    "project_id": project_id,
                    "user_id": str(u.id),
                    "role": "admin" if u.role == "admin" else "viewer",
                    "display_name": u.display_name,
                    "email": u.email,
                    "created_at": (
                        u.created_at.isoformat()
                        if getattr(u, "created_at", None) is not None
                        else utcnow().isoformat()
                    ),
                }
            )
        return {"items": items}
    rows = session.scalars(
        select(ProjectMembershipRecord)
        .where(ProjectMembershipRecord.project_id == project_id)
        .order_by(ProjectMembershipRecord.created_at.asc())
    ).all()
    items = []
    for membership in rows:
        user = session.get(UserRecord, membership.user_id)
        items.append(
            {
                "membership_id": membership.id,
                "project_id": membership.project_id,
                "user_id": membership.user_id,
                "role": membership.role,
                "display_name": user.display_name if user else None,
                "email": user.email if user else None,
                "created_at": membership.created_at.isoformat(),
            }
        )
    return {"items": items}


@app.put("/api/v1/projects/{project_id}/members")
def upsert_project_member(
    project_id: str,
    payload: MembershipWrite,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if session.get(ProjectRecord, project_id) is None:
        raise ServiceError("PROJECT_NOT_FOUND", "项目不存在。", status_code=404)
    if get_settings().auth_mode == "studio":
        raise ServiceError(
            "OPERATION_NOT_ALLOWED",
            "QuicStudio 模式下请在用户管理中心统一管理用户与角色。",
            status_code=400,
        )
    actor = enforce_actor(
        session,
        project_id=project_id,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="admin",
    )
    user: UserRecord | None = None
    if payload.user_id:
        user = session.get(UserRecord, payload.user_id)
    elif payload.email:
        user = session.scalar(select(UserRecord).where(UserRecord.email == payload.email))
    if user is None:
        if not payload.email or not payload.display_name:
            raise ServiceError(
                "MEMBER_IDENTITY_REQUIRED",
                "新建成员需要 email 与 display_name，或提供已有 user_id。",
                status_code=400,
            )
        user = UserRecord(
            id=payload.user_id or new_id("usr"),
            display_name=payload.display_name,
            email=payload.email,
            role="user",
            active=True,
        )
        session.add(user)
        session.flush()
    if payload.password:
        user.password_hash = hash_password(payload.password)
    membership = session.scalar(
        select(ProjectMembershipRecord).where(
            ProjectMembershipRecord.project_id == project_id,
            ProjectMembershipRecord.user_id == user.id,
        )
    )
    created = membership is None
    if membership is None:
        membership = ProjectMembershipRecord(
            id=new_id("pjm"),
            project_id=project_id,
            user_id=user.id,
            role=payload.role,
        )
        session.add(membership)
    else:
        membership.role = payload.role
    session.add(
        AuditEventRecord(
            id=new_id("aud"),
            actor_id=actor.user_id,
            action="project.member.upsert",
            resource_type="project",
            resource_id=project_id,
            details={"user_id": user.id, "role": payload.role, "created": created},
        )
    )
    session.commit()
    return {
        "membership_id": membership.id,
        "project_id": project_id,
        "user_id": user.id,
        "role": membership.role,
        "created": created,
    }


@app.delete("/api/v1/projects/{project_id}/members/{user_id}", status_code=200)
def delete_project_member(
    project_id: str,
    user_id: str,
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if get_settings().auth_mode == "studio":
        raise ServiceError(
            "OPERATION_NOT_ALLOWED",
            "QuicStudio 模式下请在用户管理中心统一管理用户与角色。",
            status_code=400,
        )
    actor = enforce_actor(
        session,
        project_id=project_id,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="admin",
    )
    membership = session.scalar(
        select(ProjectMembershipRecord).where(
            ProjectMembershipRecord.project_id == project_id,
            ProjectMembershipRecord.user_id == user_id,
        )
    )
    if membership is None:
        raise ServiceError("MEMBERSHIP_NOT_FOUND", "项目成员不存在。", status_code=404)
    session.delete(membership)
    session.add(
        AuditEventRecord(
            id=new_id("aud"),
            actor_id=actor.user_id,
            action="project.member.delete",
            resource_type="project",
            resource_id=project_id,
            details={"user_id": user_id},
        )
    )
    session.commit()
    return {"project_id": project_id, "user_id": user_id, "deleted": True}


@app.get("/api/v1/admin/audit")
def audit_events(
    authorization: str | None = Header(default=None),
    x_quic_actor: str | None = Header(default=None, alias="X-Quic-Actor"),
    x_quic_project: str | None = Header(default=None, alias="X-Quic-Project"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    project_id = x_quic_project or get_settings().default_project_id
    enforce_actor(
        session,
        project_id=project_id,
        authorization=authorization,
        actor_header=x_quic_actor,
        minimum_role="admin",
    )
    events = session.scalars(
        select(AuditEventRecord).order_by(AuditEventRecord.created_at.desc()).limit(100)
    ).all()
    return {
        "items": [
            {
                "id": event.id,
                "actor_id": event.actor_id,
                "action": event.action,
                "resource_type": event.resource_type,
                "resource_id": event.resource_id,
                "details": event.details,
                "created_at": event.created_at.isoformat(),
            }
            for event in events
        ]
    }


def model_summary(record: ModelVersionRecord) -> dict[str, Any]:
    manifest = record.manifest
    return {
        "id": record.model_id,
        "version_id": record.id,
        "name": record.display_name,
        "version": record.version,
        "backend": record.backend,
        "maturity": record.maturity,
        "description": manifest["description"],
        "recipes": manifest["recipes"],
        "upstream_repo": manifest["upstream_repo"],
        "upstream_ref": manifest["upstream_ref"],
        "adapter_version": manifest["adapter_version"],
        "schema_hash": record.schema_hash,
        "image_digest": manifest["image_digest"],
        "selectable": bool(manifest.get("selectable", True)),
        "availability_message": manifest.get("availability_message"),
    }
