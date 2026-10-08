"""Episode read APIs. Storage URIs are deliberately not exposed here."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from data.database import (
    CollectionDevice,
    Episode,
    EpisodeArtifact,
    JobRun,
    PersonnelProfile,
    TaskSet,
    User,
    WorkspacePersonnelProfile,
    get_db,
)
from data.models.collection_core import CollectionProject
from data.realtime.outbox import enqueue_resource_event
from data.realtime.projections import job_run_snapshot
from data.realtime.socketio import schedule_realtime_dispatch
from data.security.audit import emit_audit_event
from data.security.signed_url import consume_download_jti, verify_signed_download_token
from data.services.episode_asset_projection import episode_asset_states
from data.services.episode_media_access import (
    issue_episode_preview_fallback_url,
    issue_episode_preview_url,
    resolve_episode_preview_file,
)
from data.services.episode_multimodal import (
    EpisodeAiSuggestionError,
    EpisodeMultimodalError,
    ai_suggestions_projection,
    create_episode_ai_suggestion,
    frame_projection,
    multimodal_session_projection,
    retry_episode_ai_suggestion,
    timeline_projection,
    timeseries_projection,
)
from data.services.episode_observability import episode_assets_projection
from data.services.episode_raw_source_access import (
    issue_raw_source_file_access,
    raw_source_artifact_for_episode,
    raw_source_file_names,
    raw_source_file_size_bytes,
    raw_source_local_file_path,
)
from data.services.episode_workbench import (
    WorkbenchError,
    effective_collector_attribution,
    effective_device_attribution,
    workbench_snapshot,
)
from data.services.episodes import list_episode_assets_page, list_episode_page
from data.services.public_metadata import serialize_public_packet_metadata
from data.services.task_dispatcher import (
    JobDispatchUnavailable,
    celery_available,
    dispatch_media_job,
    require_celery_worker,
)
from data.services.work_queue_projection import episode_summary, publication_summary
from data.services.workspace_access import require_episode_actor, require_workspace_actor
from data.utils.formatting import format_api_datetime
from data.utils.helpers import get_current_user, get_optional_user, require_permission, success

router = APIRouter(prefix="/episodes", tags=["Episode"])


class AiSuggestionRequest(BaseModel):
    rgb_topic: str = Field(min_length=1, max_length=512)


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub"))
    except (TypeError, ValueError):
        return None


def _require_collection_project(
    db: Session,
    *,
    workspace_id: int,
    project_id: int | None,
) -> None:
    """Reject unknown or cross-workspace collection project filters."""

    if project_id is None:
        return
    project = db.get(CollectionProject, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="collection project does not exist")
    if int(project.workspace_id) != int(workspace_id):
        raise HTTPException(
            status_code=403, detail="collection project is outside the requested workspace"
        )


def _require_workspace_filter_resource(
    db: Session,
    *,
    model: type[PersonnelProfile] | type[CollectionDevice],
    resource_id: int | None,
    workspace_id: int,
    label: str,
) -> None:
    if resource_id is None:
        return
    resource = db.get(model, resource_id)
    if resource is None:
        raise HTTPException(status_code=404, detail=f"{label} does not exist")
    if model is PersonnelProfile:
        # Historical assets remain searchable after a collector leaves employment.
        in_workspace = (
            db.query(WorkspacePersonnelProfile)
            .filter_by(
                workspace_id=workspace_id,
                personnel_profile_id=resource_id,
            )
            .first()
            is not None
        )
    else:
        in_workspace = resource.workspace_id == workspace_id
    if not in_workspace:
        raise HTTPException(status_code=403, detail=f"{label} is outside the requested workspace")


def episode_item(
    db: Session,
    episode: Episode,
    *,
    actor_id: int | None,
    include_artifacts: bool = False,
    asset_state: dict[str, object] | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        **episode_summary(episode),
        "workspace_id": episode.workspace_id,
        "task_set_id": episode.task_set_id,
        "import_session_id": episode.import_session_id,
        "derivation_version": episode.derivation_version,
        "embodiment_id": episode.embodiment_id,
        "task_label_id": episode.task_label_id,
        "task_language": episode.task_language,
        "source_start_ns": str(episode.source_start_ns)
        if episode.source_start_ns is not None
        else None,
        "source_end_ns": str(episode.source_end_ns) if episode.source_end_ns is not None else None,
        "publication": (
            asset_state["publication"]
            if asset_state is not None
            else publication_summary(db, episode=episode, actor_id=actor_id)
        ),
        "created_at": format_api_datetime(episode.created_at),
        "updated_at": format_api_datetime(episode.updated_at),
    }
    if asset_state is not None:
        item["human_work"] = asset_state["human_work"]
        item["attribution"] = asset_state["attribution"]
    if include_artifacts:
        item["attribution"] = {
            "collector": effective_collector_attribution(db, episode=episode),
            "device": effective_device_attribution(db, episode=episode),
        }
        item["quality_diagnostic"] = _quality_diagnostic(db, episode=episode, actor_id=actor_id)
        item["artifacts"] = [
            {
                "id": artifact.id,
                "artifact_type": artifact.artifact_type,
                "storage_role": artifact.storage_role,
                "size_bytes": artifact.size_bytes,
                "retention_policy": artifact.retention_policy,
                "retention_until": format_api_datetime(artifact.retention_until)
                if artifact.retention_until
                else None,
            }
            for artifact in episode.artifacts
        ]
        item["packet_metadata"] = serialize_public_packet_metadata(episode.metadata_json)
    return item


def _quality_diagnostic(
    db: Session, *, episode: Episode, actor_id: int | None
) -> dict[str, object] | None:
    if episode.quality_status != "failed":
        return None
    job = (
        db.query(JobRun)
        .filter(
            JobRun.kind == "episode_quality",
            JobRun.resource_type == "episode",
            JobRun.resource_id == str(episode.id),
        )
        .order_by(JobRun.created_at.desc(), JobRun.id.desc())
        .first()
    )
    if job is None:
        return {
            "job_id": None,
            "error_code": "episode_quality_failed",
            "summary": "quality_check_failed",
            "phase": "quality",
            "occurred_at": format_api_datetime(episode.updated_at),
            "attempt": 1,
            "suggested_action": "inspect_source_and_retry",
            "retry_allowed": False,
        }
    actor = db.get(User, actor_id) if actor_id is not None else None
    failure = _quality_failure_projection(job)
    return {
        "job_id": job.id,
        **failure,
        "occurred_at": format_api_datetime(job.finished_at or job.updated_at),
        "attempt": max(1, int(job.attempt_count or 0), int(job.retry_count or 0) + 1),
        "retry_allowed": bool(actor is not None and actor.role == "admin"),
    }


_QUALITY_FAILURE_BY_MESSAGE = {
    "source metadata is unavailable": (
        "metadata_invalid",
        "metadata_invalid",
        "metadata",
        "inspect_metadata_and_retry",
    ),
    "source metadata is invalid": (
        "metadata_invalid",
        "metadata_invalid",
        "metadata",
        "inspect_metadata_and_retry",
    ),
    "raw source artifact is unavailable": (
        "raw_source_unavailable",
        "raw_source_unavailable",
        "raw_source",
        "restore_raw_source_and_retry",
    ),
    "source artifact path is unavailable": (
        "raw_source_unavailable",
        "raw_source_unavailable",
        "raw_source",
        "restore_raw_source_and_retry",
    ),
    "source data file is unavailable": (
        "raw_source_unavailable",
        "raw_source_unavailable",
        "raw_source",
        "restore_raw_source_and_retry",
    ),
    "source MCAP structural scan failed": (
        "raw_source_unavailable",
        "raw_source_unavailable",
        "raw_source",
        "inspect_recording_and_retry",
    ),
    "reference camera timeline is unavailable": (
        "reference_topic_missing",
        "reference_topic_missing",
        "reference_timeline",
        "inspect_recording_and_retry",
    ),
    "reference camera timeline does not cover source": (
        "reference_timeline_incomplete",
        "reference_timeline_incomplete",
        "reference_timeline",
        "inspect_recording_and_retry",
    ),
    "source timeline is unavailable": (
        "reference_timeline_incomplete",
        "reference_timeline_incomplete",
        "reference_timeline",
        "inspect_recording_and_retry",
    ),
}


def _quality_failure_projection(job: JobRun) -> dict[str, str]:
    if job.error_code == "worker_lease_expired":
        return {
            "error_code": "worker_lease_expired",
            "summary": "worker_interrupted",
            "phase": "quality",
            "suggested_action": "retry",
        }
    code, summary, phase, action = _QUALITY_FAILURE_BY_MESSAGE.get(
        str(job.error_message or ""),
        ("episode_quality_failed", "quality_check_failed", "quality", "inspect_source_and_retry"),
    )
    return {"error_code": code, "summary": summary, "phase": phase, "suggested_action": action}


@router.get("")
def list_episode_endpoint(
    workspace_id: int = Query(..., gt=0),
    task_set_id: int | None = Query(default=None, gt=0),
    batch_id: int | None = Query(default=None, gt=0),
    collection_project_id: int | None = Query(default=None, gt=0),
    modality: str | None = Query(default=None, max_length=32),
    embodiment_id: int | None = Query(default=None, gt=0),
    task_label_id: int | None = Query(default=None, gt=0),
    scene: str | None = Query(default=None, max_length=64),
    workflow_status: str | None = Query(default=None, max_length=32),
    kind: Literal["source", "derived"] | None = Query(default=None),
    annotation_status: Literal["pending", "submitted", "annotated", "accepted", "rejected"]
    | None = Query(default=None),
    review_status: Literal["pending", "accepted", "rejected", "not_applicable"] | None = Query(
        default=None
    ),
    publication_status: Literal["unpublished", "publishing", "published", "failed"] | None = Query(
        default=None
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:read")
    try:
        require_workspace_actor(db, actor_id=_actor_id(user), workspace_id=workspace_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if task_set_id is not None:
        task_set = db.get(TaskSet, task_set_id)
        if task_set is None:
            raise HTTPException(status_code=404, detail="task set does not exist")
        if int(task_set.workspace_id) != int(workspace_id):
            raise HTTPException(
                status_code=403, detail="task set is outside the requested workspace"
            )
    _require_collection_project(db, workspace_id=workspace_id, project_id=collection_project_id)
    try:
        episodes, total = list_episode_page(
            db,
            workspace_id=workspace_id,
            task_set_id=task_set_id,
            batch_id=batch_id,
            collection_project_id=collection_project_id,
            modality=modality,
            embodiment_id=embodiment_id,
            task_label_id=task_label_id,
            scene=scene,
            workflow_status=workflow_status,
            kind=kind,
            annotation_status=annotation_status,
            review_status=review_status,
            publication_status=publication_status,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    actor_id = _actor_id(user)
    states = episode_asset_states(db, episodes=episodes)
    return success(
        {
            "items": [
                episode_item(db, episode, actor_id=actor_id, asset_state=states[episode.id])
                for episode in episodes
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@router.get("/assets")
def list_episode_assets_endpoint(
    workspace_id: int = Query(..., gt=0),
    task_set_id: int | None = Query(default=None, gt=0),
    collection_project_id: int | None = Query(default=None, gt=0),
    modality: str | None = Query(default=None, max_length=32),
    task_label_id: int | None = Query(default=None, gt=0),
    kind: Literal["source", "derived"] | None = Query(default=None),
    keyword: str | None = Query(default=None, max_length=128),
    collector_profile_id: int | None = Query(default=None, gt=0),
    collection_device_id: int | None = Query(default=None, gt=0),
    published_from: datetime | None = Query(default=None),
    published_to: datetime | None = Query(default=None),
    sort_by: Literal["published_at", "created_at", "updated_at", "episode_uid", "duration"] = Query(
        default="published_at"
    ),
    sort_order: Literal["asc", "desc"] = Query(default="desc"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:read")
    try:
        require_workspace_actor(db, actor_id=_actor_id(user), workspace_id=workspace_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if task_set_id is not None:
        task_set = db.get(TaskSet, task_set_id)
        if task_set is None:
            raise HTTPException(status_code=404, detail="task set does not exist")
        if int(task_set.workspace_id) != int(workspace_id):
            raise HTTPException(
                status_code=403, detail="task set is outside the requested workspace"
            )
    _require_collection_project(db, workspace_id=workspace_id, project_id=collection_project_id)
    _require_workspace_filter_resource(
        db,
        model=PersonnelProfile,
        resource_id=collector_profile_id,
        workspace_id=workspace_id,
        label="collector profile",
    )
    _require_workspace_filter_resource(
        db,
        model=CollectionDevice,
        resource_id=collection_device_id,
        workspace_id=workspace_id,
        label="collection device",
    )
    try:
        rows, total = list_episode_assets_page(
            db,
            workspace_id=workspace_id,
            task_set_id=task_set_id,
            modality=modality,
            task_label_id=task_label_id,
            kind=kind,
            keyword=keyword,
            collector_profile_id=collector_profile_id,
            collection_device_id=collection_device_id,
            published_from=published_from,
            published_to=published_to,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    episodes = [episode for episode, _publication in rows]
    states = episode_asset_states(db, episodes=episodes)
    actor_id = _actor_id(user)
    items = []
    for episode, publication in rows:
        state = dict(states[episode.id])
        state["publication"] = {
            "status": "succeeded",
            "category": "published",
            "job_id": None,
            "retry_allowed": False,
        }
        item = episode_item(db, episode, actor_id=actor_id, asset_state=state)
        item["published_at"] = format_api_datetime(publication.published_at)
        items.append(item)
    return success({"items": items, "total": total, "limit": limit, "offset": offset})


@router.get("/{episode_id}/workbench")
def get_episode_workbench_endpoint(
    episode_id: int,
    work_item_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Return a safe, authorized snapshot for one cut/annotation/review item."""
    require_permission(user, "episode:read")
    try:
        return success(
            workbench_snapshot(
                db,
                episode_id=episode_id,
                work_item_id=work_item_id,
                actor_id=_actor_id(user),
            )
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except WorkbenchError as exc:
        status_code = (
            404 if str(exc) in {"episode_unavailable", "work_item_episode_mismatch"} else 422
        )
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.get("/{episode_id}/timeline")
def get_episode_timeline(
    episode_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:read")
    episode = _authorized_episode_or_404(db, episode_id=episode_id, user=user)
    try:
        return success(timeline_projection(episode))
    except EpisodeMultimodalError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{episode_id}/multimodal/session")
def get_episode_multimodal_session(
    episode_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:read")
    episode = _authorized_episode_or_404(db, episode_id=episode_id, user=user)
    return success(multimodal_session_projection(episode))


@router.get("/{episode_id}/multimodal/frame")
def get_episode_multimodal_frame(
    episode_id: int,
    stream_id: str = Query(..., min_length=1, max_length=512),
    timestamp_ns: str = Query(..., min_length=1, max_length=20),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:read")
    episode = _authorized_episode_or_404(db, episode_id=episode_id, user=user)
    try:
        return success(frame_projection(episode, stream_id=stream_id, timestamp_ns=timestamp_ns))
    except EpisodeMultimodalError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{episode_id}/timeseries")
def get_episode_timeseries(
    episode_id: int,
    series_id: str = Query(..., min_length=1, max_length=512),
    limit: int = Query(default=500, ge=1, le=2000),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:read")
    episode = _authorized_episode_or_404(db, episode_id=episode_id, user=user)
    try:
        return success(timeseries_projection(episode, series_id=series_id, limit=limit))
    except EpisodeMultimodalError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{episode_id}/ai-suggestions")
def get_episode_ai_suggestions(
    episode_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:read")
    episode = _authorized_episode_or_404(db, episode_id=episode_id, user=user)
    return success(
        ai_suggestions_projection(
            db,
            episode=episode,
            worker_available=celery_available("ai"),
        )
    )


@router.post("/{episode_id}/ai-suggestions")
def create_episode_ai_suggestions(
    episode_id: int,
    body: AiSuggestionRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:annotate")
    episode = _authorized_episode_or_404(db, episode_id=episode_id, user=user)
    try:
        require_celery_worker("ai")
        job, created = create_episode_ai_suggestion(
            db,
            episode=episode,
            actor_id=_actor_id(user) or 0,
            rgb_topic=body.rgb_topic,
        )
        if created:
            enqueue_resource_event(
                db,
                resource=job,
                resource_type="job_run",
                event_name="job_run.updated",
                resource_snapshot=job_run_snapshot(job),
            )
        db.commit()
        db.refresh(job)
        dispatch = dispatch_media_job(job, worker_prechecked=True) if created else "already_exists"
    except JobDispatchUnavailable as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="ai worker is unavailable") from exc
    except (EpisodeMultimodalError, EpisodeAiSuggestionError) as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PermissionError as exc:
        db.rollback()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    emit_audit_event(
        "episode.ai_suggestion.create",
        actor=str(user.get("email") or ""),
        resource=f"episode:{episode.id}",
        detail={"job_id": job.id},
    )
    schedule_realtime_dispatch()
    return success({"job": _ai_job_response(job), "dispatch": dispatch})


@router.post("/{episode_id}/ai-suggestions/{job_id}/retry")
def retry_episode_ai_suggestions(
    episode_id: int,
    job_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:annotate")
    episode = _authorized_episode_or_404(db, episode_id=episode_id, user=user)
    try:
        require_celery_worker("ai")
        job, created = retry_episode_ai_suggestion(
            db,
            episode=episode,
            job_id=job_id,
            actor_id=_actor_id(user) or 0,
        )
        if created:
            enqueue_resource_event(
                db,
                resource=job,
                resource_type="job_run",
                event_name="job_run.updated",
                resource_snapshot=job_run_snapshot(job),
            )
        db.commit()
        db.refresh(job)
        dispatch = dispatch_media_job(job, worker_prechecked=True) if created else "already_exists"
    except JobDispatchUnavailable as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="ai worker is unavailable") from exc
    except (EpisodeMultimodalError, EpisodeAiSuggestionError) as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PermissionError as exc:
        db.rollback()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    emit_audit_event(
        "episode.ai_suggestion.retry",
        actor=str(user.get("email") or ""),
        resource=f"episode:{episode.id}",
        detail={"job_id": job.id},
    )
    schedule_realtime_dispatch()
    return success({"job": _ai_job_response(job), "dispatch": dispatch})


@router.get("/{episode_id}")
def get_episode_endpoint(
    episode_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:read")
    episode = _authorized_episode_or_404(db, episode_id=episode_id, user=user)
    return success(episode_item(db, episode, actor_id=_actor_id(user), include_artifacts=True))


def _admission_fact_preview_access(db: Session, *, episode: Episode) -> dict[str, object] | None:
    """Sign a verified collection preview when no EpisodeArtifact row exists yet.

    Collection admission stores the immutable preview identities in the current
    admission fact.  Older rows may not have a materialized ``process_preview``
    EpisodeArtifact, so the read endpoint must still be able to serve the
    already-verified object without changing the Episode or its review record.
    """
    from data.infra.storage_provider import get_storage_provider
    from data.services.episode_admission import current_episode_admission_fact
    from data.services.episode_objects import EpisodeObjectsError, fact_objects, preview_streams

    fact = current_episode_admission_fact(db, episode_id=episode.id)
    if (
        fact is None
        or not fact.is_current
        or fact.source_fingerprint != episode.source_fingerprint
        or fact.preview_status != "ready"
        or fact.output_verification_status != "verified"
    ):
        return None
    try:
        streams = preview_streams(fact_objects(fact))
    except EpisodeObjectsError:
        return None
    try:
        provider = get_storage_provider()
    except Exception:
        return None
    for stream in streams:
        ref = stream["video"].storage_ref()
        if ref.bucket_role != "process" or not ref.object_key or not (ref.version_id or ref.etag):
            continue
        try:
            url = provider.sign_get(ref, expires=900)
        except Exception:
            continue
        if not url:
            continue
        return {
            "available": True,
            "direct": True,
            "url": url,
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=900)).isoformat(),
            "media_type": "video/mp4",
        }
    return None


@router.get("/{episode_id}/preview-url")
def get_episode_preview_url_endpoint(
    episode_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Return an authorized, short-lived browser preview descriptor only."""
    require_permission(user, "episode:read")
    episode = _authorized_episode_or_404(db, episode_id=episode_id, user=user)

    artifact = next(
        (item for item in episode.artifacts if item.artifact_type == "process_preview"),
        None,
    )
    media_type = "video/mp4"
    if artifact is not None and isinstance(artifact.metadata_json, dict):
        candidate = artifact.metadata_json.get("media_type")
        if isinstance(candidate, str) and candidate in {"video/mp4", "video/webm"}:
            media_type = candidate
    actor_id = _actor_id(user)
    access = None
    if artifact is not None:
        uri = str(artifact.storage_uri or "")
        if uri.startswith("oss://"):
            access = issue_episode_preview_url(
                uri,
                download_name=f"{episode.episode_uid}.mp4",
                media_type=media_type,
            )
        if access is None:
            access = issue_episode_preview_fallback_url(
                uri,
                episode_id=episode.id,
                actor_id=actor_id,
                media_type=media_type,
            )
    if access is None:
        admission_access = _admission_fact_preview_access(db, episode=episode)
        if admission_access is not None:
            return success(admission_access)

    if access is None:
        return success(
            {
                "available": False,
                "direct": False,
                "url": "",
                "expires_at": "",
                "media_type": media_type,
            }
        )
    return success({"available": True, **access.as_payload()})


@router.get("/{episode_id}/raw-source/downloads")
def get_raw_source_downloads_endpoint(
    episode_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Issue exact raw-source file descriptors after Episode authorization."""
    require_permission(user, "episode:read")
    episode = _authorized_episode_or_404(db, episode_id=episode_id, user=user)
    artifact = raw_source_artifact_for_episode(episode=episode)
    if artifact is None:
        return success({"available": False, "artifact_id": None, "files": []})
    actor_id = _actor_id(user)
    files = []
    for file_name in raw_source_file_names(episode=episode, artifact=artifact):
        access = issue_raw_source_file_access(
            episode=episode,
            artifact=artifact,
            file_name=file_name,
            actor_id=actor_id,
        )
        size_bytes = raw_source_file_size_bytes(
            episode=episode,
            artifact=artifact,
            file_name=file_name,
        )
        files.append(
            {
                "name": file_name,
                "size_bytes": size_bytes,
                "available": access is not None,
                **(
                    access.as_payload()
                    if access is not None
                    else {
                        "direct": False,
                        "url": "",
                        "expires_at": "",
                        "media_type": "application/octet-stream",
                    }
                ),
            }
        )
        if access is not None:
            emit_audit_event(
                "episode.artifact.download",
                actor=str(user.get("email") or ""),
                resource=f"episode_artifact:{artifact.id}",
                detail={
                    "workspace_id": episode.workspace_id,
                    "episode_id": episode.id,
                    "artifact_type": "raw_source",
                    "delivery": "direct" if access.direct else "same_origin",
                },
            )
    return success(
        {
            "available": any(item["available"] for item in files),
            "artifact_id": artifact.id,
            "files": files,
        }
    )


@router.get("/{episode_id}/raw-source/media")
def stream_raw_source_media(
    episode_id: int,
    file: str = Query(..., min_length=1, max_length=256),
    sig: str | None = Query(default=None, max_length=4096),
    db: Session = Depends(get_db),
    user: dict | None = Depends(get_optional_user),
):
    """Stream one local raw file after token or bearer authorization."""
    signed_payload: dict[str, object] | None = None
    artifact: EpisodeArtifact | None = None
    if sig:
        try:
            signed_payload = verify_signed_download_token(
                sig,
                resource_type="episode_raw_source",
                resource_id=episode_id,
            )
            actor_id = _actor_id({"sub": signed_payload.get("sub")})
            artifact_id = signed_payload.get("artifact_id")
            signed_file = signed_payload.get("file")
        except ValueError as exc:
            raise HTTPException(
                status_code=401, detail="raw source signature is invalid or expired"
            ) from exc
        if actor_id is None or type(artifact_id) is not int or signed_file != file:
            raise HTTPException(
                status_code=401, detail="raw source signature is invalid or expired"
            )
        artifact = db.get(EpisodeArtifact, artifact_id)
        actor_label = str(actor_id)
    else:
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        require_permission(user, "episode:read")
        actor_id = _actor_id(user)
        actor_label = str(user.get("email") or "")

    episode = _authorized_episode_for_actor_or_404(db, episode_id=episode_id, actor_id=actor_id)
    if artifact is None:
        artifact = raw_source_artifact_for_episode(episode=episode)
    if artifact is None or artifact.episode_id != episode.id:
        raise HTTPException(status_code=404, detail="raw source artifact is unavailable")
    if signed_payload is not None:
        max_uses = int(signed_payload.get("max_uses") or 1)
        if not consume_download_jti(str(signed_payload.get("jti") or ""), max_uses):
            raise HTTPException(status_code=403, detail="raw source signature use limit reached")
    path = raw_source_local_file_path(episode=episode, artifact=artifact, file_name=file)
    if path is None:
        raise HTTPException(status_code=404, detail="raw source file is unavailable")
    emit_audit_event(
        "episode.artifact.download",
        actor=actor_label,
        resource=f"episode_artifact:{artifact.id}",
        detail={
            "workspace_id": episode.workspace_id,
            "episode_id": episode.id,
            "artifact_type": "raw_source",
            "delivery": "same_origin_stream",
        },
    )
    media_type = "application/json" if file.endswith(".json") else "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=f"{episode.episode_uid}-{path.name}")


@router.get("/{episode_id}/preview/media")
def stream_episode_preview_media(
    episode_id: int,
    sig: str | None = Query(default=None, max_length=4096),
    db: Session = Depends(get_db),
    user: dict | None = Depends(get_optional_user),
):
    """Serve one already-authorized local preview with native Range support.

    Browser videos cannot attach the SPA's Bearer header.  The descriptor
    endpoint therefore creates a short-lived, Episode-bound token only when a
    local mirror is the development authority; UAT and production keep using
    direct short-lived OSS URLs.
    """
    signed_payload: dict[str, object] | None = None
    if sig:
        try:
            signed_payload = verify_signed_download_token(
                sig,
                resource_type="episode_preview",
                resource_id=episode_id,
            )
            actor_id = _actor_id({"sub": signed_payload.get("sub")})
        except ValueError as exc:
            raise HTTPException(
                status_code=401, detail="preview signature is invalid or expired"
            ) from exc
        if actor_id is None:
            raise HTTPException(status_code=401, detail="preview signature is invalid or expired")
        actor_label = str(actor_id)
    else:
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        require_permission(user, "episode:read")
        actor_id = _actor_id(user)
        actor_label = str(user.get("email") or "")

    episode = _authorized_episode_for_actor_or_404(db, episode_id=episode_id, actor_id=actor_id)
    if signed_payload is not None:
        max_uses = int(signed_payload.get("max_uses") or 1)
        if not consume_download_jti(str(signed_payload.get("jti") or ""), max_uses):
            raise HTTPException(status_code=403, detail="preview signature use limit reached")
    artifact = next(
        (
            item
            for item in episode.artifacts
            if item.artifact_type == "process_preview" and item.storage_role == "process"
        ),
        None,
    )
    media_type = "video/mp4"
    if artifact is not None and isinstance(artifact.metadata_json, dict):
        candidate = artifact.metadata_json.get("media_type")
        if isinstance(candidate, str) and candidate in {"video/mp4", "video/webm"}:
            media_type = candidate
    preview_path = (
        resolve_episode_preview_file(str(artifact.storage_uri or ""), media_type=media_type)
        if artifact
        else None
    )
    if preview_path is None:
        raise HTTPException(status_code=404, detail="episode preview is unavailable")
    emit_audit_event(
        "file.preview",
        actor=actor_label,
        resource=f"episode:{episode.id}",
        detail={"delivery": "same_origin_range", "kind": "preview"},
    )
    return FileResponse(
        preview_path,
        media_type=media_type,
        filename=f"{episode.episode_uid}{preview_path.suffix.lower()}",
        content_disposition_type="inline",
    )


@router.get("/{episode_id}/assets")
def get_episode_assets_endpoint(
    episode_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:read")
    episode = _authorized_episode_or_404(db, episode_id=episode_id, user=user)
    return success(episode_assets_projection(db, episode=episode, actor_id=_actor_id(user)))


def _authorized_episode_or_404(db: Session, *, episode_id: int, user: dict) -> Episode:
    return _authorized_episode_for_actor_or_404(db, episode_id=episode_id, actor_id=_actor_id(user))


def _authorized_episode_for_actor_or_404(
    db: Session, *, episode_id: int, actor_id: int | None
) -> Episode:
    episode = db.get(Episode, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="episode does not exist")
    try:
        require_episode_actor(db, actor_id=actor_id, episode=episode)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return episode


def _ai_job_response(job) -> dict[str, object]:
    return {
        "id": job.id,
        "status": job.status,
        "phase": job.phase,
        "progress_percent": max(0, min(100, int(job.progress_percent or 0))),
    }
