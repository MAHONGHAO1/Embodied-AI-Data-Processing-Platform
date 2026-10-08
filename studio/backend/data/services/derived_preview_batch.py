"""Durable contracts for one derived-preview batch per accepted cut review."""

from __future__ import annotations

import hashlib
import logging
import math
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from sqlalchemy import cast, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from data.database import ArtifactOperation, Episode, EpisodeArtifact, JobRun, WorkItem, Workspace
from data.infra import oss_client
from data.integrations.qrdf.preview_export import (
    PreviewSegmentFacts,
    PreviewSegmentSpec,
    export_preview_segments_from_mp4,
)
from data.realtime.outbox import enqueue_work_queue_invalidated
from data.services.artifact_operations import (
    complete_artifact_operation,
    ensure_artifact_operation,
    materialize_artifact_operation,
)
from data.services.batch_paths import process_episode_run_prefix
from data.services.storage_mode import uses_cloud_uri_authority
from data.utils.storage_paths import (
    cloud_path_is_safe,
    is_under_storage_root,
    resolve_storage_path,
    storage_root_path,
)
from data.utils.storage_uri import is_public_storage_uri, parse_storage_uri

DERIVED_PREVIEW_BATCH_JOB_KIND = "derived_preview_batch"
DERIVED_PREVIEW_BATCH_QUEUE = "media"
_LEGACY_PREVIEW_JOB_KIND = "episode_preview"
MAX_DERIVED_PREVIEW_SEGMENTS = 1000
_MAX_PARENT_PREVIEW_TIMELINE_FRAMES = 100_000
_PROCESS_PREVIEW_PREFIXES = ("process/v1/", "process/v2/")
_JOB_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ParentPreviewFacts:
    artifact: EpisodeArtifact
    reference_topic: str
    encoded_fps: float
    frame_timestamps_ns: tuple[int, ...]


@dataclass(frozen=True)
class _ChildPreviewRequest:
    episode: Episode
    frame_timestamps_ns: tuple[int, ...]
    output_path: Path


def ensure_derived_preview_batch_job(
    db: Session,
    *,
    source: Episode,
    review_item_id: int,
    actor_id: int | None,
) -> JobRun:
    """Create or return the one media job for a source/review pair."""
    source_id, review_id, _workspace_id, _task_set_id = _source_review_scope(source, review_item_id)
    key = f"derived-preview-batch:{source_id}:review:{review_id}:v1"
    existing = db.scalar(select(JobRun).where(JobRun.idempotency_key == key))
    if existing is not None:
        return existing
    job = JobRun(
        id=uuid4().hex,
        kind=DERIVED_PREVIEW_BATCH_JOB_KIND,
        resource_type="episode",
        resource_id=str(source_id),
        workspace_id=source.workspace_id,
        task_set_id=source.task_set_id,
        idempotency_key=key,
        queue=DERIVED_PREVIEW_BATCH_QUEUE,
        actor_id=actor_id,
        status="queued",
        phase="queued",
        detail_json={
            "source_episode_id": source_id,
            "cut_review_work_item_id": review_id,
        },
    )
    db.add(job)
    db.flush()
    return job


def latest_derived_preview_batch_job(
    db: Session,
    *,
    source: Episode,
    review_item_id: int,
) -> JobRun | None:
    """Return the newest valid batch job for one source/review pair."""
    scope = _source_review_scope(source, review_item_id)
    return _latest_batch_jobs_for_scopes(db, scopes={scope}).get(scope)


def latest_preview_job_for_episode(db: Session, *, episode: Episode) -> JobRun | None:
    """Return the exact batch job when available, otherwise the legacy child job."""
    episode_id = _positive_int(episode.id)
    if episode_id is None:
        return None
    return latest_preview_jobs_for_episodes(db, episodes=(episode,)).get(episode_id)


def latest_preview_jobs_for_episodes(
    db: Session,
    *,
    episodes: Sequence[Episode],
) -> dict[int, JobRun]:
    """Load legacy and exact batch preview jobs for one queue page without N+1 queries."""
    episode_ids = {
        episode_id
        for episode in episodes
        if episode.kind in {"source", "derived"}
        and (episode_id := _positive_int(episode.id)) is not None
    }
    if not episode_ids:
        return {}
    jobs_by_episode_id = _latest_legacy_preview_jobs(db, episode_ids=episode_ids)
    scopes_by_episode_id = {
        episode_id: scope
        for episode in episodes
        if (episode_id := _positive_int(episode.id)) is not None
        and (scope := _derived_review_scope(episode)) is not None
    }
    if not scopes_by_episode_id:
        return jobs_by_episode_id
    batch_jobs = _latest_batch_jobs_for_scopes(db, scopes=set(scopes_by_episode_id.values()))
    for episode_id, scope in scopes_by_episode_id.items():
        batch_job = batch_jobs.get(scope)
        if batch_job is not None:
            jobs_by_episode_id[episode_id] = batch_job
    return jobs_by_episode_id


def run_derived_preview_batch(db: Session, job: JobRun) -> dict[str, int]:
    """Generate and publish all missing child previews from one parent MP4 pass."""
    source, review_id = _resolve_batch_source_and_review(db, job=job)
    children = _resolve_batch_children(db, source=source, review_id=review_id)
    events_pending_before_attempt = _preview_events_pending(job)
    try:
        parent = _resolve_published_parent_preview(db, source=source)
    except ValueError:
        _persist_batch_progress_and_enqueue_events(
            db,
            job_id=job.id,
            workspace_id=source.workspace_id,
            result={
                "source_episode_id": source.id,
                "total_count": len(children),
                "completed_count": 0,
                "failed_count": len(children),
            },
            preview_events_pending=True if events_pending_before_attempt else None,
        )
        raise

    completed_count = 0
    failed_episode_ids: list[int] = []
    pending: list[tuple[Episode, tuple[int, ...]]] = []
    for child in children:
        expected_timestamps = _timestamps_for_child(parent.frame_timestamps_ns, child=child)
        if _has_trusted_published_child_preview(
            db,
            child=child,
            parent=parent,
            expected_timestamps=expected_timestamps,
        ):
            completed_count += 1
        elif not expected_timestamps:
            failed_episode_ids.append(child.id)
            _log_child_failure(child.id, error_type="EmptyPreviewSegment")
        else:
            pending.append((child, expected_timestamps))

    stage: Path | None = None
    published_child_ids: list[int] = []
    parent_materialization_failed = False
    events_marker_set = False
    try:
        if pending:
            stage = _prepare_batch_stage(source_id=source.id, job_id=job.id)
            requests = tuple(
                _ChildPreviewRequest(
                    episode=child,
                    frame_timestamps_ns=timestamps,
                    output_path=_child_output_path(stage, child_id=child.id),
                )
                for child, timestamps in pending
            )
            try:
                source_path = _materialize_parent_preview(
                    source=source, artifact=parent.artifact, stage=stage
                )
                _verify_parent_preview_file(source_path, artifact=parent.artifact)
            except Exception as exc:
                logger.warning(
                    "derived preview parent materialization failed source_episode_id=%s error_type=%s",
                    source.id,
                    type(exc).__name__,
                )
                failed_episode_ids.extend(request.episode.id for request in requests)
                parent_materialization_failed = True
            else:
                try:
                    encoded = export_preview_segments_from_mp4(
                        source_path,
                        source_timestamps_ns=parent.frame_timestamps_ns,
                        segments=tuple(
                            PreviewSegmentSpec(
                                episode_id=request.episode.id,
                                start_ns=request.episode.source_start_ns,
                                end_ns=request.episode.source_end_ns,
                                output_path=request.output_path,
                            )
                            for request in requests
                        ),
                        fps=parent.encoded_fps,
                    )
                except Exception as exc:
                    logger.warning(
                        "derived preview batch encoding failed source_episode_id=%s error_type=%s",
                        source.id,
                        type(exc).__name__,
                    )
                    failed_episode_ids.extend(request.episode.id for request in requests)
                else:
                    facts_by_episode_id = _encoded_facts_by_episode(
                        requests=requests, facts=encoded, stage=stage
                    )
                    if facts_by_episode_id:
                        _mark_preview_events_pending(db, job_id=job.id)
                        events_marker_set = True
                    for request in requests:
                        facts = facts_by_episode_id.get(request.episode.id)
                        if facts is None:
                            failed_episode_ids.append(request.episode.id)
                            _log_child_failure(
                                request.episode.id, error_type="InvalidPreviewOutput"
                            )
                            continue
                        try:
                            _publish_child_preview(
                                db, job=job, parent=parent, request=request, facts=facts
                            )
                        except Exception as exc:
                            db.rollback()
                            failed_episode_ids.append(request.episode.id)
                            _log_child_failure(request.episode.id, error_type=type(exc).__name__)
                            continue
                        completed_count += 1
                        published_child_ids.append(request.episode.id)

        result = {
            "source_episode_id": source.id,
            "total_count": len(children),
            "completed_count": completed_count,
            "failed_count": len(failed_episode_ids),
        }
        if events_pending_before_attempt or published_child_ids:
            _persist_batch_progress_and_enqueue_events(
                db,
                job_id=job.id,
                workspace_id=source.workspace_id,
                result=result,
                preview_events_pending=True,
            )
        elif events_marker_set:
            _persist_batch_progress(db, job_id=job.id, result=result, preview_events_pending=False)
        else:
            _persist_batch_progress(db, job_id=job.id, result=result)
        if parent_materialization_failed:
            raise ValueError("derived preview parent preview is unavailable")
        if failed_episode_ids:
            raise ValueError("derived preview batch generation failed")
        return result
    finally:
        if stage is not None:
            _remove_batch_stage(stage, source_id=source.id, job_id=job.id)


def _resolve_batch_source_and_review(db: Session, *, job: JobRun) -> tuple[Episode, int]:
    if (
        job.kind != DERIVED_PREVIEW_BATCH_JOB_KIND
        or job.resource_type != "episode"
        or job.queue != DERIVED_PREVIEW_BATCH_QUEUE
    ):
        raise ValueError("derived preview batch job is invalid")
    source_id = _positive_int(job.resource_id)
    detail = job.detail_json if isinstance(job.detail_json, dict) else {}
    detail_source_id = _positive_int(detail.get("source_episode_id"))
    review_id = _positive_int(detail.get("cut_review_work_item_id"))
    if (
        source_id is None
        or detail_source_id != source_id
        or review_id is None
        or job.resource_id != str(source_id)
    ):
        raise ValueError("derived preview batch job is invalid")
    source = db.scalar(select(Episode).where(Episode.id == source_id).with_for_update())
    if source is None or source.kind != "source":
        raise ValueError("derived preview source is unavailable")
    if job.workspace_id != source.workspace_id or job.task_set_id != source.task_set_id:
        raise ValueError("derived preview batch scope is invalid")
    _require_accepted_cut_review(db, source=source, review_id=review_id)
    return source, review_id


def _require_accepted_cut_review(db: Session, *, source: Episode, review_id: int) -> None:
    review = db.scalar(select(WorkItem).where(WorkItem.id == review_id).with_for_update())
    if (
        review is None
        or review.kind != "review"
        or review.review_target_kind != "cut"
        or review.status != "accepted"
        or review.workspace_id != source.workspace_id
        or review.episode_id != source.id
        or review.review_of_work_item_id is None
    ):
        raise ValueError("derived preview cut review is unavailable")
    cut_item = db.get(WorkItem, review.review_of_work_item_id)
    if (
        cut_item is None
        or cut_item.kind != "cut"
        or cut_item.status != "accepted"
        or cut_item.workspace_id != source.workspace_id
        or cut_item.episode_id != source.id
    ):
        raise ValueError("derived preview cut review is unavailable")


def _resolve_batch_children(db: Session, *, source: Episode, review_id: int) -> tuple[Episode, ...]:
    rows = list(
        db.scalars(
            select(Episode)
            .where(Episode.parent_episode_id == source.id, Episode.kind == "derived")
            .with_for_update()
        )
    )
    children: list[Episode] = []
    for child in rows:
        metadata = child.metadata_json if isinstance(child.metadata_json, dict) else {}
        lineage = metadata.get("lineage") if isinstance(metadata.get("lineage"), dict) else {}
        child_review_id = _positive_int(lineage.get("cut_review_work_item_id"))
        if child_review_id != review_id:
            continue
        if (
            child.parent_episode_id != source.id
            or child.workspace_id != source.workspace_id
            or child.task_set_id != source.task_set_id
            or child.batch_id != source.batch_id
            or type(child.source_start_ns) is not int
            or type(child.source_end_ns) is not int
            or child.source_start_ns < 0
            or child.source_start_ns >= child.source_end_ns
        ):
            raise ValueError("derived preview child scope is invalid")
        children.append(child)
    if not 1 <= len(children) <= MAX_DERIVED_PREVIEW_SEGMENTS:
        raise ValueError("derived preview child count is invalid")
    children.sort(key=lambda child: (child.source_start_ns, child.source_end_ns, child.id))
    previous_end_ns: int | None = None
    for child in children:
        if previous_end_ns is not None and child.source_start_ns < previous_end_ns:
            raise ValueError("derived preview child intervals overlap")
        previous_end_ns = child.source_end_ns
    return tuple(children)


def _resolve_published_parent_preview(db: Session, *, source: Episode) -> _ParentPreviewFacts:
    artifacts = list(
        db.scalars(
            select(EpisodeArtifact).where(
                EpisodeArtifact.episode_id == source.id,
                EpisodeArtifact.artifact_type == "process_preview",
                EpisodeArtifact.storage_role == "process",
            )
        )
    )
    trusted: list[_ParentPreviewFacts] = []
    for artifact in artifacts:
        if not _has_published_preview_operation(db, artifact=artifact):
            continue
        try:
            reference_topic, encoded_fps, timestamps = _trusted_preview_metadata(
                artifact,
                maximum_frames=_MAX_PARENT_PREVIEW_TIMELINE_FRAMES,
            )
        except ValueError:
            continue
        trusted.append(
            _ParentPreviewFacts(
                artifact=artifact,
                reference_topic=reference_topic,
                encoded_fps=encoded_fps,
                frame_timestamps_ns=timestamps,
            )
        )
    if len(trusted) != 1:
        raise ValueError("derived preview parent preview is unavailable")
    return trusted[0]


def _has_published_preview_operation(db: Session, *, artifact: EpisodeArtifact) -> bool:
    operation = db.scalar(
        select(ArtifactOperation).where(
            ArtifactOperation.artifact_id == artifact.id,
            ArtifactOperation.operation_kind == "process_preview_publish",
            ArtifactOperation.status == "published",
        )
    )
    return operation is not None and operation.target_uri == artifact.storage_uri


def _trusted_preview_metadata(
    artifact: EpisodeArtifact,
    *,
    maximum_frames: int,
) -> tuple[str, float, tuple[int, ...]]:
    metadata = artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
    if str(metadata.get("media_type") or "").lower() != "video/mp4":
        raise ValueError("preview media type is invalid")
    reference_topic = metadata.get("reference_topic")
    encoded_fps = metadata.get("encoded_fps")
    frame_count = metadata.get("frame_count")
    raw_timestamps = metadata.get("frame_timestamps_ns")
    if (
        not isinstance(reference_topic, str)
        or not reference_topic.strip()
        or len(reference_topic) > 512
        or "\x00" in reference_topic
        or isinstance(encoded_fps, bool)
        or not isinstance(encoded_fps, (int, float))
        or not math.isfinite(float(encoded_fps))
        or not 0 < float(encoded_fps) <= 240
        or type(frame_count) is not int
        or not isinstance(raw_timestamps, list)
        or not 1 <= len(raw_timestamps) <= maximum_frames
        or frame_count != len(raw_timestamps)
    ):
        raise ValueError("preview timeline is invalid")
    timestamps: list[int] = []
    previous_timestamp_ns: int | None = None
    for raw_timestamp in raw_timestamps:
        timestamp_ns = _timestamp_ns(raw_timestamp)
        if timestamp_ns is None or (
            previous_timestamp_ns is not None and timestamp_ns <= previous_timestamp_ns
        ):
            raise ValueError("preview timeline is invalid")
        timestamps.append(timestamp_ns)
        previous_timestamp_ns = timestamp_ns
    return reference_topic, float(encoded_fps), tuple(timestamps)


def _timestamp_ns(value: object) -> int | None:
    if type(value) is int:
        return value if value >= 0 else None
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    return None


def _timestamps_for_child(
    parent_timestamps_ns: tuple[int, ...],
    *,
    child: Episode,
) -> tuple[int, ...]:
    return tuple(
        timestamp_ns
        for timestamp_ns in parent_timestamps_ns
        if child.source_start_ns <= timestamp_ns < child.source_end_ns
    )


def _has_trusted_published_child_preview(
    db: Session,
    *,
    child: Episode,
    parent: _ParentPreviewFacts,
    expected_timestamps: tuple[int, ...],
) -> bool:
    if not expected_timestamps:
        return False
    artifacts = list(
        db.scalars(
            select(EpisodeArtifact).where(
                EpisodeArtifact.episode_id == child.id,
                EpisodeArtifact.artifact_type == "process_preview",
                EpisodeArtifact.storage_role == "process",
            )
        )
    )
    for artifact in artifacts:
        if not _has_published_preview_operation(db, artifact=artifact):
            continue
        try:
            reference_topic, encoded_fps, timestamps = _trusted_preview_metadata(
                artifact,
                maximum_frames=_MAX_PARENT_PREVIEW_TIMELINE_FRAMES,
            )
        except ValueError:
            continue
        if (
            reference_topic == parent.reference_topic
            and encoded_fps == parent.encoded_fps
            and timestamps == expected_timestamps
        ):
            return True
    return False


def _prepare_batch_stage(*, source_id: int, job_id: str) -> Path:
    stage = _batch_stage_dir(source_id=source_id, job_id=job_id)
    if stage.exists() or stage.is_symlink():
        _remove_batch_stage(stage, source_id=source_id, job_id=job_id)
    stage.mkdir(parents=True, exist_ok=False)
    return stage


def _batch_stage_dir(*, source_id: int, job_id: str) -> Path:
    if (
        _positive_int(source_id) is None
        or not isinstance(job_id, str)
        or _JOB_ID_RE.fullmatch(job_id) is None
    ):
        raise ValueError("derived preview stage scope is invalid")
    root = storage_root_path()
    stage = root / "hot" / "derived-preview-batch" / str(source_id) / job_id
    current = root
    for component in ("hot", "derived-preview-batch", str(source_id), job_id):
        current = current / component
        if current.is_symlink():
            raise ValueError("derived preview stage is invalid")
    try:
        resolved_stage = stage.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("derived preview stage is invalid") from exc
    if resolved_stage != stage or not is_under_storage_root(resolved_stage, root=root):
        raise ValueError("derived preview stage is outside storage root")
    return stage


def _remove_batch_stage(stage: Path, *, source_id: int, job_id: str) -> None:
    expected = _batch_stage_dir(source_id=source_id, job_id=job_id)
    if stage != expected:
        raise ValueError("derived preview cleanup target is invalid")
    if not (stage.exists() or stage.is_symlink()):
        return
    if stage.is_symlink() or not stage.is_dir():
        raise ValueError("derived preview cleanup target is invalid")
    try:
        shutil.rmtree(stage)
    except OSError as exc:
        logger.warning(
            "derived preview stage cleanup failed source_episode_id=%s job_id=%s error_type=%s",
            source_id,
            job_id,
            type(exc).__name__,
        )
        raise ValueError("derived preview stage cleanup failed") from None


def _child_output_path(stage: Path, *, child_id: int) -> Path:
    if _positive_int(child_id) is None:
        raise ValueError("derived preview child id is invalid")
    output_path = (stage / "outputs" / f"{child_id}.mp4").resolve()
    if not is_under_storage_root(output_path) or output_path.parent.parent != stage:
        raise ValueError("derived preview output path is invalid")
    return output_path


def _materialize_parent_preview(*, source: Episode, artifact: EpisodeArtifact, stage: Path) -> Path:
    if source.kind != "source" or artifact.episode_id != source.id:
        raise ValueError("derived preview parent artifact is invalid")
    storage_uri = artifact.storage_uri
    if not isinstance(storage_uri, str) or not is_public_storage_uri(storage_uri):
        raise ValueError("derived preview parent storage URI is invalid")
    if storage_uri.startswith("oss://"):
        bucket, key = parse_storage_uri(storage_uri)
        if bucket != oss_client.bucket_name("process") or not _is_process_preview_key(key):
            raise ValueError("derived preview parent storage URI is invalid")
        destination = (stage / "source").resolve()
        if not is_under_storage_root(destination) or destination.parent != stage:
            raise ValueError("derived preview parent stage is invalid")
        oss_client.download_to(destination, bucket, key)
        source_path = destination / PurePosixPath(key).name
    elif storage_uri.startswith("nas://"):
        key = storage_uri.removeprefix("nas://")
        if not _is_process_preview_key(key):
            raise ValueError("derived preview parent storage URI is invalid")
        root = storage_root_path()
        unresolved = root / key
        if _has_symlinked_storage_component(unresolved, root=root):
            raise ValueError("derived preview parent storage URI is invalid")
        source_path = resolve_storage_path(storage_uri)
        if source_path is None:
            raise ValueError("derived preview parent storage URI is invalid")
    else:
        raise ValueError("derived preview parent storage URI is invalid")
    return Path(source_path)


def _is_process_preview_key(key: object) -> bool:
    return (
        isinstance(key, str)
        and key.startswith(_PROCESS_PREVIEW_PREFIXES)
        and cloud_path_is_safe("process", key)
    )


def _has_symlinked_storage_component(path: Path, *, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            return True
    return False


def _verify_parent_preview_file(path: Path, *, artifact: EpisodeArtifact) -> None:
    candidate = Path(path)
    checksum = artifact.checksum_sha256
    if (
        candidate.is_symlink()
        or not candidate.is_file()
        or not is_under_storage_root(candidate)
        or type(artifact.size_bytes) is not int
        or artifact.size_bytes <= 0
        or not isinstance(checksum, str)
        or _SHA256_RE.fullmatch(checksum) is None
        or candidate.stat().st_size != artifact.size_bytes
        or _file_sha256(candidate) != checksum
    ):
        raise ValueError("derived preview parent file is invalid")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _encoded_facts_by_episode(
    *,
    requests: tuple[_ChildPreviewRequest, ...],
    facts: object,
    stage: Path,
) -> dict[int, PreviewSegmentFacts]:
    if not isinstance(facts, tuple):
        return {}
    requests_by_id = {request.episode.id: request for request in requests}
    accepted: dict[int, PreviewSegmentFacts] = {}
    for facts_item in facts:
        if not isinstance(facts_item, PreviewSegmentFacts) or facts_item.episode_id in accepted:
            continue
        request = requests_by_id.get(facts_item.episode_id)
        if request is None or facts_item.frame_timestamps_ns != request.frame_timestamps_ns:
            continue
        try:
            output_path = facts_item.path
            if (
                output_path.is_symlink()
                or output_path.resolve() != request.output_path
                or output_path.parent.parent != stage
                or not output_path.is_file()
                or output_path.stat().st_size <= 0
                or not is_under_storage_root(output_path)
            ):
                continue
        except OSError:
            continue
        accepted[facts_item.episode_id] = facts_item
    return accepted


def _publish_child_preview(
    db: Session,
    *,
    job: JobRun,
    parent: _ParentPreviewFacts,
    request: _ChildPreviewRequest,
    facts: PreviewSegmentFacts,
) -> None:
    child = request.episode
    checksum = _file_sha256(facts.path)
    size_bytes = facts.path.stat().st_size
    if size_bytes <= 0:
        raise ValueError("derived preview output is empty")
    prefix = process_episode_run_prefix(
        workspace_id=child.workspace_id,
        task_set_id=child.task_set_id,
        batch_id=child.batch_id,
        episode_id=child.id,
        job_id=job.id,
    )
    key = f"{prefix}/preview.mp4"
    storage_uri = (
        f"oss://{oss_client.bucket_name('process')}/{key}"
        if uses_cloud_uri_authority()
        else f"nas://{key}"
    )
    artifact = db.scalar(
        select(EpisodeArtifact).where(EpisodeArtifact.storage_uri == storage_uri).with_for_update()
    )
    metadata = _child_preview_metadata(job=job, parent=parent, request=request)
    if artifact is None:
        artifact = EpisodeArtifact(
            episode_id=child.id,
            artifact_type="process_preview",
            storage_role="process",
            storage_uri=storage_uri,
            checksum_sha256=checksum,
            size_bytes=size_bytes,
            manifest_hash=checksum,
            retention_policy="permanent",
            metadata_json=metadata,
        )
        db.add(artifact)
        db.flush()
    else:
        if (
            artifact.episode_id != child.id
            or artifact.artifact_type != "process_preview"
            or artifact.storage_role != "process"
        ):
            raise ValueError("derived preview target artifact is invalid")
        artifact.checksum_sha256 = checksum
        artifact.size_bytes = size_bytes
        artifact.manifest_hash = checksum
        artifact.metadata_json = metadata
    operation = ensure_artifact_operation(
        db,
        artifact=artifact,
        job=job,
        operation_kind="process_preview_publish",
        source_path=facts.path,
    )
    if operation.status == "published":
        raise ValueError("derived preview published operation is invalid")
    db.commit()
    materialize_artifact_operation(db, operation_id=operation.id, source_path=facts.path)
    complete_artifact_operation(db, operation_id=operation.id)
    _record_child_preview_metrics(child, parent=parent, timestamps=facts.frame_timestamps_ns)
    db.commit()


def _child_preview_metadata(
    *,
    job: JobRun,
    parent: _ParentPreviewFacts,
    request: _ChildPreviewRequest,
) -> dict[str, object]:
    return {
        "media_type": "video/mp4",
        "generated_by_job_id": job.id,
        "reference_topic": parent.reference_topic,
        "frame_count": len(request.frame_timestamps_ns),
        "encoded_fps": parent.encoded_fps,
        "frame_timestamps_ns": [str(value) for value in request.frame_timestamps_ns],
    }


def _record_child_preview_metrics(
    child: Episode,
    *,
    parent: _ParentPreviewFacts,
    timestamps: tuple[int, ...],
) -> None:
    if (
        not timestamps
        or type(child.source_start_ns) is not int
        or type(child.source_end_ns) is not int
    ):
        raise ValueError("derived preview child metrics are invalid")
    duration_seconds = (child.source_end_ns - child.source_start_ns) / 1_000_000_000
    if duration_seconds <= 0:
        raise ValueError("derived preview child metrics are invalid")
    metadata = dict(child.metadata_json or {}) if isinstance(child.metadata_json, dict) else {}
    metrics = (
        dict(metadata.get("metrics") or {}) if isinstance(metadata.get("metrics"), dict) else {}
    )
    metrics.update(
        {
            "reference_frame_count": len(timestamps),
            "duration_s": round(duration_seconds, 6),
            "average_rgb_rate_hz": round(len(timestamps) / duration_seconds, 6),
        }
    )
    metadata["metrics"] = metrics
    metadata["reference_topic"] = parent.reference_topic
    child.metadata_json = metadata


def _enqueue_batch_preview_events(db: Session, *, workspace_id: int) -> None:
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise ValueError("derived preview workspace is unavailable")
    enqueue_work_queue_invalidated(db, workspace=workspace)


def _preview_events_pending(job: JobRun) -> bool:
    detail = job.detail_json if isinstance(job.detail_json, dict) else {}
    return detail.get("preview_events_pending") is True


def _mark_preview_events_pending(db: Session, *, job_id: str) -> None:
    job = db.get(JobRun, job_id)
    if job is None:
        raise ValueError("derived preview batch job is unavailable")
    if _preview_events_pending(job):
        return
    detail = dict(job.detail_json or {}) if isinstance(job.detail_json, dict) else {}
    detail["preview_events_pending"] = True
    job.detail_json = detail
    db.commit()


def _persist_batch_progress_and_enqueue_events(
    db: Session,
    *,
    job_id: str,
    workspace_id: int,
    result: dict[str, int],
    preview_events_pending: bool | None,
) -> None:
    _persist_batch_progress(
        db,
        job_id=job_id,
        result=result,
        preview_events_pending=preview_events_pending,
    )
    if preview_events_pending is not True:
        return
    _enqueue_batch_preview_events(db, workspace_id=workspace_id)
    _persist_batch_progress(db, job_id=job_id, result=result, preview_events_pending=False)


def _persist_batch_progress(
    db: Session,
    *,
    job_id: str,
    result: dict[str, int],
    preview_events_pending: bool | None = None,
) -> None:
    job = db.get(JobRun, job_id)
    if job is None:
        raise ValueError("derived preview batch job is unavailable")
    existing_detail = job.detail_json if isinstance(job.detail_json, dict) else {}
    review_id = _positive_int(existing_detail.get("cut_review_work_item_id"))
    if review_id is None:
        raise ValueError("derived preview batch review is unavailable")
    detail: dict[str, object] = {
        "source_episode_id": result["source_episode_id"],
        "cut_review_work_item_id": review_id,
        "total_count": result["total_count"],
        "completed_count": result["completed_count"],
        "failed_count": result["failed_count"],
    }
    if preview_events_pending is None:
        if "preview_events_pending" in existing_detail:
            detail["preview_events_pending"] = existing_detail.get("preview_events_pending") is True
    else:
        detail["preview_events_pending"] = preview_events_pending
    job.detail_json = detail
    # The durable detail intentionally contains only stable IDs and bounded counts.
    db.commit()


def _log_child_failure(child_id: int, *, error_type: str) -> None:
    logger.warning(
        "derived preview child failed episode_id=%s error_type=%s",
        child_id,
        error_type[:96],
    )


def _latest_legacy_preview_jobs(db: Session, *, episode_ids: set[int]) -> dict[int, JobRun]:
    jobs = db.scalars(
        select(JobRun)
        .where(
            JobRun.kind == _LEGACY_PREVIEW_JOB_KIND,
            JobRun.resource_type == "episode",
            JobRun.resource_id.in_([str(episode_id) for episode_id in sorted(episode_ids)]),
        )
        .order_by(JobRun.created_at.desc(), JobRun.id.desc())
    ).all()
    latest: dict[int, JobRun] = {}
    for job in jobs:
        episode_id = _positive_int(job.resource_id)
        if episode_id in episode_ids:
            latest.setdefault(episode_id, job)
    return latest


def _latest_batch_jobs_for_scopes(
    db: Session,
    *,
    scopes: set[tuple[int, int, int, int]],
) -> dict[tuple[int, int, int, int], JobRun]:
    if not scopes:
        return {}
    source_ids = sorted(
        {source_id for source_id, _review_id, _workspace_id, _task_set_id in scopes}
    )
    review_ids = sorted(
        {review_id for _source_id, review_id, _workspace_id, _task_set_id in scopes}
    )
    jobs = db.scalars(
        select(JobRun)
        .where(
            JobRun.kind == DERIVED_PREVIEW_BATCH_JOB_KIND,
            JobRun.resource_type == "episode",
            JobRun.resource_id.in_([str(source_id) for source_id in source_ids]),
            cast(JobRun.detail_json, JSONB)["cut_review_work_item_id"].astext.in_(
                [str(review_id) for review_id in review_ids]
            ),
            cast(JobRun.detail_json, JSONB)["source_episode_id"].astext.in_(
                [str(source_id) for source_id in source_ids]
            ),
        )
        .order_by(JobRun.created_at.desc(), JobRun.id.desc())
    ).all()
    latest: dict[tuple[int, int, int, int], JobRun] = {}
    for job in jobs:
        scope = _batch_job_scope(job)
        if scope in scopes:
            latest.setdefault(scope, job)
    return latest


def _source_review_scope(source: Episode, review_item_id: object) -> tuple[int, int, int, int]:
    source_id = _positive_int(source.id)
    workspace_id = _positive_int(source.workspace_id)
    task_set_id = _positive_int(source.task_set_id)
    review_id = _positive_int(review_item_id)
    if source.kind != "source" or None in {source_id, workspace_id, task_set_id, review_id}:
        raise ValueError("derived preview batch source scope is invalid")
    return source_id, review_id, workspace_id, task_set_id


def _derived_review_scope(episode: Episode) -> tuple[int, int, int, int] | None:
    if episode.kind != "derived":
        return None
    source_id = _positive_int(episode.parent_episode_id)
    workspace_id = _positive_int(episode.workspace_id)
    task_set_id = _positive_int(episode.task_set_id)
    metadata = episode.metadata_json if isinstance(episode.metadata_json, dict) else {}
    lineage = metadata.get("lineage") if isinstance(metadata.get("lineage"), dict) else {}
    review_id = _positive_int(lineage.get("cut_review_work_item_id"))
    if None in {source_id, workspace_id, task_set_id, review_id}:
        return None
    return source_id, review_id, workspace_id, task_set_id


def _batch_job_scope(job: JobRun) -> tuple[int, int, int, int] | None:
    source_id = _positive_int(job.resource_id)
    workspace_id = _positive_int(job.workspace_id)
    task_set_id = _positive_int(job.task_set_id)
    detail = job.detail_json if isinstance(job.detail_json, dict) else {}
    detail_source_id = _positive_int(detail.get("source_episode_id"))
    review_id = _positive_int(detail.get("cut_review_work_item_id"))
    if None in {source_id, workspace_id, task_set_id, detail_source_id, review_id}:
        return None
    if source_id != detail_source_id:
        return None
    return source_id, review_id, workspace_id, task_set_id


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None
