"""Bounded, path-free multimodal projections for the Episode workbench.

The browser never receives a raw MCAP path, a storage URI, or arbitrary JSON
from source metadata.  Quality workers persist a small normalized projection;
this module validates it again before it becomes an API response.
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy.orm import Session, object_session

from data.database import Episode, EpisodeArtifact, JobRun, WorkItem

MAX_STREAMS = 32
MAX_EVENT_TRACKS = 32
MAX_EVENTS_PER_TRACK = 500
MAX_TIMESERIES_POINTS = 2_000
MAX_AI_PREVIEW_TIMELINE_FRAMES = 20_000
_QUALITY_READY = frozenset({"passed", "recovered", "profiled"})
_STREAM_KINDS = frozenset({"camera", "eef", "gripper", "pose", "lowdim"})
_EVENT_KINDS = frozenset({"annotation", "suggestion", "marker"})
EPISODE_AI_SUGGESTION_JOB_KIND = "episode_ai_suggestion"


class EpisodeMultimodalError(ValueError):
    """A stable, user-safe error for malformed or unavailable projections."""


class EpisodeAiSuggestionError(EpisodeMultimodalError):
    """A stable public error for generic Episode-scoped AI suggestions."""


def episode_timeline_bounds(episode: Episode) -> tuple[int, int]:
    """Return only quality-validated source bounds as integer nanoseconds."""
    if episode.kind == "derived":
        start, end = episode.source_start_ns, episode.source_end_ns
    else:
        metadata = _metadata(episode)
        timing = metadata.get("timing")
        if not isinstance(timing, dict):
            raise EpisodeMultimodalError("episode_timeline_unavailable")
        start = timing.get("start_timestamp_ns")
        end = timing.get("end_timestamp_ns")
    start_ns = _timestamp(start)
    end_ns = _timestamp(end)
    if start_ns is None or end_ns is None or start_ns >= end_ns:
        raise EpisodeMultimodalError("episode_timeline_unavailable")
    return start_ns, end_ns


def timeline_projection(episode: Episode) -> dict[str, object]:
    start_ns, end_ns = episode_timeline_bounds(episode)
    metadata = _metadata(episode)
    reference_topic = _safe_identifier(metadata.get("reference_topic"))
    timeline = metadata.get("timeline")
    raw_tracks = timeline.get("event_tracks") if isinstance(timeline, dict) else None
    return {
        "start_ns": str(start_ns),
        "end_ns": str(end_ns),
        "duration_s": round((end_ns - start_ns) / 1_000_000_000, 6),
        "reference_topic": reference_topic,
        "event_tracks": _event_tracks(raw_tracks, start_ns=start_ns, end_ns=end_ns),
    }


def multimodal_session_projection(episode: Episode) -> dict[str, object]:
    metadata = _metadata(episode)
    multimodal = metadata.get("multimodal")
    if not isinstance(multimodal, dict):
        multimodal = {}
    streams = _streams(multimodal.get("streams"))
    series = _timeseries(multimodal.get("timeseries"))
    return {
        "available": bool(streams or series),
        "streams": streams,
        "timeseries": series,
    }


def derived_context_projection(source: Episode) -> dict[str, object]:
    """Copy only normalized camera and sensor context into a derived asset."""
    session = multimodal_session_projection(source)
    reference_topic = _safe_identifier(_metadata(source).get("reference_topic"))
    result: dict[str, object] = {
        "multimodal": {
            "streams": session["streams"],
            "timeseries": session["timeseries"],
        }
    }
    if reference_topic is not None:
        result["reference_topic"] = reference_topic
    return result


def timeseries_projection(episode: Episode, *, series_id: str, limit: int) -> dict[str, object]:
    if not 1 <= limit <= MAX_TIMESERIES_POINTS:
        raise EpisodeMultimodalError("timeseries_limit_invalid")
    safe_series_id = _safe_identifier(series_id)
    if safe_series_id is None:
        raise EpisodeMultimodalError("timeseries_unavailable")
    session = multimodal_session_projection(episode)
    allowed = {row["id"] for row in session["timeseries"] if isinstance(row, dict)}
    if safe_series_id not in allowed:
        raise EpisodeMultimodalError("timeseries_unavailable")
    # Numeric series are optional process artifacts.  Absence is explicit; the
    # client must not fetch or derive values from the raw source as a fallback.
    return {"available": False, "series_id": safe_series_id, "points": []}


def frame_projection(episode: Episode, *, stream_id: str, timestamp_ns: str) -> dict[str, object]:
    safe_stream_id = _safe_identifier(stream_id)
    requested_timestamp = _timestamp(timestamp_ns)
    if safe_stream_id is None or requested_timestamp is None:
        raise EpisodeMultimodalError("frame_request_invalid")
    start_ns, end_ns = episode_timeline_bounds(episode)
    if requested_timestamp < start_ns or requested_timestamp >= end_ns:
        raise EpisodeMultimodalError("frame_request_out_of_range")
    session = multimodal_session_projection(episode)
    allowed = {
        row["id"]
        for row in session["streams"]
        if isinstance(row, dict) and row.get("kind") == "camera"
    }
    if safe_stream_id not in allowed:
        raise EpisodeMultimodalError("frame_stream_unavailable")
    return {
        "available": False,
        "stream_id": safe_stream_id,
        "timestamp_ns": str(requested_timestamp),
    }


def ai_suggestion_capability(
    episode: Episode,
    *,
    worker_available: bool | None = None,
) -> dict[str, object]:
    """Return a safe UI gate; it intentionally does not disclose provider config."""
    duration = _duration_seconds(episode)
    session = multimodal_session_projection(episode)
    rgb_topics = [
        row["id"]
        for row in session["streams"]
        if isinstance(row, dict) and row.get("kind") == "camera"
    ]
    reference_topic = _safe_identifier(_metadata(episode).get("reference_topic"))
    if reference_topic and reference_topic not in rgb_topics:
        rgb_topics.append(reference_topic)
    preview_exists = any(
        artifact.artifact_type == "process_preview" and artifact.storage_role == "process"
        for artifact in episode.artifacts
    )
    enabled = bool(_behavior_ai_runtime(episode).can_call_provider)
    if not enabled:
        disabled_reason = "ai_disabled"
    elif worker_available is False:
        disabled_reason = "ai_worker_unavailable"
    elif episode.kind != "derived" or episode.quality_status not in _QUALITY_READY:
        disabled_reason = "ai_target_not_eligible"
    elif duration is None or duration > 300.0:
        disabled_reason = "ai_target_too_long"
    elif not preview_exists or not rgb_topics:
        disabled_reason = "ai_preview_unavailable"
    else:
        disabled_reason = ""
    return {
        "enabled": enabled,
        "eligible": not bool(disabled_reason),
        "disabled_reason": disabled_reason,
        "rgb_topics": rgb_topics,
        "default_rgb_topic": rgb_topics[0] if rgb_topics else "",
    }


def ai_suggestions_projection(
    db: Session,
    *,
    episode: Episode,
    worker_available: bool | None = None,
) -> dict[str, object]:
    """List only UI-safe suggestion summaries for one authorized Episode."""
    jobs = (
        db.query(JobRun)
        .filter(
            JobRun.kind == EPISODE_AI_SUGGESTION_JOB_KIND,
            JobRun.resource_type == "episode",
            JobRun.resource_id == str(episode.id),
        )
        .order_by(JobRun.created_at.desc(), JobRun.id.desc())
        .limit(50)
        .all()
    )
    return {
        "capability": ai_suggestion_capability(episode, worker_available=worker_available),
        "items": [_ai_job_projection(job) for job in jobs],
    }


def create_episode_ai_suggestion(
    db: Session,
    *,
    episode: Episode,
    actor_id: int,
    rgb_topic: str,
) -> tuple[JobRun, bool]:
    """Create one idempotent, annotation-scoped AI suggestion JobRun.

    The exact annotation work item and current draft version form the request
    identity.  Repeated clicks therefore reuse the same durable job rather
    than enqueueing duplicate provider calls.
    """
    capability = ai_suggestion_capability(episode)
    if not capability["eligible"]:
        raise EpisodeAiSuggestionError(
            str(capability["disabled_reason"] or "ai_target_not_eligible")
        )
    topic = _safe_identifier(rgb_topic)
    allowed_topics = capability["rgb_topics"]
    if topic is None or topic not in allowed_topics:
        raise EpisodeAiSuggestionError("ai_rgb_topic_unavailable")
    annotation_item = _locked_annotation_item(db, episode=episode, actor_id=actor_id)
    preview = _preview_artifact(episode)
    if preview is None:
        raise EpisodeAiSuggestionError("ai_preview_unavailable")
    preview_fingerprint = _preview_fingerprint(preview)
    topic_digest = hashlib.sha256(topic.encode("utf-8")).hexdigest()[:16]
    key = (
        f"episode-ai:{episode.id}:item:{annotation_item.id}:draft:{int(annotation_item.draft_version or 0)}:"
        f"preview:{preview_fingerprint.removeprefix('sha256:')[:16]}:topic:{topic_digest}"
    )
    existing = (
        db.query(JobRun).filter(JobRun.idempotency_key == key).with_for_update().one_or_none()
    )
    if existing is not None:
        return existing, False
    job = JobRun(
        id=hashlib.sha256(f"{key}:job".encode()).hexdigest()[:32],
        kind=EPISODE_AI_SUGGESTION_JOB_KIND,
        resource_type="episode",
        resource_id=str(episode.id),
        workspace_id=episode.workspace_id,
        task_set_id=episode.task_set_id,
        idempotency_key=key,
        queue="ai",
        actor_id=actor_id,
        status="queued",
        phase="queued",
        detail_json={
            "episode_id": episode.id,
            "work_item_id": annotation_item.id,
            "draft_version": int(annotation_item.draft_version or 0),
            "rgb_topic": topic,
            "preview_fingerprint": preview_fingerprint,
        },
    )
    db.add(job)
    db.flush()
    return job, True


def retry_episode_ai_suggestion(
    db: Session,
    *,
    episode: Episode,
    job_id: str,
    actor_id: int,
) -> tuple[JobRun, bool]:
    """Create a fenced retry while retaining the terminal JobRun as evidence."""
    source = (
        db.query(JobRun)
        .filter(
            JobRun.id == job_id,
            JobRun.kind == EPISODE_AI_SUGGESTION_JOB_KIND,
            JobRun.resource_type == "episode",
            JobRun.resource_id == str(episode.id),
        )
        .with_for_update()
        .one_or_none()
    )
    if source is None:
        raise EpisodeAiSuggestionError("ai_suggestion_unavailable")
    if source.status not in {"failed", "cancelled"}:
        raise EpisodeAiSuggestionError("ai_suggestion_not_retryable")
    annotation_item = _locked_annotation_item(db, episode=episode, actor_id=actor_id)
    detail = dict(source.detail_json or {}) if isinstance(source.detail_json, dict) else {}
    topic = _safe_identifier(detail.get("rgb_topic"))
    capability = ai_suggestion_capability(episode)
    if not capability["eligible"] or topic is None or topic not in capability["rgb_topics"]:
        raise EpisodeAiSuggestionError(
            str(capability["disabled_reason"] or "ai_target_not_eligible")
        )
    root_key = str(detail.get("retry_root_key") or source.idempotency_key)
    prefix = f"{root_key}:retry:"
    retries = (
        db.query(JobRun)
        .filter(JobRun.idempotency_key.startswith(prefix))
        .order_by(JobRun.created_at.desc(), JobRun.id.desc())
        .with_for_update()
        .all()
    )
    if retries and retries[0].status not in {"failed", "cancelled", "succeeded"}:
        return retries[0], False
    attempt = len(retries) + 1
    retry_key = f"{prefix}{attempt}"
    job = JobRun(
        id=hashlib.sha256(f"{retry_key}:job".encode()).hexdigest()[:32],
        kind=EPISODE_AI_SUGGESTION_JOB_KIND,
        resource_type="episode",
        resource_id=str(episode.id),
        workspace_id=episode.workspace_id,
        task_set_id=episode.task_set_id,
        idempotency_key=retry_key,
        queue="ai",
        actor_id=actor_id,
        status="queued",
        phase="queued",
        detail_json={
            "episode_id": episode.id,
            "work_item_id": annotation_item.id,
            "draft_version": int(annotation_item.draft_version or 0),
            "rgb_topic": topic,
            "preview_fingerprint": _preview_fingerprint(_preview_artifact_or_raise(episode)),
            "retry_root_key": root_key,
            "retry_of_job_id": source.id,
            "retry_attempt": attempt,
        },
    )
    db.add(job)
    db.flush()
    return job, True


def run_episode_ai_suggestion(db: Session, job: JobRun) -> dict[str, object]:
    """Run one provider call against a worker-local process preview.

    The input URL/path is never stored in the JobRun or returned from this
    function.  The worker converts provider frame coordinates into bounded
    Episode nanosecond intervals before persisting the result.
    """
    if job.kind != EPISODE_AI_SUGGESTION_JOB_KIND or job.resource_type != "episode":
        raise EpisodeAiSuggestionError("ai_suggestion_unavailable")
    try:
        episode_id = int(job.resource_id)
    except (TypeError, ValueError) as exc:
        raise EpisodeAiSuggestionError("ai_suggestion_unavailable") from exc
    episode = db.query(Episode).filter(Episode.id == episode_id).with_for_update().one_or_none()
    if episode is None:
        raise EpisodeAiSuggestionError("ai_suggestion_unavailable")
    detail = dict(job.detail_json or {}) if isinstance(job.detail_json, dict) else {}
    topic = _safe_identifier(detail.get("rgb_topic"))
    capability = ai_suggestion_capability(episode)
    if not capability["eligible"] or topic is None or topic not in capability["rgb_topics"]:
        raise EpisodeAiSuggestionError(
            str(capability["disabled_reason"] or "ai_target_not_eligible")
        )
    preview = _preview_artifact_or_raise(episode)
    if detail.get("preview_fingerprint") != _preview_fingerprint(preview):
        raise EpisodeAiSuggestionError("ai_preview_fingerprint_changed")
    expected_work_item_id = detail.get("work_item_id")
    if type(expected_work_item_id) is not int or expected_work_item_id <= 0:
        raise EpisodeAiSuggestionError("ai_suggestion_unavailable")
    item = db.get(WorkItem, expected_work_item_id)
    if item is None or item.episode_id != episode.id or item.kind != "annotation":
        raise EpisodeAiSuggestionError("ai_suggestion_unavailable")

    from data.integrations.embodied_vl.behavior_suggestion import (
        BehaviorAiProviderError,
        EmbodiedVlBehaviorSuggestionProvider,
    )
    from data.services.behavior_ai_delivery import (
        BehaviorAiDeliveryError,
        issue_behavior_ai_input_url,
    )
    from data.services.platform_settings import load_behavior_ai_runtime

    try:
        runtime = load_behavior_ai_runtime(db)
        preview_url = issue_behavior_ai_input_url(
            storage_uri=preview.storage_uri,
            episode_id=episode.id,
            preview_fingerprint=_preview_fingerprint(preview),
            runtime=runtime,
        )
        suggestion = EmbodiedVlBehaviorSuggestionProvider(config=runtime).suggest(
            preview_url,
            correlation_id=job.id,
        )
    except EpisodeAiSuggestionError:
        raise
    except BehaviorAiDeliveryError as exc:
        raise EpisodeAiSuggestionError(str(exc)) from None
    except BehaviorAiProviderError as exc:
        raise EpisodeAiSuggestionError(exc.code) from None
    except Exception:
        raise EpisodeAiSuggestionError("ai_provider_unavailable") from None

    return {
        "schema": "quicdata.episode-ai-suggestion.v1",
        "rgb_topic": topic,
        "input_fingerprint": _preview_fingerprint(preview),
        "segments": _provider_segments_to_episode(
            episode, job=job, preview=preview, segments=suggestion.segments
        ),
    }


def _ai_job_projection(job: JobRun) -> dict[str, object]:
    result = job.result_json if isinstance(job.result_json, dict) else {}
    segments = result.get("segments") if isinstance(result.get("segments"), list) else []
    return {
        "id": job.id,
        "status": job.status,
        "phase": job.phase,
        "progress_percent": max(0, min(100, int(job.progress_percent or 0))),
        "error_code": str(job.error_code or "")[:64],
        "segments": _ai_segments(segments),
    }


def _ai_segments(value: list[object]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for raw in value[:500]:
        if not isinstance(raw, dict):
            continue
        segment_id = _safe_identifier(raw.get("id"))
        start_ns = _timestamp(raw.get("start_ns"))
        end_ns = _timestamp(raw.get("end_ns"))
        description = raw.get("description")
        if (
            segment_id is None
            or start_ns is None
            or end_ns is None
            or start_ns >= end_ns
            or not isinstance(description, str)
            or not description.strip()
            or len(description.strip()) > 2_000
        ):
            continue
        item: dict[str, object] = {
            "id": segment_id,
            "start_ns": str(start_ns),
            "end_ns": str(end_ns),
            "description": description.strip(),
        }
        confidence = _bounded_positive_float(raw.get("confidence"), maximum=1.0)
        if confidence is not None:
            item["confidence"] = confidence
        result.append(item)
    return result


def _locked_annotation_item(db: Session, *, episode: Episode, actor_id: int) -> WorkItem:
    item = (
        db.query(WorkItem)
        .filter(
            WorkItem.episode_id == episode.id,
            WorkItem.kind == "annotation",
            WorkItem.assignee_user_id == actor_id,
            WorkItem.status == "in_progress",
        )
        .with_for_update()
        .order_by(WorkItem.generation.desc(), WorkItem.id.desc())
        .first()
    )
    if item is None:
        raise EpisodeAiSuggestionError("ai_target_not_claimed")
    return item


def _preview_artifact(episode: Episode) -> EpisodeArtifact | None:
    for artifact in episode.artifacts:
        if artifact.artifact_type == "process_preview" and artifact.storage_role == "process":
            return artifact
    return None


def _preview_artifact_or_raise(episode: Episode) -> EpisodeArtifact:
    artifact = _preview_artifact(episode)
    if artifact is None:
        raise EpisodeAiSuggestionError("ai_preview_unavailable")
    return artifact


def _preview_fingerprint(artifact: EpisodeArtifact) -> str:
    checksum = str(artifact.checksum_sha256 or "").strip().lower()
    if len(checksum) == 64 and all(char in "0123456789abcdef" for char in checksum):
        return f"sha256:{checksum}"
    payload = f"artifact:{artifact.id}:size:{int(artifact.size_bytes or 0)}:manifest:{artifact.manifest_hash or ''}"
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _behavior_ai_runtime(episode: Episode):
    from data.services.platform_settings import load_behavior_ai_runtime

    db = object_session(episode)
    if db is None:
        raise EpisodeAiSuggestionError("ai_disabled")
    return load_behavior_ai_runtime(db)


def _provider_segments_to_episode(
    episode: Episode,
    *,
    job: JobRun,
    preview: EpisodeArtifact,
    segments: Any,
) -> list[dict[str, object]]:
    try:
        start_ns, end_ns = episode_timeline_bounds(episode)
    except EpisodeMultimodalError as exc:
        raise EpisodeAiSuggestionError(str(exc)) from exc
    timestamps = _preview_frame_timestamps(preview, start_ns=start_ns, end_ns=end_ns)
    if timestamps is not None:
        frame_count = len(timestamps)
    else:
        metrics = _metadata(episode).get("metrics")
        frame_count = metrics.get("reference_frame_count") if isinstance(metrics, dict) else None
        if isinstance(frame_count, bool):
            frame_count = None
        try:
            frame_count = int(frame_count)
        except (TypeError, ValueError):
            frame_count = 0
    if frame_count <= 0:
        raise EpisodeAiSuggestionError("ai_preview_timeline_unavailable")
    result: list[dict[str, object]] = []
    for index, segment in enumerate(list(segments)[:500]):
        start_frame = getattr(segment, "start_frame", None)
        end_frame = getattr(segment, "end_frame", None)
        description = getattr(segment, "description", None)
        confidence = getattr(segment, "confidence", None)
        if (
            isinstance(start_frame, bool)
            or not isinstance(start_frame, int)
            or isinstance(end_frame, bool)
            or not isinstance(end_frame, int)
            or not isinstance(description, str)
            or not description.strip()
        ):
            continue
        start_frame = max(0, min(frame_count - 1, start_frame))
        end_frame = max(start_frame + 1, min(frame_count, end_frame))
        if timestamps is None:
            start = start_ns + ((end_ns - start_ns) * start_frame // frame_count)
            end = start_ns + ((end_ns - start_ns) * end_frame // frame_count)
        else:
            start = timestamps[start_frame]
            end = timestamps[end_frame] if end_frame < frame_count else end_ns
        if start >= end:
            continue
        item: dict[str, object] = {
            "id": f"ai_{job.id[:12]}_{index:04d}",
            "start_ns": str(start),
            "end_ns": str(end),
            "description": description.strip()[:2_000],
        }
        if (
            isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and 0 <= float(confidence) <= 1
        ):
            item["confidence"] = float(confidence)
        result.append(item)
    return result


def _preview_frame_timestamps(
    preview: EpisodeArtifact, *, start_ns: int, end_ns: int
) -> tuple[int, ...] | None:
    metadata = preview.metadata_json if isinstance(preview.metadata_json, dict) else {}
    raw = metadata.get("frame_timestamps_ns")
    if raw is None:
        return None
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_AI_PREVIEW_TIMELINE_FRAMES:
        raise EpisodeAiSuggestionError("ai_preview_timeline_unavailable")
    values: list[int] = []
    prior: int | None = None
    for value in raw:
        timestamp = _timestamp(value)
        if (
            timestamp is None
            or timestamp < start_ns
            or timestamp >= end_ns
            or (prior is not None and timestamp <= prior)
        ):
            raise EpisodeAiSuggestionError("ai_preview_timeline_unavailable")
        values.append(timestamp)
        prior = timestamp
    return tuple(values)


def _metadata(episode: Episode) -> dict[str, object]:
    return dict(episode.metadata_json or {}) if isinstance(episode.metadata_json, dict) else {}


def _duration_seconds(episode: Episode) -> float | None:
    try:
        start_ns, end_ns = episode_timeline_bounds(episode)
    except EpisodeMultimodalError:
        return None
    return (end_ns - start_ns) / 1_000_000_000


def _streams(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in value[:MAX_STREAMS]:
        if not isinstance(row, dict):
            continue
        stream_id = _safe_identifier(row.get("id"))
        kind = row.get("kind")
        if (
            stream_id is None
            or not isinstance(kind, str)
            or kind not in _STREAM_KINDS
            or stream_id in seen
        ):
            continue
        item: dict[str, object] = {"id": stream_id, "kind": kind}
        for field in ("width", "height"):
            integer = _bounded_positive_int(row.get(field), maximum=16_384)
            if integer is not None:
                item[field] = integer
        frequency = _bounded_positive_float(
            row.get("fps", row.get("frequency_hz")), maximum=1_000.0
        )
        if frequency is not None:
            item["fps" if kind == "camera" else "frequency_hz"] = frequency
        normalized.append(item)
        seen.add(stream_id)
    return normalized


def _timeseries(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in value[:MAX_STREAMS]:
        if not isinstance(row, dict):
            continue
        series_id = _safe_identifier(row.get("id"))
        kind = row.get("kind")
        if (
            series_id is None
            or not isinstance(kind, str)
            or kind not in _STREAM_KINDS
            or series_id in seen
        ):
            continue
        item: dict[str, object] = {"id": series_id, "kind": kind}
        frequency = _bounded_positive_float(row.get("frequency_hz"), maximum=10_000.0)
        if frequency is not None:
            item["frequency_hz"] = frequency
        normalized.append(item)
        seen.add(series_id)
    return normalized


def _event_tracks(value: object, *, start_ns: int, end_ns: int) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, object]] = []
    for raw_track in value[:MAX_EVENT_TRACKS]:
        if not isinstance(raw_track, dict):
            continue
        track_id = _safe_identifier(raw_track.get("id"))
        kind = raw_track.get("kind")
        raw_events = raw_track.get("events")
        if (
            track_id is None
            or not isinstance(kind, str)
            or kind not in _EVENT_KINDS
            or not isinstance(raw_events, list)
        ):
            continue
        events: list[dict[str, object]] = []
        for raw_event in raw_events[:MAX_EVENTS_PER_TRACK]:
            if not isinstance(raw_event, dict):
                continue
            event_start = _timestamp(raw_event.get("start_ns"))
            event_end = _timestamp(raw_event.get("end_ns"))
            label = raw_event.get("label")
            if (
                event_start is None
                or event_end is None
                or event_start < start_ns
                or event_end > end_ns
                or event_start >= event_end
                or not isinstance(label, str)
                or not label.strip()
                or len(label.strip()) > 256
            ):
                continue
            events.append(
                {"start_ns": str(event_start), "end_ns": str(event_end), "label": label.strip()}
            )
        result.append({"id": track_id, "kind": kind, "events": events})
    return result


def _safe_identifier(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 512
        or "\x00" in normalized
        or "\\" in normalized
        or "://" in normalized
    ):
        return None
    return normalized


def _timestamp(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdigit() and len(value) <= 20:
        return int(value)
    return None


def _bounded_positive_int(value: object, *, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= maximum else None


def _bounded_positive_float(value: object, *, maximum: float) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= maximum else None
