"""Safe, versioned Episode workbench state and workflow transitions.

This module owns the data contract shared by cut, annotation, and review
workbenches.  It deliberately stores only reviewed business payloads in the
database and never uses browser-provided storage locations or media paths.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import case, cast, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from data.database import (
    CollectionDevice,
    Episode,
    EpisodeAnnotation,
    EpisodeArtifact,
    EpisodeCollectorAttribution,
    EpisodeDeviceAttribution,
    EpisodeReview,
    JobRun,
    PersonnelProfile,
    TaskLabel,
    User,
    WorkItem,
)
from data.services.collector_profiles import (
    available_collector_profile,
    collector_profile_brief,
)
from data.services.derived_preview_batch import (
    ensure_derived_preview_batch_job,
    latest_derived_preview_batch_job,
    latest_preview_job_for_episode,
)
from data.services.episode_cut_suggestions import safe_cut_suggestion_items
from data.services.episode_media_access import (
    issue_episode_preview_fallback_url,
    issue_episode_preview_url,
)
from data.services.episode_multimodal import (
    EpisodeMultimodalError,
    ai_suggestion_capability,
    derived_context_projection,
)
from data.services.episode_multimodal import (
    episode_timeline_bounds as _quality_timeline_bounds,
)
from data.services.workspace_access import (
    require_episode_actor,
    require_workspace_actor,
)

logger = logging.getLogger(__name__)

QUALITY_READY_STATUSES = frozenset({"passed", "recovered", "profiled"})
EDITABLE_WORK_KINDS = frozenset({"cut", "annotation"})
REVIEW_TARGET_KINDS = frozenset({"cut", "annotation"})
_UNSET = object()
MAX_CUT_WINDOW_NS = 300 * 1_000_000_000
MAX_WORKBENCH_SEGMENTS = 1000
MAX_PREVIEW_TIMELINE_FRAMES = 100_000
CUT_ELIGIBILITY_VALUES = frozenset({"included", "excluded"})
CUT_EXCLUSION_REASONS = frozenset({"off_task", "idle_or_setup", "privacy_sensitive", "other"})


class WorkbenchError(ValueError):
    """A stable, user-safe workbench error code."""


class DraftConflict(WorkbenchError):
    def __init__(self) -> None:
        super().__init__("draft_conflict")


class CollectorAttributionImmutable(WorkbenchError):
    def __init__(self) -> None:
        super().__init__("collector_attribution_immutable")


class DeviceAttributionImmutable(WorkbenchError):
    def __init__(self) -> None:
        super().__init__("device_attribution_immutable")


@dataclass(frozen=True)
class ReviewResult:
    review_item: WorkItem
    submitted_item: WorkItem
    episode: Episode
    publication: JobRun | None
    derived_episodes: tuple[Episode, ...]
    preview_jobs: tuple[JobRun, ...]
    replayed: bool


def workbench_snapshot(
    db: Session,
    *,
    episode_id: int,
    work_item_id: int,
    actor_id: int | None,
) -> dict[str, object]:
    """Return a bounded, authorized workbench projection for one work item."""
    episode = _require_episode(db, episode_id)
    actor = require_episode_actor(db, actor_id=actor_id, episode=episode)
    item = db.get(WorkItem, work_item_id)
    if item is None or item.episode_id != episode.id:
        raise WorkbenchError("work_item_episode_mismatch")
    if item.assignee_user_id not in {actor.id, None} and actor.role != "admin":
        raise PermissionError("work item is assigned to another user")
    if item.status in {"accepted", "cancelled", "stale"} and actor.role != "admin":
        raise PermissionError("work item is no longer available")

    start_ns, end_ns = episode_timeline_bounds(episode)
    annotation = _latest_annotation(db, episode_id=episode.id)
    review = _latest_review(db, episode_id=episode.id)
    review_target = _review_target_projection(db, item=item, episode=episode)
    collector = effective_collector_attribution(db, episode=episode)
    device = effective_device_attribution(db, episode=episode)
    metrics = _metrics_projection(episode, start_ns=start_ns, end_ns=end_ns)
    return {
        "episode": {
            "id": episode.id,
            "episode_uid": episode.episode_uid,
            "kind": episode.kind,
            "workspace_id": episode.workspace_id,
            "task_set_id": episode.task_set_id,
            "batch_id": episode.batch_id,
            "task_label": _task_label_projection(db, task_label_id=episode.task_label_id),
            "parent_episode_id": episode.parent_episode_id,
            "collector_attribution": collector,
            "device_attribution": device,
            "source_start_ns": str(start_ns),
            "source_end_ns": str(end_ns),
            "quality": _quality_projection(episode),
            "metrics": metrics,
        },
        "work_item": _work_item_projection(item, actor=actor),
        "capabilities": {
            "cut": item.kind == "cut" and episode.kind == "source",
            "annotation": item.kind == "annotation" and episode.kind == "derived",
            "review": item.kind == "review",
            "ai_suggestions": _ai_suggestions_available(episode, metrics=metrics),
        },
        "media": {"preview": _preview_projection(db, episode, actor_id=actor.id)},
        "timeline": {
            "start_ns": str(start_ns),
            "end_ns": str(end_ns),
            "duration_s": _duration_seconds(start_ns, end_ns),
            "reference_topic": _safe_reference_topic(episode),
            "event_tracks": [],
            "boundary_suggestions": safe_cut_suggestion_items(
                projection=(
                    episode.metadata_json.get("cut_suggestions")
                    if isinstance(episode.metadata_json, dict)
                    else None
                ),
                start_ns=start_ns,
                end_ns=end_ns,
            ),
        },
        "draft": {
            "kind": item.kind,
            "version": int(item.draft_version or 0),
            "payload": dict(item.draft_json or {}),
        },
        "annotation": (
            {"version": annotation.version, "payload": dict(annotation.payload_json or {})}
            if annotation is not None
            else {"version": None, "payload": {}}
        ),
        "review": (
            {"decision": review.decision, "note": review.note}
            if review is not None
            else {"decision": None, "note": ""}
        ),
        "review_target": review_target,
        "publication": publication_projection(db, episode=episode, actor_id=actor.id),
    }


def _review_target_projection(
    db: Session,
    *,
    item: WorkItem,
    episode: Episode,
) -> dict[str, object] | None:
    """Expose only the exact submitted payload a review item is allowed to judge."""
    if item.kind != "review":
        return None
    if item.review_target_kind not in REVIEW_TARGET_KINDS or item.review_of_work_item_id is None:
        raise WorkbenchError("review_target_unavailable")
    submitted = db.get(WorkItem, item.review_of_work_item_id)
    if (
        submitted is None
        or submitted.episode_id != episode.id
        or submitted.kind != item.review_target_kind
        or submitted.status not in {"submitted", "accepted", "rejected"}
    ):
        raise WorkbenchError("review_submission_unavailable")
    return {
        "kind": item.review_target_kind,
        "work_item_id": submitted.id,
        "draft_version": int(submitted.draft_version or 0),
        "payload": dict(submitted.draft_json or {}),
    }


def save_draft(
    db: Session,
    *,
    work_item_id: int,
    actor_id: int | None,
    base_version: int,
    payload: object,
) -> WorkItem:
    """Validate and persist one editable draft with optimistic concurrency."""
    item, actor, episode = _locked_editable_work_item(
        db, work_item_id=work_item_id, actor_id=actor_id
    )
    if int(item.draft_version or 0) != base_version:
        raise DraftConflict()
    normalized = validate_draft_payload(db, item=item, episode=episode, payload=payload)
    item.draft_json = normalized
    item.draft_version = int(item.draft_version or 0) + 1
    _bump(item)
    db.flush()
    return item


def submit_draft(
    db: Session,
    *,
    work_item_id: int,
    actor_id: int | None,
    base_version: int,
) -> tuple[WorkItem, WorkItem]:
    """Freeze the current draft and create its exact review work item.

    The operation is intentionally idempotent for a retry carrying the same
    draft version.  A rejected item can be claimed, edited to a new version,
    and submitted again without changing old review evidence.
    """
    item, _actor, episode = _locked_editable_work_item(
        db,
        work_item_id=work_item_id,
        actor_id=actor_id,
        allowed_statuses=frozenset({"in_progress", "submitted"}),
    )
    if int(item.draft_version or 0) != base_version:
        raise DraftConflict()
    if not item.draft_json:
        raise WorkbenchError("draft_required")
    normalized = validate_draft_payload(
        db,
        item=item,
        episode=episode,
        payload=item.draft_json,
        for_submission=True,
    )
    item.draft_json = normalized

    reviews = (
        db.query(WorkItem)
        .filter(WorkItem.review_of_work_item_id == item.id)
        .order_by(WorkItem.generation.desc(), WorkItem.id.desc())
        .with_for_update()
        .all()
    )
    existing = next((review for review in reviews if review.generation == item.generation), None)
    if item.status == "submitted":
        if existing is None:
            raise WorkbenchError("review_work_item_missing")
        return item, existing
    if item.status != "in_progress":
        raise WorkbenchError("work_item_must_be_in_progress")

    latest = reviews[0] if reviews else None
    if latest is not None and latest.generation >= item.generation:
        if latest.status != "rejected":
            raise WorkbenchError("review_attempt_not_finalized")
        item.generation = int(latest.generation) + 1
        existing = None

    if item.kind == "annotation":
        _create_annotation_revision(db, item=item, episode=episode)
        episode.annotation_status = "submitted"
    item.status = "submitted"
    _bump(item)
    if existing is None:
        existing = WorkItem(
            workspace_id=episode.workspace_id,
            episode_id=episode.id,
            kind="review",
            review_target_kind=item.kind,
            review_of_work_item_id=item.id,
            status="pending",
            created_by_user_id=item.assignee_user_id,
            generation=item.generation,
            version=1,
            note=f"review for {item.kind} work item {item.id}",
        )
        db.add(existing)
        db.flush()
    return item, existing


def review_draft(
    db: Session,
    *,
    work_item_id: int,
    actor_id: int | None,
    decision: str,
    note: str = "",
    collector_profile_id: int | None | object = _UNSET,
    collection_device_id: int | None | object = _UNSET,
) -> ReviewResult:
    """Apply an authorized cut or annotation review atomically."""
    if decision not in {"accepted", "rejected"}:
        raise WorkbenchError("unsupported_review_decision")
    review_item = (
        db.query(WorkItem).filter(WorkItem.id == work_item_id).with_for_update().one_or_none()
    )
    if review_item is None or review_item.kind != "review":
        raise WorkbenchError("review_work_item_unavailable")
    if (
        review_item.review_target_kind not in REVIEW_TARGET_KINDS
        or review_item.review_of_work_item_id is None
    ):
        raise WorkbenchError("review_target_unavailable")
    actor = require_workspace_actor(db, actor_id=actor_id, workspace_id=review_item.workspace_id)
    if actor.role not in {"admin", "auditor"}:
        raise PermissionError("actor role cannot review this work item")
    if review_item.assignee_user_id != actor.id:
        raise PermissionError("work item is assigned to another user")

    submitted = (
        db.query(WorkItem)
        .filter(WorkItem.id == review_item.review_of_work_item_id)
        .with_for_update()
        .one_or_none()
    )
    if submitted is None or submitted.kind != review_item.review_target_kind:
        raise WorkbenchError("review_submission_unavailable")
    episode = _require_episode(db, review_item.episode_id)

    if review_item.status in {"accepted", "rejected"}:
        if review_item.status != decision:
            raise WorkbenchError("review_already_finalized")
        return _existing_review_result(
            db, review_item=review_item, submitted=submitted, episode=episode
        )
    if review_item.status != "in_progress" or submitted.status != "submitted":
        raise WorkbenchError("review_work_item_must_be_in_progress")

    review_item.status = decision
    review_item.note = _bounded_text(note, limit=2000)
    _bump(review_item)
    _bump(submitted)
    submitted.status = decision
    if decision == "rejected":
        submitted.assignee_user_id = None
    target = review_item.review_target_kind
    annotation_version: int | None = None
    derived_episodes: tuple[Episode, ...] = ()
    preview_jobs: tuple[JobRun, ...] = ()
    publication: JobRun | None = None

    if target == "cut":
        episode.review_status = decision
        if decision == "accepted":
            derived_episodes = tuple(
                create_derived_episodes_from_cut_draft(
                    db,
                    source=episode,
                    cut_item=submitted,
                    review_item=review_item,
                    actor_id=actor.id,
                )
            )
            preview_jobs = _ensure_derived_preview_jobs(
                db,
                source=episode,
                review_item_id=review_item.id,
                episodes=derived_episodes,
                actor_id=actor.id,
            )
    else:
        annotation = _annotation_for_submission(db, item=submitted)
        if annotation is None:
            raise WorkbenchError("submitted_annotation_unavailable")
        annotation_version = annotation.version
        episode.annotation_status = decision
        episode.review_status = decision
        if decision == "accepted":
            _apply_collector_attribution(
                db,
                episode=episode,
                actor=actor,
                payload=annotation.payload_json,
                override_collector_profile_id=collector_profile_id,
                annotation_version=annotation.version,
                note=review_item.note,
            )
            _apply_device_attribution(
                db,
                episode=episode,
                actor=actor,
                payload=annotation.payload_json,
                override_collection_device_id=collection_device_id,
                annotation_version=annotation.version,
                note=review_item.note,
            )
            publication = _ensure_publication_job(
                db,
                episode=episode,
                actor_id=actor.id,
                review_item_id=review_item.id,
            )

    db.add(
        EpisodeReview(
            episode_id=episode.id,
            annotation_version=annotation_version,
            review_target_kind=target,
            review_work_item_id=review_item.id,
            decision=decision,
            auditor_id=actor.id,
            note=review_item.note,
        )
    )
    db.flush()
    return ReviewResult(
        review_item=review_item,
        submitted_item=submitted,
        episode=episode,
        publication=publication,
        derived_episodes=derived_episodes,
        preview_jobs=preview_jobs,
        replayed=False,
    )


def validate_draft_payload(
    db: Session,
    *,
    item: WorkItem,
    episode: Episode,
    payload: object,
    for_submission: bool = False,
) -> dict[str, object]:
    if item.kind not in EDITABLE_WORK_KINDS:
        raise WorkbenchError("draft_kind_unsupported")
    if not isinstance(payload, dict):
        raise WorkbenchError("draft_payload_invalid")
    if item.kind == "cut":
        return _validate_cut_payload(
            episode=episode, payload=payload, for_submission=for_submission
        )
    return _validate_annotation_payload(
        db,
        episode=episode,
        payload=payload,
        for_submission=for_submission,
    )


def episode_timeline_bounds(episode: Episode) -> tuple[int, int]:
    """Use the same quality-validated bounds as multimodal API projections."""
    try:
        return _quality_timeline_bounds(episode)
    except EpisodeMultimodalError as exc:
        raise WorkbenchError(str(exc)) from exc


def effective_collector_attribution(db: Session, *, episode: Episode) -> dict[str, object]:
    root = root_source_episode(db, episode=episode)
    attribution = (
        db.query(EpisodeCollectorAttribution)
        .filter(EpisodeCollectorAttribution.root_source_episode_id == root.id)
        .order_by(
            _attribution_priority(EpisodeCollectorAttribution.source).desc(),
            EpisodeCollectorAttribution.id.desc(),
        )
        .first()
    )
    profile = attribution.collector_profile if attribution is not None else None
    source = attribution.source if attribution is not None else "unknown"
    return {
        "source_episode_id": root.id,
        "collector": _collector_projection(profile),
        "effective_source": source,
        "editable": source != "online_verified",
    }


def effective_device_attribution(db: Session, *, episode: Episode) -> dict[str, object]:
    root = root_source_episode(db, episode=episode)
    attribution = (
        db.query(EpisodeDeviceAttribution)
        .filter(EpisodeDeviceAttribution.root_source_episode_id == root.id)
        .order_by(
            _attribution_priority(EpisodeDeviceAttribution.source).desc(),
            EpisodeDeviceAttribution.id.desc(),
        )
        .first()
    )
    device = attribution.collection_device if attribution is not None else None
    source = attribution.source if attribution is not None else "unknown"
    return {
        "source_episode_id": root.id,
        "device": _device_projection(device),
        "effective_source": source,
        "editable": source != "online_verified",
    }


def _attribution_priority(source_column):
    return case(
        (source_column == "online_verified", 4),
        (source_column == "offline_curated", 3),
        (source_column == "machine_reported", 2),
        (source_column == "offline_declared", 1),
        else_=0,
    )


def root_source_episode(db: Session, *, episode: Episode) -> Episode:
    current = episode
    seen: set[int] = set()
    for _ in range(32):
        if current.id in seen:
            raise WorkbenchError("episode_lineage_invalid")
        seen.add(current.id)
        if current.kind == "source":
            return current
        if current.parent_episode_id is None:
            raise WorkbenchError("episode_lineage_invalid")
        parent = db.get(Episode, current.parent_episode_id)
        if parent is None:
            raise WorkbenchError("episode_lineage_invalid")
        current = parent
    raise WorkbenchError("episode_lineage_invalid")


def create_derived_episodes_from_cut_draft(
    db: Session,
    *,
    source: Episode,
    cut_item: WorkItem,
    review_item: WorkItem,
    actor_id: int,
) -> list[Episode]:
    if source.kind != "source" or cut_item.kind != "cut":
        raise WorkbenchError("cut_source_invalid")
    payload = validate_draft_payload(
        db,
        item=cut_item,
        episode=source,
        payload=cut_item.draft_json,
        for_submission=True,
    )
    segments = payload.get("segments")
    if not isinstance(segments, list) or not segments:
        raise WorkbenchError("cut_segments_required")
    included_segments = [
        segment for segment in segments if segment.get("eligibility") == "included"
    ]
    if not included_segments:
        source.workflow_status = "excluded"
        db.flush()
        return []

    existing = (
        db.query(Episode)
        .filter(
            Episode.parent_episode_id == source.id,
            cast(Episode.metadata_json, JSONB)["lineage"]["cut_review_work_item_id"].astext
            == str(review_item.id),
        )
        .order_by(Episode.source_start_ns, Episode.source_end_ns)
        .all()
    )
    if existing:
        return existing

    latest_version = (
        db.query(func.max(Episode.derivation_version))
        .filter(Episode.parent_episode_id == source.id)
        .scalar()
    )
    derivation_version = int(latest_version or 0) + 1
    children: list[Episode] = []
    for segment in included_segments:
        start_ns = int(str(segment["start_ns"]))
        end_ns = int(str(segment["end_ns"]))
        child = Episode(
            episode_uid=_derived_episode_uid(
                source=source,
                derivation_version=derivation_version,
                start_ns=start_ns,
                end_ns=end_ns,
            ),
            workspace_id=source.workspace_id,
            task_set_id=source.task_set_id,
            batch_id=source.batch_id,
            import_session_id=source.import_session_id,
            kind="derived",
            parent_episode_id=source.id,
            derivation_version=derivation_version,
            modality=source.modality,
            embodiment_id=source.embodiment_id,
            task_label_id=source.task_label_id,
            scene=source.scene,
            source_fingerprint=source.source_fingerprint,
            source_start_ns=start_ns,
            source_end_ns=end_ns,
            workflow_status="ready",
            quality_status=_inherited_quality_status(source),
            annotation_status="pending",
            review_status="pending",
            task_language=source.task_language,
            metadata_json={
                "lineage": {
                    "cut_work_item_id": cut_item.id,
                    "cut_review_work_item_id": review_item.id,
                },
                "metrics": {"duration_s": _duration_seconds(start_ns, end_ns)},
                **derived_context_projection(source),
            },
        )
        children.append(child)
    db.add_all(children)
    db.flush()
    db.add_all(
        [
            WorkItem(
                workspace_id=child.workspace_id,
                episode_id=child.id,
                kind="annotation",
                status="pending",
                created_by_user_id=actor_id,
                generation=1,
                version=1,
            )
            for child in children
        ]
    )
    return children


def _ensure_derived_preview_jobs(
    db: Session,
    *,
    source: Episode,
    review_item_id: int,
    episodes: tuple[Episode, ...],
    actor_id: int,
) -> tuple[JobRun, ...]:
    """Queue one media batch only when a cut produced previewable children."""
    if not any(
        episode.kind == "derived" and _safe_reference_topic(episode) for episode in episodes
    ):
        return ()
    return (
        ensure_derived_preview_batch_job(
            db,
            source=source,
            review_item_id=review_item_id,
            actor_id=actor_id,
        ),
    )


def publication_projection(
    db: Session, *, episode: Episode, actor_id: int | None
) -> dict[str, object]:
    require_episode_actor(db, actor_id=actor_id, episode=episode)
    job = (
        db.query(JobRun)
        .filter(
            JobRun.kind == "episode_publish",
            JobRun.resource_type == "episode",
            JobRun.resource_id == str(episode.id),
        )
        .order_by(JobRun.created_at.desc(), JobRun.id.desc())
        .first()
    )
    return {
        "status": job.status if job is not None else "waiting",
        "job_id": job.id if job is not None else None,
        "retry_allowed": bool(job is not None and job.status == "failed"),
    }


def _locked_editable_work_item(
    db: Session,
    *,
    work_item_id: int,
    actor_id: int | None,
    allowed_statuses: frozenset[str] = frozenset({"in_progress"}),
) -> tuple[WorkItem, User, Episode]:
    item = db.query(WorkItem).filter(WorkItem.id == work_item_id).with_for_update().one_or_none()
    if item is None or item.kind not in EDITABLE_WORK_KINDS or item.episode_id is None:
        raise WorkbenchError("editable_work_item_unavailable")
    actor = require_workspace_actor(db, actor_id=actor_id, workspace_id=item.workspace_id)
    if item.assignee_user_id != actor.id:
        raise PermissionError("work item is assigned to another user")
    if item.status not in allowed_statuses:
        raise WorkbenchError("work_item_must_be_in_progress")
    episode = _require_episode(db, item.episode_id)
    if item.kind == "cut" and episode.kind != "source":
        raise WorkbenchError("cut_requires_source_episode")
    if item.kind == "annotation" and episode.kind != "derived":
        raise WorkbenchError("annotation_requires_derived_episode")
    return item, actor, episode


def _validate_cut_payload(
    *, episode: Episode, payload: dict[str, object], for_submission: bool
) -> dict[str, object]:
    if set(payload) - {"mode", "segments", "note"}:
        raise WorkbenchError("draft_payload_invalid")
    mode = payload.get("mode")
    if mode not in {"whole", "partitioned"}:
        raise WorkbenchError("cut_mode_invalid")
    segments = _validate_segments(
        episode=episode,
        raw_segments=payload.get("segments"),
        description_required=False,
        cut_contract=True,
    )
    if not segments:
        raise WorkbenchError("cut_segments_required")
    start_limit, end_limit = episode_timeline_bounds(episode)
    if mode == "whole":
        if (
            len(segments) != 1
            or int(segments[0]["start_ns"]) != start_limit
            or int(segments[0]["end_ns"]) != end_limit
        ):
            raise WorkbenchError("cut_whole_range_required")
    else:
        if int(segments[0]["start_ns"]) != start_limit or int(segments[-1]["end_ns"]) != end_limit:
            raise WorkbenchError("cut_partition_must_cover_source")
        prior_end = start_limit
        for segment in segments:
            start = int(segment["start_ns"])
            end = int(segment["end_ns"])
            if start != prior_end:
                raise WorkbenchError("cut_partition_must_cover_source")
            if (
                for_submission
                and segment.get("eligibility") == "included"
                and end - start > MAX_CUT_WINDOW_NS
            ):
                raise WorkbenchError("cut_partition_window_too_long")
            prior_end = end
    if mode == "whole" and (
        for_submission
        and segments[0].get("eligibility") == "included"
        and int(segments[0]["end_ns"]) - int(segments[0]["start_ns"]) > MAX_CUT_WINDOW_NS
    ):
        raise WorkbenchError("cut_partition_window_too_long")
    result: dict[str, object] = {"mode": mode, "segments": segments}
    if "note" in payload:
        result["note"] = _bounded_text(payload["note"], limit=2000)
    return result


def _validate_annotation_payload(
    db: Session,
    *,
    episode: Episode,
    payload: dict[str, object],
    for_submission: bool,
) -> dict[str, object]:
    if set(payload) - {
        "segments",
        "outcome",
        "rating",
        "collector_profile_id",
        "collection_device_id",
        "note",
    }:
        raise WorkbenchError("draft_payload_invalid")
    segments = _validate_segments(
        episode=episode,
        raw_segments=payload.get("segments"),
        description_required=for_submission,
    )
    if for_submission and not segments:
        raise WorkbenchError("annotation_segments_required")
    outcome = payload.get("outcome", "unknown")
    if outcome not in {"success", "failure", "unknown"}:
        raise WorkbenchError("annotation_outcome_invalid")
    result: dict[str, object] = {"segments": segments, "outcome": outcome}
    if "note" in payload:
        result["note"] = _bounded_text(payload["note"], limit=2000)
    if "rating" in payload and payload["rating"] is not None:
        rating = payload["rating"]
        if type(rating) is not int or not 1 <= rating <= 5:
            raise WorkbenchError("annotation_rating_invalid")
        result["rating"] = rating
    if "collector_profile_id" in payload:
        collector_profile_id = payload["collector_profile_id"]
        _validate_collector_choice(db, episode=episode, collector_profile_id=collector_profile_id)
        result["collector_profile_id"] = collector_profile_id
    if "collection_device_id" in payload:
        collection_device_id = payload["collection_device_id"]
        _validate_device_choice(db, episode=episode, collection_device_id=collection_device_id)
        result["collection_device_id"] = collection_device_id
    return result


def _validate_segments(
    *,
    episode: Episode,
    raw_segments: object,
    description_required: bool,
    cut_contract: bool = False,
) -> list[dict[str, object]]:
    if not isinstance(raw_segments, list) or len(raw_segments) > MAX_WORKBENCH_SEGMENTS:
        raise WorkbenchError("draft_segments_invalid")
    start_limit, end_limit = episode_timeline_bounds(episode)
    normalized: list[dict[str, object]] = []
    prior_end: int | None = None
    seen_ids: set[str] = set()
    cut_fields = {"eligibility", "exclusion_reason", "boundary_after"} if cut_contract else set()
    for index, raw in enumerate(raw_segments):
        if not isinstance(raw, dict) or set(raw) - {
            "id",
            "start_ns",
            "end_ns",
            "description",
            "behavior_tag_ids",
            *cut_fields,
        }:
            raise WorkbenchError("draft_segments_invalid")
        segment_id = raw.get("id")
        if (
            not isinstance(segment_id, str)
            or not segment_id.strip()
            or len(segment_id) > 128
            or segment_id in seen_ids
        ):
            raise WorkbenchError("draft_segments_invalid")
        start = _parse_client_timestamp(raw.get("start_ns"))
        end = _parse_client_timestamp(raw.get("end_ns"))
        if (
            start < start_limit
            or end > end_limit
            or start >= end
            or (prior_end is not None and start < prior_end)
        ):
            raise WorkbenchError("draft_segment_out_of_range")
        description = raw.get("description", "")
        if not isinstance(description, str) or len(description.strip()) > 2000:
            raise WorkbenchError("draft_segments_invalid")
        if description_required and not description.strip():
            raise WorkbenchError("annotation_description_required")
        result: dict[str, object] = {
            "id": segment_id,
            "start_ns": str(start),
            "end_ns": str(end),
        }
        if description.strip():
            result["description"] = description.strip()
        if "behavior_tag_ids" in raw:
            tags = raw["behavior_tag_ids"]
            if (
                not isinstance(tags, list)
                or len(tags) > 100
                or any(type(tag) is not int or tag <= 0 for tag in tags)
            ):
                raise WorkbenchError("draft_segments_invalid")
            result["behavior_tag_ids"] = list(tags)
        if cut_contract:
            eligibility = raw.get("eligibility", "included")
            if eligibility not in CUT_ELIGIBILITY_VALUES:
                raise WorkbenchError("draft_segments_invalid")
            result["eligibility"] = eligibility
            exclusion_reason = raw.get("exclusion_reason", _UNSET)
            if eligibility == "included" and exclusion_reason is not _UNSET:
                raise WorkbenchError("draft_segments_invalid")
            if exclusion_reason is not _UNSET:
                if exclusion_reason not in CUT_EXCLUSION_REASONS:
                    raise WorkbenchError("draft_segments_invalid")
                result["exclusion_reason"] = exclusion_reason
            if index == len(raw_segments) - 1:
                if "boundary_after" in raw:
                    raise WorkbenchError("draft_segments_invalid")
            else:
                result["boundary_after"] = _validate_cut_boundary_after(
                    raw.get("boundary_after"),
                    episode_start_ns=start_limit,
                    episode_end_ns=end_limit,
                    actual_timestamp_ns=end,
                )
        normalized.append(result)
        seen_ids.add(segment_id)
        prior_end = end
    return normalized


def _validate_cut_boundary_after(
    raw: object,
    *,
    episode_start_ns: int,
    episode_end_ns: int,
    actual_timestamp_ns: int,
) -> dict[str, object]:
    if raw is None:
        return {"origin": "human"}
    if not isinstance(raw, dict):
        raise WorkbenchError("draft_segments_invalid")
    origin = raw.get("origin")
    if origin == "human":
        if set(raw) != {"origin"}:
            raise WorkbenchError("draft_segments_invalid")
        return {"origin": "human"}
    expected = {
        "origin",
        "suggested_timestamp_ns",
        "adjusted",
        "segment_id_hint",
        "segment_index",
        "protocol_version",
    }
    if origin != "qr_event" or set(raw) != expected:
        raise WorkbenchError("draft_segments_invalid")
    suggested = _parse_client_timestamp(raw.get("suggested_timestamp_ns"))
    segment_id_hint = raw.get("segment_id_hint")
    segment_index = raw.get("segment_index")
    protocol_version = raw.get("protocol_version")
    if not episode_start_ns < suggested < episode_end_ns:
        raise WorkbenchError("draft_segments_invalid")
    if (
        not isinstance(segment_id_hint, str)
        or not segment_id_hint.strip()
        or len(segment_id_hint.strip()) > 128
        or "\x00" in segment_id_hint
        or type(segment_index) is not int
        or not 0 <= segment_index <= 1_000_000
        or type(protocol_version) is not int
        or not 1 <= protocol_version <= 1_000_000
        or type(raw.get("adjusted")) is not bool
    ):
        raise WorkbenchError("draft_segments_invalid")
    return {
        "origin": "qr_event",
        "suggested_timestamp_ns": str(suggested),
        "adjusted": actual_timestamp_ns != suggested,
        "segment_id_hint": segment_id_hint.strip(),
        "segment_index": segment_index,
        "protocol_version": protocol_version,
    }


def _create_annotation_revision(
    db: Session, *, item: WorkItem, episode: Episode
) -> EpisodeAnnotation:
    existing = _annotation_for_submission(db, item=item)
    if existing is not None:
        return existing
    next_version = (
        int(
            db.query(func.coalesce(func.max(EpisodeAnnotation.version), 0))
            .filter(EpisodeAnnotation.episode_id == episode.id)
            .scalar()
            or 0
        )
        + 1
    )
    annotation = EpisodeAnnotation(
        episode_id=episode.id,
        work_item_id=item.id,
        draft_version=item.draft_version,
        version=next_version,
        created_by_user_id=item.assignee_user_id,
        payload_json=dict(item.draft_json or {}),
    )
    db.add(annotation)
    db.flush()
    return annotation


def _annotation_for_submission(db: Session, *, item: WorkItem) -> EpisodeAnnotation | None:
    return (
        db.query(EpisodeAnnotation)
        .filter(
            EpisodeAnnotation.work_item_id == item.id,
            EpisodeAnnotation.draft_version == item.draft_version,
        )
        .one_or_none()
    )


def _apply_collector_attribution(
    db: Session,
    *,
    episode: Episode,
    actor: User,
    payload: object,
    override_collector_profile_id: int | None | object,
    annotation_version: int,
    note: str,
) -> None:
    if override_collector_profile_id is _UNSET:
        if not isinstance(payload, dict) or "collector_profile_id" not in payload:
            return
        collector_profile_id = payload["collector_profile_id"]
    else:
        collector_profile_id = override_collector_profile_id
    _validate_collector_choice(db, episode=episode, collector_profile_id=collector_profile_id)
    root = root_source_episode(db, episode=episode)
    current = effective_collector_attribution(db, episode=root)
    if current["effective_source"] == "online_verified":
        raise CollectorAttributionImmutable()
    db.add(
        EpisodeCollectorAttribution(
            root_source_episode_id=root.id,
            collector_profile_id=collector_profile_id,
            source="offline_curated",
            created_by_user_id=actor.id,
            annotation_version=annotation_version,
            note=_bounded_text(note, limit=2000),
        )
    )
    root.updated_at = datetime.utcnow()


def _apply_device_attribution(
    db: Session,
    *,
    episode: Episode,
    actor: User,
    payload: object,
    override_collection_device_id: int | None | object,
    annotation_version: int,
    note: str,
) -> None:
    if override_collection_device_id is _UNSET:
        if not isinstance(payload, dict) or "collection_device_id" not in payload:
            return
        collection_device_id = payload["collection_device_id"]
    else:
        collection_device_id = override_collection_device_id
    _validate_device_choice(db, episode=episode, collection_device_id=collection_device_id)
    root = root_source_episode(db, episode=episode)
    current = effective_device_attribution(db, episode=root)
    if current["effective_source"] == "online_verified":
        raise DeviceAttributionImmutable()
    db.add(
        EpisodeDeviceAttribution(
            root_source_episode_id=root.id,
            collection_device_id=collection_device_id,
            source="offline_curated",
            created_by_user_id=actor.id,
            annotation_version=annotation_version,
            note=_bounded_text(note, limit=2000),
        )
    )
    root.updated_at = datetime.utcnow()


def _validate_collector_choice(
    db: Session, *, episode: Episode, collector_profile_id: object
) -> None:
    if (
        effective_collector_attribution(db, episode=episode)["effective_source"]
        == "online_verified"
    ):
        raise CollectorAttributionImmutable()
    if collector_profile_id is None:
        return
    if type(collector_profile_id) is not int or collector_profile_id <= 0:
        raise WorkbenchError("collector_profile_invalid")
    profile = available_collector_profile(
        db, workspace_id=episode.workspace_id, profile_id=collector_profile_id
    )
    if profile is None:
        raise WorkbenchError("collector_profile_invalid")


def _validate_device_choice(db: Session, *, episode: Episode, collection_device_id: object) -> None:
    if effective_device_attribution(db, episode=episode)["effective_source"] == "online_verified":
        raise DeviceAttributionImmutable()
    if collection_device_id is None:
        return
    if type(collection_device_id) is not int or collection_device_id <= 0:
        raise WorkbenchError("collection_device_invalid")
    device = db.get(CollectionDevice, collection_device_id)
    if device is None or not device.is_active or device.workspace_id != episode.workspace_id:
        raise WorkbenchError("collection_device_invalid")


def _ensure_publication_job(
    db: Session, *, episode: Episode, actor_id: int, review_item_id: int
) -> JobRun | None:
    """Publication is retired; accepted review remains an atomic review only."""
    return None
    key = f"episode-publication:{episode.id}:review:{review_item_id}"
    job = db.query(JobRun).filter(JobRun.idempotency_key == key).one_or_none()
    if job is None:
        job = JobRun(
            id=uuid4().hex,
            kind="episode_publish",
            resource_type="episode",
            resource_id=str(episode.id),
            workspace_id=episode.workspace_id,
            task_set_id=episode.task_set_id,
            idempotency_key=key,
            queue="publish",
            actor_id=actor_id,
            status="queued",
            phase="queued",
            detail_json={"episode_id": episode.id, "review_work_item_id": review_item_id},
        )
        db.add(job)
        db.flush()
    return job


def _existing_review_result(
    db: Session, *, review_item: WorkItem, submitted: WorkItem, episode: Episode
) -> ReviewResult:
    derived: tuple[Episode, ...] = ()
    preview_jobs: tuple[JobRun, ...] = ()
    publication: JobRun | None = None
    if review_item.review_target_kind == "cut" and review_item.status == "accepted":
        derived = tuple(
            db.query(Episode)
            .filter(
                Episode.parent_episode_id == episode.id,
                cast(Episode.metadata_json, JSONB)["lineage"]["cut_review_work_item_id"].astext
                == str(review_item.id),
            )
            .order_by(Episode.source_start_ns, Episode.source_end_ns)
            .all()
        )
        if derived:
            batch_job = latest_derived_preview_batch_job(
                db,
                source=episode,
                review_item_id=review_item.id,
            )
            if batch_job is not None:
                preview_jobs = (batch_job,)
            else:
                preview_jobs = tuple(
                    db.query(JobRun)
                    .filter(
                        JobRun.kind == "episode_preview",
                        JobRun.resource_type == "episode",
                        JobRun.resource_id.in_([str(item.id) for item in derived]),
                    )
                    .order_by(JobRun.created_at.asc(), JobRun.id.asc())
                    .all()
                )
    if review_item.review_target_kind == "annotation" and review_item.status == "accepted":
        publication = (
            db.query(JobRun)
            .filter(
                JobRun.kind == "episode_publish",
                JobRun.resource_type == "episode",
                JobRun.resource_id == str(episode.id),
                cast(JobRun.detail_json, JSONB)["review_work_item_id"].astext
                == str(review_item.id),
            )
            .one_or_none()
        )
    return ReviewResult(
        review_item=review_item,
        submitted_item=submitted,
        episode=episode,
        publication=publication,
        derived_episodes=derived,
        preview_jobs=preview_jobs,
        replayed=True,
    )


def _require_episode(db: Session, episode_id: int | None) -> Episode:
    episode = db.get(Episode, episode_id)
    if episode is None:
        raise WorkbenchError("episode_unavailable")
    return episode


def _latest_annotation(db: Session, *, episode_id: int) -> EpisodeAnnotation | None:
    return (
        db.query(EpisodeAnnotation)
        .filter(EpisodeAnnotation.episode_id == episode_id)
        .order_by(EpisodeAnnotation.version.desc())
        .first()
    )


def _latest_review(db: Session, *, episode_id: int) -> EpisodeReview | None:
    return (
        db.query(EpisodeReview)
        .filter(EpisodeReview.episode_id == episode_id)
        .order_by(EpisodeReview.created_at.desc(), EpisodeReview.id.desc())
        .first()
    )


def _task_label_projection(db: Session, *, task_label_id: int | None) -> dict[str, object] | None:
    if task_label_id is None:
        return None
    label = db.get(TaskLabel, task_label_id)
    if label is None:
        return None
    return {"id": label.id, "key": label.key, "name": label.name}


def _collector_projection(profile: PersonnelProfile | None) -> dict[str, object] | None:
    if profile is None:
        return None
    return collector_profile_brief(profile)


def _device_projection(device: CollectionDevice | None) -> dict[str, object] | None:
    if device is None:
        return None
    return {
        "id": device.id,
        "name": device.name,
        "device_type": device.device_type,
        "model": device.model,
        "serial_number": device.serial_number,
    }


def _quality_projection(episode: Episode) -> dict[str, object]:
    status = "passed" if episode.quality_status == "profiled" else episode.quality_status
    return {"status": status, "restored": status == "recovered"}


def _metrics_projection(
    episode: Episode, *, start_ns: int, end_ns: int
) -> dict[str, int | float | None]:
    if episode.quality_status not in QUALITY_READY_STATUSES:
        return {"reference_frame_count": None, "duration_s": None, "average_rgb_rate_hz": None}
    raw = (
        episode.metadata_json.get("metrics", {}) if isinstance(episode.metadata_json, dict) else {}
    )
    reference_frames = _positive_int(raw.get("reference_frame_count"))
    rate = _positive_float(raw.get("average_rgb_rate_hz"))
    duration = _positive_float(raw.get("duration_s")) or _duration_seconds(start_ns, end_ns)
    return {
        "reference_frame_count": reference_frames,
        "duration_s": duration,
        "average_rgb_rate_hz": rate,
    }


def _work_item_projection(item: WorkItem, *, actor: User) -> dict[str, object]:
    return {
        "id": item.id,
        "kind": item.kind,
        "status": item.status,
        "assignee_user_id": item.assignee_user_id,
        "review_target_kind": item.review_target_kind,
        "available_actions": work_item_available_actions(item, actor=actor),
    }


def work_item_available_actions(item: WorkItem, *, actor: User) -> list[str]:
    allowed_roles = {
        "cut": {"admin", "annotator"},
        "annotation": {"admin", "annotator"},
        "review": {"admin", "auditor"},
    }
    if actor.role not in allowed_roles.get(item.kind, set()):
        return []
    if item.status == "pending":
        return ["claim"]
    if item.status == "rejected":
        return ["claim"] if item.kind in EDITABLE_WORK_KINDS else []
    if item.assignee_user_id != actor.id:
        if item.status in {"assigned", "in_progress"} and actor.role == "admin":
            return ["release"]
        return []
    if item.status == "assigned":
        return ["continue", "release"]
    if item.status == "in_progress":
        if item.kind == "review":
            return ["review"]
        return ["save_draft", "submit", "release"]
    return []


def _preview_projection(db: Session, episode: Episode, *, actor_id: int) -> dict[str, object]:
    """Return a non-blocking preview descriptor for the workbench first paint.

    Signing failures and missing artifacts degrade to ``available=false`` with an
    explicit status.  This never scans MCAP or generates preview media inline.
    """
    artifact = next(
        (item for item in episode.artifacts if item.artifact_type == "process_preview"), None
    )
    media_type = "video/mp4"
    if artifact is not None and isinstance(artifact.metadata_json, dict):
        candidate = artifact.metadata_json.get("media_type")
        if candidate in {"video/mp4", "video/webm"}:
            media_type = candidate
    playback_timeline = _preview_playback_timeline(episode, artifact=artifact)
    unavailable = {
        "available": False,
        "direct": False,
        "url": "",
        "expires_at": "",
        "media_type": media_type,
        "playback_timeline": playback_timeline,
    }
    if artifact is None:
        status = _preview_job_status(db, episode=episode)
        return {**unavailable, "playback_timeline": None, "status": status}
    uri = str(artifact.storage_uri or "")
    access = None
    try:
        access = (
            issue_episode_preview_url(
                uri,
                download_name=f"{episode.episode_uid}.mp4",
                media_type=media_type,
            )
            if uri.startswith("oss://")
            else None
        )
        if access is None:
            access = issue_episode_preview_fallback_url(
                uri,
                episode_id=episode.id,
                actor_id=actor_id,
                media_type=media_type,
            )
    except Exception:
        # Defensive: signing helpers already swallow expected failures; never
        # let an unexpected error block the whole workbench snapshot.
        logger.exception("workbench preview signing failed episode_id=%s", episode.id)
        access = None
    if access is None:
        return {**unavailable, "status": "unavailable"}
    return {
        "available": True,
        **access.as_payload(),
        "playback_timeline": playback_timeline,
        "status": "ready",
    }


def _preview_job_status(db: Session, *, episode: Episode) -> str:
    job = latest_preview_job_for_episode(db, episode=episode)
    if job is None:
        return "unavailable"
    if job.status in {"queued", "running", "retry_pending"}:
        return "processing"
    if job.status == "failed":
        return "failed"
    return "unavailable"


def _preview_playback_timeline(
    episode: Episode, *, artifact: EpisodeArtifact | None
) -> dict[str, object] | None:
    if artifact is None or not isinstance(artifact.metadata_json, dict):
        return None
    encoded_fps = artifact.metadata_json.get("encoded_fps")
    raw_timestamps = artifact.metadata_json.get("frame_timestamps_ns")
    if (
        not isinstance(encoded_fps, (int, float))
        or isinstance(encoded_fps, bool)
        or not math.isfinite(float(encoded_fps))
        or not 0 < float(encoded_fps) <= 240
        or not isinstance(raw_timestamps, list)
        or not 1 <= len(raw_timestamps) <= MAX_PREVIEW_TIMELINE_FRAMES
    ):
        return None
    start_ns, end_ns = episode_timeline_bounds(episode)
    timestamps: list[str] = []
    prior: int | None = None
    for raw in raw_timestamps:
        try:
            value = _parse_client_timestamp(raw)
        except WorkbenchError:
            return None
        if value < start_ns or value >= end_ns or (prior is not None and value <= prior):
            return None
        timestamps.append(str(value))
        prior = value
    return {"encoded_fps": float(encoded_fps), "frame_timestamps_ns": timestamps}


def _safe_reference_topic(episode: Episode) -> str | None:
    metadata = episode.metadata_json if isinstance(episode.metadata_json, dict) else {}
    timeline = metadata.get("timeline") if isinstance(metadata.get("timeline"), dict) else {}
    value = timeline.get("reference_topic", metadata.get("reference_topic"))
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        return None
    return value.strip()


def _ai_suggestions_available(episode: Episode, *, metrics: dict[str, int | float | None]) -> bool:
    del metrics
    return bool(ai_suggestion_capability(episode).get("eligible"))


def _inherited_quality_status(source: Episode) -> str:
    return source.quality_status if source.quality_status in QUALITY_READY_STATUSES else "pending"


def _derived_episode_uid(
    *, source: Episode, derivation_version: int, start_ns: int, end_ns: int
) -> str:
    value = f"drv_{source.id}_{derivation_version}_{start_ns}_{end_ns}"
    if len(value) <= 64:
        return value
    return f"drv_{source.id}_{derivation_version}_{uuid4().hex[:24]}"


def _parse_client_timestamp(value: object) -> int:
    if not isinstance(value, str) or not value.isdigit() or len(value) > 20:
        raise WorkbenchError("draft_timestamp_invalid")
    parsed = int(value)
    if parsed < 0:
        raise WorkbenchError("draft_timestamp_invalid")
    return parsed


def _bounded_text(value: object, *, limit: int) -> str:
    if not isinstance(value, str):
        raise WorkbenchError("draft_payload_invalid")
    normalized = value.strip()
    if len(normalized) > limit:
        raise WorkbenchError("draft_payload_invalid")
    return normalized


def _duration_seconds(start_ns: int, end_ns: int) -> float:
    return round((end_ns - start_ns) / 1_000_000_000, 6)


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _bump(item: WorkItem) -> None:
    item.version = int(item.version or 0) + 1
    item.updated_at = datetime.utcnow()
