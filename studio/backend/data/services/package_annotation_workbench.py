"""Frozen package annotation sources, drafts and read-only workbench projections.

The HTTP surface never expands package membership or accepts a media path. All
nanoseconds cross the browser boundary as canonical decimal strings.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from pydantic import ValidationError
from qrdf.models.annotation import EpisodeAnnotation as QrdfEpisodeAnnotation
from sqlalchemy.orm import Session

from data.database import Episode, User
from data.infra.object_storage import StorageObjectRef
from data.infra.storage_provider import get_storage_provider
from data.models.annotation_work import AnnotationSubmission, AnnotationWorkItem, ReviewWorkItem
from data.models.data_batch import DataBatch, DataBatchEpisode
from data.models.data_package import DataPackage
from data.models.episode_admission import EpisodeAdmissionFact
from data.services.annotation_work_items import (
    AnnotationWorkConflict,
    AnnotationWorkError,
    AnnotationWorkForbidden,
)
from data.services.episode_admission import episode_review_decisions
from data.services.episode_multimodal import EpisodeMultimodalError, episode_timeline_bounds
from data.utils.formatting import format_api_datetime

DRAFT_SCHEMA = "quicstudio.package-annotation-draft.v1"
MAX_SEGMENTS = 1000
MAX_TIMELINE_BYTES = 32 * 1024 * 1024
MAX_TIMELINE_FRAMES = 250_000
_NS = re.compile(r"^(?:0|[1-9][0-9]{0,19})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WRITABLE = frozenset({"assigned", "in_progress", "returned"})


def _timestamp(value: object) -> int:
    if not isinstance(value, str) or not _NS.fullmatch(value):
        raise AnnotationWorkError("annotation_timestamp_invalid")
    return int(value)


def _uuid7() -> str:
    random_bits = uuid4().int
    value = ((time.time_ns() // 1_000_000) << 80) | (7 << 76)
    value |= ((random_bits >> 64) & 0xFFF) << 64
    value |= (2 << 62) | (random_bits & ((1 << 62) - 1))
    return str(UUID(int=value))


def frozen_members(
    db: Session, item: AnnotationWorkItem
) -> list[tuple[Episode, EpisodeAdmissionFact]]:
    """Resolve accepted frozen attempts, never current package membership."""
    raw = item.episode_members_json
    if not isinstance(raw, list) or not raw:
        raise AnnotationWorkError("annotation_members_invalid")
    requested = []
    for member in raw:
        if (
            not isinstance(member, dict)
            or type(member.get("episode_id")) is not int
            or type(member.get("admission_attempt")) is not int
            or member["episode_id"] <= 0
            or member["admission_attempt"] <= 0
        ):
            raise AnnotationWorkError("annotation_members_invalid")
        requested.append((member["episode_id"], member["admission_attempt"]))
    ids = [episode_id for episode_id, _ in requested]
    if len(set(ids)) != len(ids):
        raise AnnotationWorkError("annotation_members_invalid")
    episodes = {
        episode.id: episode
        for episode in db.query(Episode)
        .filter(
            Episode.id.in_(ids),
            Episode.workspace_id == item.workspace_id,
            Episode.data_package_id == item.data_package_id,
        )
        .all()
    }
    facts = {
        (fact.episode_id, fact.attempt): fact
        for fact in db.query(EpisodeAdmissionFact)
        .filter(
            EpisodeAdmissionFact.episode_id.in_(ids),
        )
        .all()
    }
    batch_members = {
        member.episode_id: member
        for member in db.query(DataBatchEpisode)
        .filter(
            DataBatchEpisode.data_batch_id == item.data_batch_id,
            DataBatchEpisode.episode_id.in_(ids),
        )
        .all()
    }
    decisions = episode_review_decisions(db, data_package_id=item.data_package_id)
    result = []
    for episode_id, attempt in requested:
        episode, fact = episodes.get(episode_id), facts.get((episode_id, attempt))
        member = batch_members.get(episode_id)
        if (
            episode is None
            or fact is None
            or member is None
            or member.data_package_id != item.data_package_id
            or member.admission_attempt != attempt
            or decisions.get((episode_id, attempt)) != "accepted"
        ):
            raise AnnotationWorkConflict("annotation_frozen_source_unavailable")
        if (
            not episode.source_fingerprint
            or fact.source_fingerprint != episode.source_fingerprint
            or fact.integrity_status != "passed"
            or fact.output_verification_status != "verified"
            or fact.preview_status not in {"ready", "not_applicable"}
        ):
            raise AnnotationWorkConflict("annotation_frozen_source_changed")
        result.append((episode, fact))
    return result


def source_binding(episode: Episode, fact: EpisodeAdmissionFact) -> dict:
    metadata = episode.metadata_json or {}
    upload = metadata.get("collection_upload") or {}
    external_id = upload.get("external_episode_id") or episode.episode_uid
    from data.services.episode_objects import EpisodeObjectsError, data_object, fact_objects

    try:
        source = data_object(fact_objects(fact))
    except EpisodeObjectsError as exc:
        raise AnnotationWorkError("annotation_source_hash_unavailable") from exc
    if not source.path.endswith(".mcap"):
        raise AnnotationWorkError("annotation_source_hash_unavailable")
    mcap = [{**source.ref, "relative_path": source.path}]
    try:
        start, end = episode_timeline_bounds(episode)
    except EpisodeMultimodalError as exc:
        raise AnnotationWorkError(str(exc)) from exc
    return {
        "episode_id": episode.id,
        "qrdf_episode_id": external_id,
        "admission_attempt": fact.attempt,
        "source_fingerprint": fact.source_fingerprint,
        "data_sha256": mcap[0]["sha256"],
        "start_ns": str(start),
        "end_ns": str(end),
    }


def normalize_draft(draft: dict, *, member_ids: set[int], complete: bool = False) -> dict:
    """Validate the editor envelope without requiring unfinished descriptions."""
    if (
        not isinstance(draft, dict)
        or set(draft) - {"schema", "episodes", "source", "review_required"}
        or draft.get("schema") != DRAFT_SCHEMA
        or not isinstance(draft.get("episodes"), dict)
    ):
        raise AnnotationWorkError("annotation_draft_invalid")
    entries = draft["episodes"]
    expected = {str(episode_id) for episode_id in member_ids}
    if not set(entries).issubset(expected) or (complete and set(entries) != expected):
        raise AnnotationWorkError("annotation_draft_membership_mismatch")
    normalized = {}
    for key, entry in entries.items():
        if not isinstance(entry, dict) or set(entry) - {
            "conclusion",
            "segments",
            "reason",
            "qrdf_payload",
        }:
            raise AnnotationWorkError(f"episode:{key}:annotation_entry_invalid")
        conclusion = entry.get("conclusion")
        segments = entry.get("segments", [])
        if conclusion not in {"segments", "no_valid_segments"} or not isinstance(segments, list):
            raise AnnotationWorkError(f"episode:{key}:annotation_conclusion_required")
        reason = entry.get("reason", "")
        if not isinstance(reason, str) or len(reason) > 2000:
            raise AnnotationWorkError(f"episode:{key}:annotation_reason_invalid")
        if conclusion == "no_valid_segments":
            if segments or (complete and not reason.strip()):
                raise AnnotationWorkError(f"episode:{key}:annotation_no_valid_reason_required")
            normalized[key] = {"conclusion": conclusion, "reason": reason.strip(), "segments": []}
            continue
        if len(segments) > MAX_SEGMENTS or (complete and not segments):
            raise AnnotationWorkError(f"episode:{key}:annotation_segments_required")
        parsed = []
        segment_ids = set()
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict) or set(segment) - {
                "id",
                "start_ns",
                "end_ns",
                "description",
            }:
                raise AnnotationWorkError(
                    f"episode:{key}:segment:{index}:annotation_segment_invalid"
                )
            start, end = _timestamp(segment.get("start_ns")), _timestamp(segment.get("end_ns"))
            description = segment.get("description", "")
            segment_id = segment.get("id")
            if (
                start >= end
                or not isinstance(description, str)
                or len(description) > 4000
                or not isinstance(segment_id, str)
                or not segment_id
                or len(segment_id) > 128
                or segment_id in segment_ids
            ):
                raise AnnotationWorkError(
                    f"episode:{key}:segment:{index}:annotation_segment_invalid"
                )
            if complete and not description.strip():
                raise AnnotationWorkError(
                    f"episode:{key}:segment:{index}:annotation_description_required"
                )
            segment_ids.add(segment_id)
            parsed.append(
                {
                    "id": segment_id,
                    "start_ns": str(start),
                    "end_ns": str(end),
                    "description": description.strip(),
                }
            )
        parsed.sort(key=lambda segment: (int(segment["start_ns"]), int(segment["end_ns"])))
        for previous, current in zip(parsed, parsed[1:], strict=False):
            if int(current["start_ns"]) < int(previous["end_ns"]):
                raise AnnotationWorkError(f"episode:{key}:annotation_segments_overlap")
        normalized[key] = {"conclusion": conclusion, "segments": parsed}
        # The algorithm compatibility adapter retains its original typed QRDF
        # payload, which is checked again against these exact effective ranges.
        if "qrdf_payload" in entry:
            normalized[key]["qrdf_payload"] = deepcopy(entry["qrdf_payload"])
    result = {"schema": DRAFT_SCHEMA, "episodes": normalized}
    for key in ("source", "review_required"):
        if key in draft:
            result[key] = deepcopy(draft[key])
    return result


def adapt_legacy_qrdf_draft(draft: dict) -> dict:
    """One compatibility adapter for the pre-workbench algorithm API envelope.

    An empty track list never implies that an Episode was explicitly discarded.
    Version omission is handled by the caller under the work-item row lock.
    """
    if draft.get("schema") == DRAFT_SCHEMA:
        return draft
    if not draft:
        raise AnnotationWorkError("annotation_draft_required")
    if set(draft) - {"episodes", "source", "review_required"} or not isinstance(
        draft.get("episodes"), dict
    ):
        raise AnnotationWorkError("annotation_draft_invalid")
    entries = {}
    for key, payload in draft["episodes"].items():
        try:
            typed = QrdfEpisodeAnnotation.model_validate(payload)
        except (ValidationError, TypeError):
            raise AnnotationWorkError(f"episode:{key}:annotation_draft_payload_invalid") from None
        segments = [
            {
                "id": item.id,
                "start_ns": item.target.start_ns,
                "end_ns": item.target.end_ns,
                "description": item.high_level_subtask,
            }
            for track in typed.tracks
            if track.name == "high_level_subtask"
            for item in track.items
        ]
        if not segments:
            raise AnnotationWorkError(f"episode:{key}:annotation_segments_required")
        entries[key] = {
            "conclusion": "segments",
            "segments": segments,
            "qrdf_payload": typed.model_dump(mode="json", exclude_none=True),
        }
    return {**draft, "schema": DRAFT_SCHEMA, "episodes": entries}


def _preview_ref(raw: object) -> StorageObjectRef:
    if not isinstance(raw, dict):
        raise AnnotationWorkError("annotation_preview_unavailable")
    try:
        ref = StorageObjectRef(**raw)
    except TypeError:
        raise AnnotationWorkError("annotation_preview_unavailable") from None
    if (
        ref.bucket_role != "process"
        or not (ref.version_id or ref.etag)
        or type(ref.size_bytes) is not int
        or ref.size_bytes < 1
        or not ref.sha256
        or not _SHA256.fullmatch(ref.sha256)
    ):
        raise AnnotationWorkError("annotation_preview_unavailable")
    return ref


def _preview_descriptor(fact: EpisodeAdmissionFact) -> dict:
    from data.services.episode_objects import EpisodeObjectsError, fact_objects, preview_streams

    try:
        streams = preview_streams(fact_objects(fact))
    except EpisodeObjectsError as exc:
        raise AnnotationWorkError("annotation_preview_unavailable") from exc
    if not streams:
        raise AnnotationWorkError("annotation_preview_unavailable")
    first = streams[0]
    return {"topic": first["topic"], "video": first["video"].ref, "timeline": first["timeline"].ref}


def exact_playback_timeline(episode: Episode, fact: EpisodeAdmissionFact) -> dict:
    """Read only the small immutable mapping, never media or the raw MCAP."""
    preview = _preview_descriptor(fact)
    ref = _preview_ref(preview.get("timeline"))
    if ref.size_bytes > MAX_TIMELINE_BYTES:
        raise AnnotationWorkError("annotation_preview_timeline_too_large")
    try:
        with TemporaryDirectory(prefix="quicstudio-annotation-timeline-") as directory:
            path = Path(directory) / "timeline.json"
            get_storage_provider().download_file(ref, str(path))
            if path.stat().st_size != ref.size_bytes or path.stat().st_size > MAX_TIMELINE_BYTES:
                raise AnnotationWorkError("annotation_preview_timeline_invalid")
            raw_bytes = path.read_bytes()
            if hashlib.sha256(raw_bytes).hexdigest() != ref.sha256:
                raise AnnotationWorkError("annotation_preview_timeline_changed")
            payload = json.loads(raw_bytes)
    except AnnotationWorkError:
        raise
    except Exception as exc:
        raise AnnotationWorkError("annotation_preview_timeline_unavailable") from exc
    return validate_playback_timeline(payload, episode=episode, topic=preview.get("topic"))


def validate_playback_timeline(payload: object, *, episode: Episode, topic: str | None) -> dict:
    if not isinstance(payload, dict) or payload.get("topic") != topic:
        raise AnnotationWorkError("annotation_preview_timeline_invalid")
    raw = payload.get("entries")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_TIMELINE_FRAMES:
        raise AnnotationWorkError("annotation_preview_timeline_invalid")
    start, end = episode_timeline_bounds(episode)
    entries = []
    previous_ns = previous_pts = -1
    for entry in raw:
        if not isinstance(entry, dict):
            raise AnnotationWorkError("annotation_preview_timeline_invalid")
        if entry.get("kind") == "dropped":
            continue
        stamp, pts = _timestamp(entry.get("timestamp_ns")), _timestamp(entry.get("video_pts_us"))
        if (
            entry.get("kind") != "frame"
            or not start <= stamp < end
            or stamp <= previous_ns
            or pts <= previous_pts
            or type(entry.get("frame_index")) is not int
            or entry["frame_index"] != len(entries)
        ):
            raise AnnotationWorkError("annotation_preview_timeline_invalid")
        entries.append(
            {
                "timestamp_ns": str(stamp),
                "video_pts_us": str(pts),
                "frame_index": len(entries),
                "kind": "frame",
            }
        )
        previous_ns, previous_pts = stamp, pts
    if not entries:
        raise AnnotationWorkError("annotation_preview_timeline_invalid")
    return {"kind": "qrdf_preview_timeline", "topic": topic, "entries": entries}


def _validate_qrdf(payload: dict, binding: dict, segments: list[dict]) -> dict:
    try:
        typed = QrdfEpisodeAnnotation.model_validate(payload)
    except ValidationError:
        raise AnnotationWorkError("annotation_draft_payload_invalid") from None
    if (
        typed.source.episode_id != binding["qrdf_episode_id"]
        or typed.source.data_sha256 != binding["data_sha256"]
    ):
        raise AnnotationWorkError("annotation_qrdf_source_mismatch")
    track_names, item_ids, parents, atomic = set(), set(), {}, []
    start, end = int(binding["start_ns"]), int(binding["end_ns"])
    actual_segments = []
    for track in typed.tracks:
        if track.name in track_names:
            raise AnnotationWorkError("annotation_qrdf_duplicate_track")
        track_names.add(track.name)
        for entry in track.items:
            if entry.id in item_ids:
                raise AnnotationWorkError("annotation_qrdf_duplicate_item")
            item_ids.add(entry.id)
            target = entry.target
            if target.type == "time_range":
                if not start <= int(target.start_ns) < int(target.end_ns) <= end:
                    raise AnnotationWorkError("annotation_segments_out_of_bounds")
            elif not start <= int(target.timestamp_ns) < end:
                raise AnnotationWorkError("annotation_segments_out_of_bounds")
            if track.name == "high_level_subtask":
                parents[entry.id] = entry
                actual_segments.append(
                    (entry.id, target.start_ns, target.end_ns, entry.high_level_subtask)
                )
            elif track.name == "atomic_subtask":
                atomic.append(entry)
    expected = [
        (entry["id"], entry["start_ns"], entry["end_ns"], entry["description"])
        for entry in segments
    ]
    if sorted(actual_segments) != sorted(expected):
        raise AnnotationWorkError("annotation_qrdf_effective_ranges_mismatch")
    siblings = {}
    for entry in atomic:
        parent = parents.get(entry.parent_high_level_subtask_id)
        if (
            parent is None
            or int(entry.target.start_ns) < int(parent.target.start_ns)
            or int(entry.target.end_ns) > int(parent.target.end_ns)
        ):
            raise AnnotationWorkError("annotation_qrdf_atomic_parent_invalid")
        siblings.setdefault(parent.id, []).append(entry)
    for entries in siblings.values():
        entries.sort(key=lambda entry: int(entry.target.start_ns))
        if any(
            int(current.target.start_ns) < int(previous.target.end_ns)
            for previous, current in zip(entries, entries[1:], strict=False)
        ):
            raise AnnotationWorkError("annotation_qrdf_atomic_overlap")
    return typed.model_dump(mode="json", exclude_none=True)


def validated_submission_entries(
    db: Session, item: AnnotationWorkItem, *, require_mapping: bool
) -> tuple[dict, list[dict]]:
    members = frozen_members(db, item)
    draft = normalize_draft(
        adapt_legacy_qrdf_draft(item.draft_json or {}),
        member_ids={episode.id for episode, _ in members},
        complete=True,
    )
    results = []
    for episode, fact in members:
        binding = source_binding(episode, fact)
        entry = deepcopy(draft["episodes"][str(episode.id)])
        if require_mapping:
            exact_playback_timeline(episode, fact)
        segments = entry["segments"]
        for segment in segments:
            if (
                not int(binding["start_ns"])
                <= int(segment["start_ns"])
                < int(segment["end_ns"])
                <= int(binding["end_ns"])
            ):
                raise AnnotationWorkError(f"episode:{episode.id}:annotation_segments_out_of_bounds")
        payload = None
        if entry["conclusion"] == "segments":
            payload = entry.pop("qrdf_payload", None)
            if payload is None:
                for segment in segments:
                    segment["id"] = _uuid7()
                payload = {
                    "kind": "episode_annotation",
                    "qrdf_version": "0.2.0",
                    "source": {
                        "episode_id": binding["qrdf_episode_id"],
                        "data_sha256": binding["data_sha256"],
                    },
                    "episode": {"outcome": "unknown"},
                    "tracks": [
                        {
                            "name": "high_level_subtask",
                            "items": [
                                {
                                    "id": segment["id"],
                                    "target": {
                                        "type": "time_range",
                                        "start_ns": segment["start_ns"],
                                        "end_ns": segment["end_ns"],
                                    },
                                    "high_level_subtask": segment["description"],
                                }
                                for segment in segments
                            ],
                        }
                    ],
                }
            payload = _validate_qrdf(payload, binding, segments)
        results.append(
            {
                **entry,
                **binding,
                "annotation_payload": payload,
                "effective_duration_ns": str(
                    sum(int(segment["end_ns"]) - int(segment["start_ns"]) for segment in segments)
                ),
            }
        )
    return draft, results


def _authorized_item(db: Session, *, workspace_id: int, item_id: int, actor: User, review: bool):
    model = ReviewWorkItem if review else AnnotationWorkItem
    item = (
        db.query(model)
        .filter(model.id == item_id, model.workspace_id == workspace_id)
        .one_or_none()
    )
    if item is None:
        raise LookupError("work item does not exist")
    if item.assignee_user_id != actor.id and actor.role != "admin":
        raise AnnotationWorkForbidden("work item is assigned to another user")
    annotation = item.annotation_item if review else item
    return item, annotation


def _submission(
    db: Session, annotation: AnnotationWorkItem, submission_id: int | None
) -> AnnotationSubmission:
    row = db.get(AnnotationSubmission, submission_id) if submission_id else None
    if (
        row is None
        or row.annotation_work_item_id != annotation.id
        or row.workspace_id != annotation.workspace_id
    ):
        raise AnnotationWorkConflict("annotation_historical_result_uncertain")
    return row


def _item_projection(item, annotation: AnnotationWorkItem) -> dict:
    return {
        "id": item.id,
        "annotation_work_item_id": annotation.id,
        "workspace_id": item.workspace_id,
        "data_batch_id": annotation.data_batch_id,
        "data_package_id": annotation.data_package_id,
        "assignee_user_id": item.assignee_user_id,
        "status": item.status,
        "generation": item.generation,
        "draft_version": annotation.draft_version,
        "submission_id": getattr(item, "submission_id", annotation.current_submission_id),
        "return_reason": annotation.return_reason or "",
    }


def package_workbench(
    db: Session, *, workspace_id: int, item_id: int, actor: User, review: bool = False
) -> dict:
    item, annotation = _authorized_item(
        db, workspace_id=workspace_id, item_id=item_id, actor=actor, review=review
    )
    members = frozen_members(db, annotation)
    fixed = review or annotation.status in {"submitted", "done"}
    submission_id = item.submission_id if review else annotation.current_submission_id
    submission = _submission(db, annotation, submission_id) if fixed else None
    draft = deepcopy(
        submission.draft_json
        if submission
        else annotation.draft_json or {"schema": DRAFT_SCHEMA, "episodes": {}}
    )
    entries = (
        {str(entry["episode_id"]): entry for entry in submission.episodes_json}
        if submission
        else draft.get("episodes", {})
    )
    package, batch = (
        db.get(DataPackage, annotation.data_package_id),
        db.get(DataBatch, annotation.data_batch_id),
    )
    summaries = []
    for episode, fact in members:
        entry = entries.get(str(episode.id), {})
        summaries.append(
            {
                "id": episode.id,
                "episode_id": episode.id,
                "episode_uid": episode.episode_uid,
                "admission_attempt": fact.attempt,
                "conclusion": entry.get("conclusion"),
                "segment_count": len(entry.get("segments", [])),
                "effective_duration_ns": str(
                    sum(
                        int(segment["end_ns"]) - int(segment["start_ns"])
                        for segment in entry.get("segments", [])
                    )
                ),
                "processed": bool(entry.get("conclusion")),
            }
        )
    can_edit = not review and annotation.status in _WRITABLE and item.assignee_user_id == actor.id
    can_review = (
        review
        and item.assignee_user_id == actor.id
        and annotation.status == "submitted"
        and item.status in {"assigned", "in_progress"}
        and item.submission_id == annotation.current_submission_id
    )
    return {
        "item": _item_projection(item, annotation),
        "package": {
            "id": package.id,
            "package_uid": package.package_uid,
            "collection_task_id": package.collection_task_id,
            "task_name": package.task.name if package.task else "",
            "batch_name": batch.name,
        },
        "episodes": summaries,
        "draft_json": draft,
        "draft_version": annotation.draft_version,
        "generation": item.generation,
        "submission_id": submission.id if submission else None,
        "submission": {
            "id": submission.id,
            "draft_version": submission.draft_version,
            "generation": submission.generation,
            "created_at": format_api_datetime(submission.created_at),
        }
        if submission
        else None,
        "capabilities": {
            "edit": can_edit,
            "save": can_edit,
            "submit": can_edit,
            "approve": bool(can_review),
            "return": bool(can_review),
            "read_only": not can_edit,
        },
    }


def _media_projection(episode: Episode, fact: EpisodeAdmissionFact) -> dict:
    unavailable = {
        "available": False,
        "url": "",
        "media_type": "video/mp4",
        "playback_timeline": None,
        "mapping_available": False,
    }
    try:
        descriptor = _preview_descriptor(fact)
        video = _preview_ref(descriptor.get("video"))
        timeline_ref = _preview_ref(descriptor.get("timeline"))
        provider = get_storage_provider()
        url = provider.sign_get(video, expires=300)
        timeline_url = provider.sign_get(timeline_ref, expires=300)
    except Exception:
        return {
            **unavailable,
            "status": "unavailable",
            "disabled_reason": "annotation_preview_unavailable",
        }
    try:
        timeline = exact_playback_timeline(episode, fact)
        reason = ""
    except (AnnotationWorkError, EpisodeMultimodalError) as exc:
        timeline, reason = None, str(exc)
    return {
        "available": True,
        "url": url,
        "timeline_url": timeline_url,
        "topic": descriptor.get("topic"),
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat(),
        "direct": True,
        "media_type": "video/mp4",
        "playback_timeline": timeline,
        "mapping_available": timeline is not None,
        "status": "ready",
        "disabled_reason": reason,
    }


def episode_workbench(
    db: Session,
    *,
    workspace_id: int,
    item_id: int,
    episode_id: int,
    actor: User,
    review: bool = False,
) -> dict:
    item, annotation = _authorized_item(
        db, workspace_id=workspace_id, item_id=item_id, actor=actor, review=review
    )
    # Reject a foreign Episode before any media lookup or storage access.
    members = frozen_members(db, annotation)
    selected = next(
        ((episode, fact) for episode, fact in members if episode.id == episode_id), None
    )
    if selected is None:
        raise LookupError("episode does not belong to this work item")
    episode, fact = selected
    fixed = review or annotation.status in {"submitted", "done"}
    submission_id = item.submission_id if review else annotation.current_submission_id
    submission = _submission(db, annotation, submission_id) if fixed else None
    if submission:
        entry = next(
            (
                deepcopy(entry)
                for entry in submission.episodes_json
                if entry["episode_id"] == episode_id
            ),
            None,
        )
        if entry is None:
            raise AnnotationWorkConflict("annotation_submission_membership_mismatch")
        binding = {
            key: entry[key]
            for key in (
                "episode_id",
                "qrdf_episode_id",
                "admission_attempt",
                "source_fingerprint",
                "data_sha256",
                "start_ns",
                "end_ns",
            )
        }
    else:
        entry = deepcopy((annotation.draft_json or {}).get("episodes", {}).get(str(episode_id)))
        binding = source_binding(episode, fact)
    preview = _media_projection(episode, fact)
    can_edit = (
        not fixed
        and annotation.status in _WRITABLE
        and item.assignee_user_id == actor.id
        and preview["mapping_available"]
    )
    return {
        "item": _item_projection(item, annotation),
        "episode": {
            "id": episode.id,
            "episode_uid": episode.episode_uid,
            "admission_attempt": fact.attempt,
        },
        "source_binding": binding,
        "timeline": {
            "start_ns": binding["start_ns"],
            "end_ns": binding["end_ns"],
            "duration_s": (int(binding["end_ns"]) - int(binding["start_ns"])) / 1_000_000_000,
        },
        "media": {"preview": preview},
        "entry": entry,
        "submission_id": submission.id if submission else None,
        "draft_version": annotation.draft_version,
        "generation": item.generation,
        "capabilities": {
            "edit": bool(can_edit),
            "read_only": not can_edit,
            "mapping_available": preview["mapping_available"],
            "disabled_reason": ""
            if can_edit or fixed
            else preview.get("disabled_reason", "annotation_not_writable"),
        },
    }
