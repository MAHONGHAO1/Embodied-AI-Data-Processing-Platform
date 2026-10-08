import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import Integer, and_, cast, exists, func, literal, or_
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from data.database import (
    EGO_REVIEW_ACCEPTED,
    EGO_REVIEW_REJECTED,
    AnnotationRevision,
    AuditRecord,
    CutPlanRevision,
    EgoEpisode,
    Project,
    QrdfData,
    ReviewDecision,
    ReviewFinding,
    Task,
    User,
    get_db,
)
from data.schemas.common import AuditSubmit
from data.security.audit import emit_audit_event
from data.services.annotate_service import (
    annotation_action_tags,
    build_workbench_data,
    load_annotation_doc,
    load_clip_descriptions,
    load_region_frames,
    resolve_scene,
    resolve_task_dataset_storage,
)
from data.services.audit_resource import (
    build_audit_metadata_detail,
    default_episode_id,
    resolve_audit_task_storage,
    sample_audit_episode_frames,
    serve_audit_preview_media,
)
from data.services.audit_resource import (
    get_audit_episode_metadata as fetch_audit_episode_metadata,
)
from data.services.audit_submission import AuditSubmissionConflict, claim_audit_acceptance
from data.services.behavior_tags_service import get_active_vocabulary_snapshot
from data.services.database_errors import is_concurrency_operational_error
from data.services.job_runs import mark_dispatch_uncertain
from data.services.preprocess_facts import sanitize_episode_facts, sanitize_topic_facts
from data.services.public_metadata import (
    serialize_public_qrdf_detail,
    serialize_public_qrdf_episode_detail,
    serialize_public_qrdf_episodes,
    serialize_public_qrdf_quality,
    serialize_public_qrdf_topics,
    serialize_public_review_summary,
)
from data.services.qrdf_promotion import (
    QrdfPromotionRetired,
    enqueue_qrdf_promotion_job,
)
from data.services.quality_facts import sanitize_quality_fact
from data.services.state_machine import transit_task
from data.services.task_dispatcher import dispatch_media_job
from data.services.workspace_access import (
    episode_access_filter,
    require_task_actor,
    require_workspace_actor,
    task_access_filter,
)
from data.utils.formatting import format_api_datetime
from data.utils.helpers import get_current_user, require_permission, success
from data.utils.status_labels import display_status
from data.utils.storage_uri import task_storage_display_uri

router = APIRouter(tags=["数据审核"])
logger = logging.getLogger(__name__)


def _actor_id(user: dict) -> int | None:
    try:
        raw = user.get("sub") or user.get("id")
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _authorization_error(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=403 if isinstance(exc, PermissionError) else 404, detail=str(exc)
    )


def _audit_direct_preview_issuance(result: object, *, user: dict, task_id: int) -> None:
    if not isinstance(result, dict):
        return
    payload = result.get("data")
    if not isinstance(payload, dict) or payload.get("direct") is not True:
        return
    emit_audit_event(
        "file.direct_url",
        actor=str(user.get("email") or ""),
        resource=f"task:{task_id}",
        detail={"delivery": "oss_browser", "kind": "preview"},
    )


def _authorize_task(db: Session, user: dict, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task does not exist")
    try:
        require_task_actor(db, actor_id=_actor_id(user), task=task)
    except (PermissionError, ValueError) as exc:
        raise _authorization_error(exc) from exc
    return task


def _authorize_workspace(db: Session, user: dict, workspace_id: int) -> None:
    try:
        require_workspace_actor(db, actor_id=_actor_id(user), workspace_id=workspace_id)
    except (PermissionError, ValueError) as exc:
        raise _authorization_error(exc) from exc


def _cached_episodes(meta: dict) -> list:
    return sanitize_episode_facts(
        meta.get("episodes") or (meta.get("preprocess") or {}).get("episodes") or []
    )


def _cached_topics(meta: dict) -> list:
    return sanitize_topic_facts(
        meta.get("topics") or (meta.get("preprocess") or {}).get("topics") or []
    )


def _cached_quality(meta: dict) -> dict:
    qc = meta.get("quality_check") or {}
    if qc:
        return sanitize_quality_fact(qc)
    pre = meta.get("preprocess") or {}
    if pre.get("quality"):
        return {"level": pre["quality"], "score": pre.get("quality_score", 0.0)}
    return {}


def _load_quality(task_id: int, meta: dict) -> dict:
    return meta.get("quality_check", {})


def _user_can_audit_write(user: dict) -> bool:
    role = user.get("role", "")
    if role == "admin":
        return True
    perms = user.get("permissions") or []
    if "*" in perms or "audit:write" in perms or "audit:*" in perms:
        return True
    return False


@router.get("/audit/{task_id}/data")
def get_audit_data(
    task_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
):
    """Read-only audit view: annotation summary + task metadata."""
    require_permission(user, "audit:read")
    task = _authorize_task(db, user, task_id)

    meta = task.metadata_json or {}
    storage_path = resolve_audit_task_storage(task)
    vocabulary = get_active_vocabulary_snapshot(db)
    clip_descriptions = load_clip_descriptions(db, task_id, vocabulary=vocabulary)
    region_frames = load_region_frames(db, task_id, vocabulary=vocabulary)
    workbench = build_workbench_data(db, task, vocabulary=vocabulary)
    episode_id = default_episode_id(task, storage_path)
    metadata_detail = serialize_public_qrdf_detail(build_audit_metadata_detail(task, db=db))
    quality_check = serialize_public_qrdf_quality(
        _cached_quality(meta) or _load_quality(task_id, meta)
    )
    episodes = serialize_public_qrdf_episodes(
        _cached_episodes(meta) or metadata_detail.get("episodes") or []
    )
    topics = serialize_public_qrdf_topics(
        _cached_topics(meta) or metadata_detail.get("topics") or []
    )

    display_uri = task_storage_display_uri(task)
    return success(
        {
            "task_id": task.id,
            "status": task.status,
            "reject_reason": task.reject_reason or "",
            "scene": workbench["scene"],
            "preview_available": workbench["preview_available"],
            "preview_error": workbench.get("preview_error"),
            "fps": workbench["fps"],
            "total_frames": workbench.get("total_frames"),
            "task": workbench.get("task"),
            "clip_descriptions": clip_descriptions,
            "annotations": workbench["annotations"],
            "region_frames": region_frames,
            "preview_streams": workbench["preview_streams"],
            "view_toggles": workbench.get("view_toggles"),
            "quality_check": quality_check,
            "episodes": episodes,
            "topics": topics,
            "data_source": task.data_source,
            "storage_path": display_uri,
            "storage_uri": display_uri,
            "episode_id": episode_id,
            "metadata_detail": metadata_detail,
            "read_only": True,
            "can_submit": _user_can_audit_write(user),
        }
    )


@router.get("/audit/{task_id}/metadata")
def get_audit_metadata(
    task_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
):
    """Audit metadata details: Episode / Topic / QC / metrics aggregation (P1)."""
    require_permission(user, "audit:read")
    task = _authorize_task(db, user, task_id)
    detail = serialize_public_qrdf_detail(build_audit_metadata_detail(task, db=db))
    return success({"task_id": task.id, **detail})


@router.get("/audit/{task_id}/episodes/{episode_id}/frames")
def get_audit_episode_frames(
    task_id: int,
    episode_id: str,
    topic: str | None = Query(None),
    start_index: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Audit episode frame sampling: camera thumbnails / status timeseries values (P1)."""
    require_permission(user, "audit:read")
    task = _authorize_task(db, user, task_id)
    payload = sample_audit_episode_frames(
        task,
        episode_id,
        topic=topic,
        start_index=start_index,
        limit=limit,
    )
    if not payload.get("ok"):
        return {"code": 400, "message": payload.get("message", "帧采样失败"), "data": payload}
    return success(payload)


@router.get("/audit/{task_id}/episodes/{episode_id}/metadata")
def audit_episode_metadata(
    task_id: int,
    episode_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Audit single episode metadata and metrics (P1)."""
    require_permission(user, "audit:read")
    task = _authorize_task(db, user, task_id)
    detail = fetch_audit_episode_metadata(task, episode_id)
    if not detail.get("ok"):
        return {"code": 404, "message": "Episode 不存在", "data": detail}
    return success(serialize_public_qrdf_episode_detail(detail))


@router.get("/audit/{task_id}/preview/media")
def audit_preview_media(
    task_id: int,
    topic: str = Query("head_color"),
    direct: bool = False,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Audit video preview stream (P1)."""
    require_permission(user, "audit:read")
    task = _authorize_task(db, user, task_id)
    result = serve_audit_preview_media(task, topic, direct=direct)
    if direct:
        _audit_direct_preview_issuance(result, user=user, task_id=task_id)
    if isinstance(result, FileResponse):
        return result
    return result


@router.get("/audit/{task_id}/{topic}/preview/media")
def audit_preview_media_by_path(
    task_id: int,
    topic: str,
    direct: bool = False,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Audit video preview stream (topic path parameter)."""
    require_permission(user, "audit:read")
    task = _authorize_task(db, user, task_id)
    result = serve_audit_preview_media(task, topic, direct=direct)
    if direct:
        _audit_direct_preview_issuance(result, user=user, task_id=task_id)
    if isinstance(result, FileResponse):
        return result
    return result


@router.post("/audit/{task_id}/submit")
def submit_audit(
    task_id: int,
    body: AuditSubmit,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "audit:write")
    task = _authorize_task(db, user, task_id)
    if task.status not in ("pending_audit", "annotate_done", "rejected"):
        if body.is_passed:
            return JSONResponse(
                status_code=409,
                content={
                    "code": 409,
                    "message": "task audit state changed concurrently; retry",
                    "data": None,
                },
            )
        return {"code": 400, "message": "任务不在可审核状态", "data": None}

    auditor = user.get("email", "")
    qrdf_id = None
    promotion_job = None
    promotion_retired = False
    try:
        if body.is_passed:
            task = claim_audit_acceptance(
                db,
                task.id,
                operator=auditor,
                note=body.reject_reason,
            )
            meta = task.metadata_json or {}
            dataset_storage = resolve_task_dataset_storage(task) or task.storage_path
            episodes = sanitize_episode_facts(_cached_episodes(meta))
            topics = sanitize_topic_facts(_cached_topics(meta))
            quality = _cached_quality(meta)
            tags = annotation_action_tags(load_annotation_doc(db, task.id, task=task))
            project = db.get(Project, task.project_id)
            scene = resolve_scene(task, project, meta)
            qrdf = QrdfData(
                task_id=task.id,
                project_id=task.project_id,
                name=task.name,
                data_source=task.data_source,
                scene=scene,
                storage_path=dataset_storage,
                metadata_json={
                    "episodes": episodes,
                    "topics": topics,
                    "quality_check": quality,
                    "tags": tags,
                },
                quality_level=quality.get("level", "unknown") if quality else "unknown",
            )
            db.add(qrdf)
            db.flush()
            qrdf_id = qrdf.id
            try:
                promotion_job = enqueue_qrdf_promotion_job(
                    db,
                    task=task,
                    qrdf=qrdf,
                    actor_id=_actor_id(user),
                )
            except QrdfPromotionRetired:
                # The audit acceptance and QRDF metadata remain committed; the
                # removed official-promotion side effect must not roll them back.
                promotion_retired = True
        else:
            task = transit_task(db, task, "audit_reject", auditor, body.reject_reason)
            # Rejection termination -> transit_task has already triggered cleanup of local cloud/ mirrors automatically
    except AuditSubmissionConflict:
        db.rollback()
        return JSONResponse(
            status_code=409,
            content={
                "code": 409,
                "message": "task audit state changed concurrently; retry",
                "data": None,
            },
        )
    except OperationalError as exc:
        db.rollback()
        is_conflict = is_concurrency_operational_error(db, exc)
        logger.error(
            "audit submit database error task_id=%s error_type=%s concurrency=%s",
            task_id,
            type(exc).__name__,
            is_conflict,
        )
        response_status = 409 if is_conflict else 503
        return JSONResponse(
            status_code=response_status,
            content={
                "code": response_status,
                "message": (
                    "task audit state changed concurrently; retry"
                    if is_conflict
                    else "database service is unavailable"
                ),
                "data": None,
            },
        )
    except ValueError as e:
        db.rollback()
        return {"code": 400, "message": str(e), "data": None}

    record = AuditRecord(
        task_id=task.id,
        qrdf_id=qrdf_id,
        auditor=auditor,
        is_passed="true" if body.is_passed else "false",
        reject_reason=body.reject_reason,
        remark=body.reject_reason,
    )
    db.add(record)
    db.commit()

    if promotion_retired:
        return JSONResponse(
            status_code=410,
            content={
                "code": 410,
                "message": "QRDF promotion is retired; use process/export artifacts",
                "data": {
                    "task_id": str(task.id),
                    "qrdf_id": qrdf_id,
                    "next_status": display_status(task.status),
                },
            },
        )

    if promotion_job is not None:
        try:
            dispatch_media_job(promotion_job, worker_prechecked=True)
        except Exception as exc:
            logger.error(
                "QRDF promotion dispatch failed job_id=%s error_type=%s",
                promotion_job.id,
                type(exc).__name__,
            )
            promotion_job = mark_dispatch_uncertain(db, promotion_job.id)
            return JSONResponse(
                status_code=503,
                content={
                    "code": 503,
                    "message": "job dispatch failed",
                    "data": {"job_id": promotion_job.id, "status": promotion_job.status},
                },
            )

    return success(
        {
            "task_id": str(task.id),
            "next_status": display_status(task.status),
            "qrdf_id": qrdf_id,
            "job_id": promotion_job.id if promotion_job is not None else None,
            "message": "审核通过，正在后台入库到 QRDF 数据集…" if body.is_passed else None,
        }
    )


@router.get("/audit/records")
def audit_records(
    task_id: int | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "audit:read")
    actor_id = _actor_id(user)
    q = (
        db.query(AuditRecord)
        .join(Task, Task.id == AuditRecord.task_id)
        .filter(task_access_filter(db, actor_id=actor_id))
    )
    if task_id:
        _authorize_task(db, user, task_id)
        q = q.filter(AuditRecord.task_id == task_id)
    total = q.count()
    records = q.order_by(AuditRecord.created_at.desc()).offset((page - 1) * size).limit(size).all()
    items = []
    for r in records:
        task = db.get(Task, r.task_id)
        meta = (task.metadata_json or {}) if task else {}
        quality_check = serialize_public_qrdf_quality(_cached_quality(meta))
        items.append(
            {
                "id": r.id,
                "task_id": r.task_id,
                "qrdf_id": r.qrdf_id,
                "auditor": r.auditor,
                "is_passed": r.is_passed == "true",
                "reject_reason": r.reject_reason,
                "created_at": format_api_datetime(r.created_at),
                "quality_check": quality_check,
                "quality_level": quality_check.get("level", ""),
            }
        )
    return success(
        {
            "total": total,
            "list": items,
        }
    )


@router.get("/audit/ego-records")
def ego_audit_records(
    workspace_id: int | None = Query(None, gt=0),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Project immutable EGO review decisions into the shared audit-history view."""
    require_permission(user, "audit:read")
    if workspace_id is not None:
        _authorize_workspace(db, user, workspace_id)
    actor_id = _actor_id(user)
    episode_id = func.coalesce(AnnotationRevision.ego_episode_id, CutPlanRevision.source_episode_id)
    immutable_query = (
        db.query(
            literal("immutable").label("record_kind"),
            ReviewDecision.id.label("record_id"),
            ReviewDecision.subject_type.label("resource_type"),
            EgoEpisode.task_id.label("task_id"),
            EgoEpisode.id.label("episode_id"),
            ReviewDecision.auditor_id.label("auditor_id"),
            ReviewDecision.decision.label("decision"),
            ReviewDecision.decided_at.label("created_at"),
        )
        .select_from(ReviewDecision)
        .outerjoin(
            AnnotationRevision, ReviewDecision.annotation_revision_id == AnnotationRevision.id
        )
        .outerjoin(CutPlanRevision, ReviewDecision.cut_plan_revision_id == CutPlanRevision.id)
        .join(EgoEpisode, EgoEpisode.id == episode_id)
        .filter(episode_access_filter(db, actor_id=actor_id))
    )
    if workspace_id is not None:
        immutable_query = immutable_query.filter(EgoEpisode.workspace_id == workspace_id)

    annotation_decision_exists = exists().where(
        and_(
            AnnotationRevision.ego_episode_id == EgoEpisode.id,
            ReviewDecision.annotation_revision_id == AnnotationRevision.id,
        )
    )
    cut_decision_exists = exists().where(
        and_(
            CutPlanRevision.source_episode_id == EgoEpisode.id,
            ReviewDecision.cut_plan_revision_id == CutPlanRevision.id,
        )
    )
    legacy_query = db.query(
        literal("legacy").label("record_kind"),
        EgoEpisode.id.label("record_id"),
        literal("ego_episode").label("resource_type"),
        EgoEpisode.task_id.label("task_id"),
        EgoEpisode.id.label("episode_id"),
        cast(None, Integer).label("auditor_id"),
        EgoEpisode.review_status.label("decision"),
        EgoEpisode.updated_at.label("created_at"),
    ).filter(
        episode_access_filter(db, actor_id=actor_id),
        EgoEpisode.review_status.in_((EGO_REVIEW_ACCEPTED, EGO_REVIEW_REJECTED)),
        ~or_(annotation_decision_exists, cut_decision_exists),
    )
    if workspace_id is not None:
        legacy_query = legacy_query.filter(EgoEpisode.workspace_id == workspace_id)

    history = immutable_query.union_all(legacy_query).subquery("ego_audit_history")
    total = db.query(func.count()).select_from(history).scalar() or 0
    rows = (
        db.query(history)
        .order_by(
            history.c.created_at.desc(), history.c.record_kind.asc(), history.c.record_id.desc()
        )
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    review_ids = [row.record_id for row in rows if row.record_kind == "immutable"]
    legacy_episode_ids = [row.episode_id for row in rows if row.record_kind == "legacy"]
    reviews_by_id = (
        {
            review.id: review
            for review in db.query(ReviewDecision).filter(ReviewDecision.id.in_(review_ids)).all()
        }
        if review_ids
        else {}
    )
    findings_by_review_id: dict[int, list[ReviewFinding]] = {}
    if review_ids:
        for finding in (
            db.query(ReviewFinding)
            .filter(ReviewFinding.review_decision_id.in_(review_ids))
            .order_by(ReviewFinding.review_decision_id, ReviewFinding.id)
            .all()
        ):
            findings_by_review_id.setdefault(finding.review_decision_id, []).append(finding)
    auditor_ids = {review.auditor_id for review in reviews_by_id.values()}
    auditors_by_id = (
        {auditor.id: auditor for auditor in db.query(User).filter(User.id.in_(auditor_ids)).all()}
        if auditor_ids
        else {}
    )
    legacy_episodes = (
        {
            episode.id: episode
            for episode in db.query(EgoEpisode).filter(EgoEpisode.id.in_(legacy_episode_ids)).all()
        }
        if legacy_episode_ids
        else {}
    )

    items = []
    for row in rows:
        if row.record_kind == "immutable":
            review = reviews_by_id[row.record_id]
            findings = findings_by_review_id.get(review.id, [])
            serialized_findings = [
                {
                    "domain": finding.domain,
                    "code": finding.code,
                    "attribution": finding.attribution,
                    "segment_id": finding.annotation_segment_id or finding.cut_segment_id,
                    "note": finding.note,
                }
                for finding in findings
            ]
            items.append(
                {
                    "id": f"review-{review.id}",
                    "resource_type": row.resource_type,
                    "source_label": f"EGO 资产 #{row.episode_id}",
                    "task_id": row.task_id,
                    "episode_id": row.episode_id,
                    "auditor": auditors_by_id.get(review.auditor_id).email
                    if review.auditor_id in auditors_by_id
                    else "",
                    "is_passed": review.decision == EGO_REVIEW_ACCEPTED,
                    "reject_reason": "\n".join(
                        filter(None, (finding.note or finding.code for finding in findings))
                    ),
                    "findings": serialized_findings,
                    "created_at": format_api_datetime(row.created_at),
                    "quality_check": {"level": "valid"},
                    "quality_level": "valid",
                }
            )
            continue
        episode = legacy_episodes[row.episode_id]
        legacy_review = serialize_public_review_summary((episode.metadata_json or {}).get("review"))
        items.append(
            {
                "id": f"ego-{episode.id}",
                "resource_type": "ego_episode",
                "source_label": f"EGO 资产 #{episode.id}",
                "task_id": episode.task_id,
                "episode_id": episode.id,
                "auditor": str(legacy_review.get("operator") or ""),
                "is_passed": episode.review_status == EGO_REVIEW_ACCEPTED,
                "reject_reason": str(legacy_review.get("reason") or ""),
                "findings": [],
                "created_at": format_api_datetime(row.created_at),
                "quality_check": {"level": "valid"},
                "quality_level": "valid",
            }
        )
    return success({"total": total, "list": items})
