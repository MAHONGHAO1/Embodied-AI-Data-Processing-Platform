"""Parse Batch import originals into source Episodes and quality jobs."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import stat
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

from mcap.reader import NonSeekingReader
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from data.config import settings
from data.database import (
    ArtifactOperation,
    Batch,
    Episode,
    EpisodeArtifact,
    EpisodeCollectorAttribution,
    EpisodeDeviceAttribution,
    ImportCandidate,
    ImportSession,
    JobRun,
    TaskLabel,
    TaskSetSourceImport,
    WorkItem,
    Workspace,
)
from data.infra import oss_client
from data.realtime.outbox import enqueue_resource_event, enqueue_work_queue_invalidated
from data.realtime.projections import (
    batch_snapshot,
    episode_snapshot,
    import_session_snapshot,
    job_run_snapshot,
    work_item_snapshot,
)
from data.services.artifact_operations import (
    complete_artifact_operation,
    ensure_artifact_operation,
    materialize_artifact_operation,
)
from data.services.batch_paths import process_episode_run_prefix, raw_episode_source_prefix
from data.services.capture_batch_import import (
    CAPTURE_EPISODE_OSS_CANDIDATE_TYPE,
    CaptureImportSource,
    copy_capture_source_object,
    load_capture_episode_import_source,
)
from data.services.capture_provenance import CaptureProvenanceProjection, project_capture_provenance
from data.services.duance_imports import load_duance_import_manifest
from data.services.episode_cut_suggestions import (
    MAX_CUT_SUGGESTIONS,
    RawCutSuggestion,
    decode_qr_segment_event,
    project_qr_segment_events,
)
from data.services.historical_ego_import import (
    LegacyEgoImportSource,
    copy_legacy_ego_source_object,
    load_ego_episode_import_source,
)
from data.services.import_intake import (
    SOURCE_EPISODE_CANDIDATE_TYPES,
    import_original_storage_uri,
    import_staging_dir,
)
from data.services.storage_mode import uses_cloud_uri_authority
from data.utils.storage_paths import is_under_storage_root, storage_root_path

IMPORT_PARSE_JOB_KIND = "import_parse"
IMPORT_PARSE_QUEUE = "ingest"
EPISODE_QUALITY_JOB_KIND = "episode_quality"
EPISODE_QUALITY_QUEUE = "media"
EPISODE_PREVIEW_JOB_KIND = "episode_preview"
EPISODE_PREVIEW_QUEUE = "media"
_MAX_IMPORT_EPISODES = 500
_MAX_DERIVED_PREVIEW_TIMELINE_FRAMES = 20_000
_MAX_SOURCE_PREVIEW_TIMELINE_FRAMES = 100_000
_REFERENCE_TIMELINE_EDGE_TOLERANCE_NS = 2_000_000_000
logger = logging.getLogger("quicdata.import_parser")

_SOURCE_IMPORT_OBJECT_NAMES = frozenset({"data", "metadata", "complete"})
_SAFE_PROVIDER_ERROR_TOKEN = re.compile(r"[A-Za-z0-9._:-]{1,64}\Z")


def _safe_provider_error_token(value: object) -> str | None:
    """Keep provider diagnostics useful without persisting paths or messages."""
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized if _SAFE_PROVIDER_ERROR_TOKEN.fullmatch(normalized) else None


def _source_import_failure_context(
    *,
    stage: str,
    object_name: str | None,
    error: Exception,
) -> dict[str, object]:
    """Build a display-safe failure record for one source-package attempt.

    Provider exception text can contain source and destination keys, so it is
    intentionally logged only as a bounded type/status/code projection. The
    durable record carries the operation stage and logical object role instead.
    """
    if stage == "raw_copy" and object_name in _SOURCE_IMPORT_OBJECT_NAMES:
        error_code = f"raw_copy_{object_name}_failed"
        error_message = f"source {object_name} copy failed"
        failed_object = object_name
    elif stage == "source_validation":
        error_code = "source_validation_failed"
        error_message = "source validation failed"
        failed_object = None
    elif stage == "raw_publish":
        error_code = "raw_publish_failed"
        error_message = "raw source publication failed"
        failed_object = None
    elif stage == "quality_queue":
        error_code = "quality_job_failed"
        error_message = "quality job creation failed"
        failed_object = None
    else:
        error_code = "import_parse_failed"
        error_message = "source import failed"
        failed_object = None

    context: dict[str, object] = {
        "error_code": error_code,
        "error_message": error_message,
        "failure_stage": stage,
        "failed_object": failed_object,
        "failure_error_type": _safe_provider_error_token(type(error).__name__) or "Exception",
    }
    for result_key, attr in (
        ("failure_provider_status", "status"),
        ("failure_provider_code", "code"),
    ):
        token = _safe_provider_error_token(getattr(error, attr, None))
        if token is not None:
            context[result_key] = token
    return context


def export_episode_preview(*args, **kwargs):
    """Load the optional QRDF encoder only inside the preview worker."""
    from data.integrations.qrdf.preview_export import export_episode_preview as exporter

    return exporter(*args, **kwargs)


def export_episode_preview_facts(*args, **kwargs):
    """Load the bounded QRDF preview encoder only inside a media worker."""
    from data.integrations.qrdf.preview_export import export_episode_preview_facts as exporter

    return exporter(*args, **kwargs)


def inspect_episode_preview_timeline(*args, **kwargs):
    """Load the QRDF timeline inspector only inside a media worker."""
    from data.integrations.qrdf.preview_export import inspect_episode_preview_timeline as inspector

    return inspector(*args, **kwargs)


@dataclass(frozen=True)
class ParsedSource:
    external_episode_id: str
    source_dir: Path
    metadata: dict[str, object]


@dataclass(frozen=True)
class EpisodeQualityFacts:
    """Validated facts a workbench may safely read after quality succeeds."""

    metrics: dict[str, int | float | None]
    timing: dict[str, str | float]
    reference_topic: str | None
    cut_suggestions: dict[str, object]


@dataclass(frozen=True)
class PreviewTimelineBackfillPage:
    scanned_count: int
    jobs: tuple[JobRun, ...]
    next_after_episode_id: int | None


@dataclass(frozen=True)
class QualityReferenceBackfillPage:
    """One bounded page of source Episodes queued for reference coverage rechecks."""

    scanned_count: int
    jobs: tuple[JobRun, ...]
    next_after_episode_id: int | None


def parse_import_session_original(db: Session, job: JobRun) -> dict[str, object]:
    """Materialize one completed ImportSession original into source Episodes."""
    if job.kind != IMPORT_PARSE_JOB_KIND or job.resource_type != "import_session":
        raise ValueError("import parse job resource is invalid")
    import_session = db.scalar(
        select(ImportSession).where(ImportSession.id == job.resource_id).with_for_update()
    )
    if import_session is None:
        raise ValueError("import session does not exist")
    batch = db.get(Batch, import_session.batch_id)
    if batch is None:
        raise ValueError("import session batch is unavailable")
    detail = job.detail_json if isinstance(job.detail_json, dict) else {}
    if detail.get("candidate_type") == CAPTURE_EPISODE_OSS_CANDIDATE_TYPE:
        return _parse_capture_episode_candidate(
            db, job=job, import_session=import_session, batch=batch
        )
    if detail.get("candidate_type") == "ego_episode_oss":
        return _parse_ego_episode_candidate(db, job=job, import_session=import_session, batch=batch)
    if "candidate_id" in detail or "candidate_type" in detail:
        raise ValueError("import parse candidate declaration is unsupported")
    artifact_id = _positive_int((job.detail_json or {}).get("artifact_id"), "artifact_id")
    original = db.get(EpisodeArtifact, artifact_id)
    if (
        original is None
        or original.import_session_id != import_session.id
        or original.artifact_type != "import_original"
    ):
        raise ValueError("import original artifact is unavailable")

    stage = _parse_stage_dir(import_session.id, job.id)
    try:
        original_path = _materialize_original(original, import_session=import_session, stage=stage)
        import_session.status = "parsing"
        batch.status = "processing"
        enqueue_resource_event(
            db,
            resource=import_session,
            resource_type="import_session",
            event_name="import_session.updated",
            resource_snapshot=import_session_snapshot(import_session),
        )
        enqueue_resource_event(
            db,
            resource=batch,
            resource_type="batch",
            event_name="batch.updated",
            resource_snapshot=batch_snapshot(batch),
        )
        db.flush()

        if import_session.import_type == "duance_episode":
            sources = [
                _stage_duance_episode(
                    original_path,
                    import_session=import_session,
                    stage=stage / "duance-source",
                )
            ]
        else:
            sources = _discover_sources(
                original_path, original_name=import_session.original_name, stage=stage
            )
        collector_profile_id, collection_device_id = _import_attribution_ids(
            job, import_session=import_session
        )
        episode_ids: list[int] = []
        reused_episode_ids: list[int] = []
        quality_job_ids: list[str] = []
        for index, source in enumerate(sources, start=1):
            source_fingerprint = _tree_sha256(source.source_dir)
            episode, raw_artifact, source_created = _create_source_episode(
                db,
                batch=batch,
                import_session=import_session,
                import_artifact=original,
                source=source,
                index=index,
                source_fingerprint=source_fingerprint,
                collector_profile_id=collector_profile_id,
                collection_device_id=collection_device_id,
            )
            _publish_source_directory(
                db, job=job, source_dir=source.source_dir, artifact=raw_artifact
            )
            quality_job = _get_or_create_quality_job(db, episode=episode, actor_id=job.actor_id)
            if episode.id not in episode_ids:
                episode_ids.append(episode.id)
                quality_job_ids.append(quality_job.id)
                if not source_created:
                    reused_episode_ids.append(episode.id)
            enqueue_resource_event(
                db,
                resource=episode,
                resource_type="episode",
                event_name="episode.updated",
                resource_snapshot=episode_snapshot(episode),
            )
            enqueue_resource_event(
                db,
                resource=quality_job,
                resource_type="job_run",
                event_name="job_run.updated",
                resource_snapshot=job_run_snapshot(quality_job),
            )

        import_session.status = "succeeded"
        success_result: dict[str, object] = {
            "episode_ids": episode_ids,
            "episode_count": len(episode_ids),
            "reused_episode_ids": reused_episode_ids,
            "quality_job_ids": quality_job_ids,
        }
        if import_session.import_type == "duance_episode":
            success_result["source_key"] = import_session.source_fingerprint
        import_session.result_json = success_result
        batch.status = "ready"
        enqueue_resource_event(
            db,
            resource=import_session,
            resource_type="import_session",
            event_name="import_session.updated",
            resource_snapshot=import_session_snapshot(import_session),
        )
        enqueue_resource_event(
            db,
            resource=batch,
            resource_type="batch",
            event_name="batch.updated",
            resource_snapshot=batch_snapshot(batch),
        )
        db.commit()
        return {
            "import_session_id": import_session.id,
            "episode_ids": episode_ids,
            "reused_episode_ids": reused_episode_ids,
            "_follow_up_job_ids": quality_job_ids,
        }
    except Exception as exc:
        db.rollback()
        failed = db.scalar(
            select(ImportSession).where(ImportSession.id == import_session.id).with_for_update()
        )
        failed_batch = db.get(Batch, batch.id)
        if failed is not None:
            failed.status = "failed"
            failure_result = {
                "error_code": "import_parse_failed",
                # Raw Episode objects are immutable. Only imports/<session>
                # staging is eligible for automatic recursive cleanup.
                "raw_cleanup": "not_attempted",
            }
            if failed.import_type == "duance_episode":
                prior_result = failed.result_json if isinstance(failed.result_json, dict) else {}
                manifest = prior_result.get("duance_manifest")
                if isinstance(manifest, dict):
                    failure_result["duance_manifest"] = manifest
            failed.result_json = failure_result
            enqueue_resource_event(
                db,
                resource=failed,
                resource_type="import_session",
                event_name="import_session.updated",
                resource_snapshot=import_session_snapshot(failed),
            )
        if failed_batch is not None:
            failed_batch.status = "failed"
            enqueue_resource_event(
                db,
                resource=failed_batch,
                resource_type="batch",
                event_name="batch.updated",
                resource_snapshot=batch_snapshot(failed_batch),
            )
        db.commit()
        raise ValueError("import original could not be parsed") from exc
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _parse_capture_episode_candidate(
    db: Session,
    *,
    job: JobRun,
    import_session: ImportSession,
    batch: Batch,
) -> dict[str, object]:
    """Copy one generic QRDF capture source into canonical raw storage."""
    from data.infra import oss_client

    copied_targets: list[tuple[str, str]] = []
    candidate_id: str | None = None
    episode_id: int | None = None
    failure_stage = "candidate_reservation"
    failed_object: str | None = None
    try:
        candidate, ledger = _source_episode_candidate_for_job(
            db,
            job=job,
            import_session=import_session,
            batch=batch,
        )
        candidate_id = candidate.id
        if candidate.candidate_type != CAPTURE_EPISODE_OSS_CANDIDATE_TYPE:
            raise ValueError("capture source parse job declaration is invalid")
        if ledger.status == "failed":
            ledger.status = "importing"
            ledger.error_code = ""
            ledger.error_message = ""
        collector_profile_id, collection_device_id = _import_attribution_ids(
            job, import_session=import_session
        )
        failure_stage = "source_validation"
        source_bucket, source = load_capture_episode_import_source(batch=batch, candidate=candidate)
        raw_bucket = oss_client.bucket_name("raw")
        failure_stage = "raw_publish"
        import_session.status = "parsing"
        batch.status = "processing"
        enqueue_resource_event(
            db,
            resource=import_session,
            resource_type="import_session",
            event_name="import_session.updated",
            resource_snapshot=import_session_snapshot(import_session),
        )
        enqueue_resource_event(
            db,
            resource=batch,
            resource_type="batch",
            event_name="batch.updated",
            resource_snapshot=batch_snapshot(batch),
        )
        db.flush()

        episode, raw_artifact, _source_created = _create_capture_source_episode(
            db,
            batch=batch,
            import_session=import_session,
            source=source,
            index=1,
            collector_profile_id=collector_profile_id,
            collection_device_id=collection_device_id,
        )
        episode_id = episode.id
        raw_prefix = _object_key_from_storage_uri(raw_artifact.storage_uri)
        operation = ensure_artifact_operation(
            db,
            artifact=raw_artifact,
            job=job,
            operation_kind="raw_source_publish",
            checksum_sha256=source.source_fingerprint,
            size_bytes=sum(int(identity["size_bytes"]) for identity in source.objects.values()),
            manifest_json={
                "kind": "directory",
                "entries": [
                    {"path": file_name, "size": source.objects[name]["size_bytes"]}
                    for name, file_name in (
                        ("data", "data.mcap"),
                        ("metadata", "metadata.json"),
                        ("complete", "complete.json"),
                    )
                ],
                "keys": ["data.mcap", "metadata.json", "complete.json"],
            },
        )
        if operation.status != "published":
            db.commit()
            failure_stage = "raw_copy"
            for object_name, file_name in (
                ("data", "data.mcap"),
                ("metadata", "metadata.json"),
                ("complete", "complete.json"),
            ):
                failed_object = object_name
                target_key = f"{raw_prefix}/{file_name}"
                if copy_capture_source_object(
                    source_bucket=source_bucket,
                    source=source,
                    object_name=object_name,
                    target_key=target_key,
                ):
                    copied_targets.append((raw_bucket, target_key))
            failure_stage = "raw_publish"
            complete_artifact_operation(db, operation_id=operation.id)
        if (
            episode.workflow_status in {"importing", "import_failed"}
            or episode.quality_status == "not_started"
        ):
            episode.workflow_status = "quality_pending"
            episode.quality_status = "pending"
        failure_stage = "quality_queue"
        quality_job = _get_or_create_quality_job(db, episode=episode, actor_id=job.actor_id)
        ledger.status = "imported"
        ledger.source_episode_id = episode.id
        ledger.error_code = ""
        ledger.error_message = ""
        _refresh_source_import_aggregate(db, import_session=import_session, batch=batch)
        enqueue_resource_event(
            db,
            resource=episode,
            resource_type="episode",
            event_name="episode.updated",
            resource_snapshot=episode_snapshot(episode),
        )
        enqueue_resource_event(
            db,
            resource=quality_job,
            resource_type="job_run",
            event_name="job_run.updated",
            resource_snapshot=job_run_snapshot(quality_job),
        )
        enqueue_resource_event(
            db,
            resource=import_session,
            resource_type="import_session",
            event_name="import_session.updated",
            resource_snapshot=import_session_snapshot(import_session),
        )
        enqueue_resource_event(
            db,
            resource=batch,
            resource_type="batch",
            event_name="batch.updated",
            resource_snapshot=batch_snapshot(batch),
        )
        db.commit()
        return {
            "import_session_id": import_session.id,
            "episode_ids": [episode.id],
            "_follow_up_job_ids": [quality_job.id],
        }
    except Exception as exc:
        db.rollback()
        cleanup_failed = not _cleanup_source_package_targets(copied_targets)
        failure = _source_import_failure_context(
            stage=failure_stage, object_name=failed_object, error=exc
        )
        _mark_source_episode_import_failed(
            db,
            job=job,
            import_session_id=import_session.id,
            batch_id=batch.id,
            candidate_id=candidate_id,
            episode_id=episode_id,
            cleanup_failed=cleanup_failed,
            failure=failure,
        )
        db.commit()
        raise ValueError("capture source could not be parsed") from exc


def _parse_ego_episode_candidate(
    db: Session,
    *,
    job: JobRun,
    import_session: ImportSession,
    batch: Batch,
) -> dict[str, object]:
    """Copy one verified EGO source Episode into canonical Batch raw storage."""
    from data.infra import oss_client

    copied_targets: list[tuple[str, str]] = []
    candidate_id: str | None = None
    episode_id: int | None = None
    failure_stage = "candidate_reservation"
    failed_object: str | None = None
    try:
        candidate, ledger = _source_episode_candidate_for_job(
            db,
            job=job,
            import_session=import_session,
            batch=batch,
        )
        candidate_id = candidate.id
        if ledger.status == "failed":
            ledger.status = "importing"
            ledger.error_code = ""
            ledger.error_message = ""
        collector_profile_id, collection_device_id = _import_attribution_ids(
            job, import_session=import_session
        )
        failure_stage = "source_validation"
        source_bucket, source = load_ego_episode_import_source(batch=batch, candidate=candidate)
        raw_bucket = oss_client.bucket_name("raw")
        failure_stage = "raw_publish"

        import_session.status = "parsing"
        batch.status = "processing"
        enqueue_resource_event(
            db,
            resource=import_session,
            resource_type="import_session",
            event_name="import_session.updated",
            resource_snapshot=import_session_snapshot(import_session),
        )
        enqueue_resource_event(
            db,
            resource=batch,
            resource_type="batch",
            event_name="batch.updated",
            resource_snapshot=batch_snapshot(batch),
        )
        db.flush()

        episode, raw_artifact, _source_created = _create_legacy_ego_source_episode(
            db,
            batch=batch,
            import_session=import_session,
            source=source,
            index=1,
            collector_profile_id=collector_profile_id,
            collection_device_id=collection_device_id,
        )
        episode_id = episode.id
        raw_prefix = _object_key_from_storage_uri(raw_artifact.storage_uri)
        operation = ensure_artifact_operation(
            db,
            artifact=raw_artifact,
            job=job,
            operation_kind="raw_source_publish",
            checksum_sha256=source.source_fingerprint,
            size_bytes=sum(identity.size_bytes for identity in source.objects.values()),
            manifest_json={
                "kind": "directory",
                "entries": [
                    {"path": file_name, "size": source.objects[name].size_bytes}
                    for name, file_name in _LEGACY_EGO_RAW_FILES
                ],
                "keys": [file_name for _name, file_name in _LEGACY_EGO_RAW_FILES],
            },
        )
        if operation.status != "published":
            db.commit()
            failure_stage = "raw_copy"
            for object_name, file_name in _LEGACY_EGO_RAW_FILES:
                failed_object = object_name
                target_key = f"{raw_prefix}/{file_name}"
                if copy_legacy_ego_source_object(
                    source_bucket=source_bucket,
                    source=source,
                    object_name=object_name,
                    target_key=target_key,
                ):
                    copied_targets.append((raw_bucket, target_key))
            failure_stage = "raw_publish"
            complete_artifact_operation(db, operation_id=operation.id)
        if (
            episode.workflow_status in {"importing", "import_failed"}
            or episode.quality_status == "not_started"
        ):
            episode.workflow_status = "quality_pending"
            episode.quality_status = "pending"
        failure_stage = "quality_queue"
        quality_job = _get_or_create_quality_job(db, episode=episode, actor_id=job.actor_id)
        ledger.status = "imported"
        ledger.source_episode_id = episode.id
        ledger.error_code = ""
        ledger.error_message = ""
        _refresh_source_import_aggregate(db, import_session=import_session, batch=batch)
        enqueue_resource_event(
            db,
            resource=episode,
            resource_type="episode",
            event_name="episode.updated",
            resource_snapshot=episode_snapshot(episode),
        )
        enqueue_resource_event(
            db,
            resource=quality_job,
            resource_type="job_run",
            event_name="job_run.updated",
            resource_snapshot=job_run_snapshot(quality_job),
        )
        enqueue_resource_event(
            db,
            resource=import_session,
            resource_type="import_session",
            event_name="import_session.updated",
            resource_snapshot=import_session_snapshot(import_session),
        )
        enqueue_resource_event(
            db,
            resource=batch,
            resource_type="batch",
            event_name="batch.updated",
            resource_snapshot=batch_snapshot(batch),
        )
        db.commit()
        return {
            "import_session_id": import_session.id,
            "episode_ids": [episode.id],
            "_follow_up_job_ids": [quality_job.id],
        }
    except Exception as exc:
        db.rollback()
        cleanup_failed = not _cleanup_source_package_targets(copied_targets)
        failure = _source_import_failure_context(
            stage=failure_stage, object_name=failed_object, error=exc
        )
        logger.warning(
            "legacy EGO import failed session=%s candidate=%s job=%s code=%s stage=%s object=%s "
            "error_type=%s provider_status=%s provider_code=%s cleanup_failed=%s",
            import_session.id,
            candidate_id,
            job.id,
            failure["error_code"],
            failure["failure_stage"],
            failure["failed_object"],
            failure["failure_error_type"],
            failure.get("failure_provider_status"),
            failure.get("failure_provider_code"),
            cleanup_failed,
        )
        _mark_source_episode_import_failed(
            db,
            job=job,
            import_session_id=import_session.id,
            batch_id=batch.id,
            candidate_id=candidate_id,
            episode_id=episode_id,
            cleanup_failed=cleanup_failed,
            failure=failure,
        )
        db.commit()
        raise ValueError("EGO source could not be parsed") from exc


_LEGACY_EGO_RAW_FILES = (
    ("data", "data.mcap"),
    ("metadata", "metadata.json"),
    ("complete", "complete.json"),
)


def _source_episode_candidate_for_job(
    db: Session,
    *,
    job: JobRun,
    import_session: ImportSession,
    batch: Batch,
) -> tuple[ImportCandidate, TaskSetSourceImport]:
    detail = job.detail_json if isinstance(job.detail_json, dict) else {}
    candidate_id = detail.get("candidate_id")
    if (
        detail.get("candidate_type") not in SOURCE_EPISODE_CANDIDATE_TYPES
        or not isinstance(candidate_id, str)
        or not candidate_id
        or len(candidate_id) > 36
        or "artifact_id" in detail
    ):
        raise ValueError("source package parse job declaration is invalid")
    candidate = db.scalar(
        select(ImportCandidate)
        .where(
            ImportCandidate.id == candidate_id,
            ImportCandidate.import_session_id == import_session.id,
        )
        .with_for_update()
    )
    if (
        candidate is None
        or candidate.candidate_type not in SOURCE_EPISODE_CANDIDATE_TYPES
        or candidate.status != "consumed"
    ):
        raise ValueError("source package candidate is unavailable")
    if (
        not isinstance(candidate.task_set_source_import_id, int)
        or candidate.task_set_source_import_id <= 0
    ):
        raise ValueError("source package ledger is unavailable")
    ledger = db.scalar(
        select(TaskSetSourceImport)
        .where(
            TaskSetSourceImport.id == candidate.task_set_source_import_id,
            TaskSetSourceImport.task_set_id == batch.task_set_id,
        )
        .with_for_update()
    )
    if (
        ledger is None
        or ledger.import_session_id != import_session.id
        or ledger.batch_id != batch.id
        or ledger.status not in {"failed", "importing", "imported"}
    ):
        raise ValueError("source package ledger is unavailable")
    return candidate, ledger


def _refresh_source_import_aggregate(
    db: Session,
    *,
    import_session: ImportSession,
    batch: Batch,
) -> None:
    """Aggregate per-source ledger states into the parent import execution."""
    ledgers = list(
        db.scalars(
            select(TaskSetSourceImport)
            .where(TaskSetSourceImport.import_session_id == import_session.id)
            .with_for_update()
        )
    )
    if not ledgers:
        raise ValueError("source package import has no ledger rows")
    imported_ids = sorted(
        row.source_episode_id
        for row in ledgers
        if row.status == "imported" and isinstance(row.source_episode_id, int)
    )
    importing = any(row.status == "importing" for row in ledgers)
    failed = any(row.status == "failed" for row in ledgers)
    if importing:
        import_session.status = "parsing"
        batch.status = "processing"
    elif failed:
        import_session.status = "failed"
        batch.status = "failed"
    else:
        import_session.status = "succeeded"
        batch.status = "ready"
    import_session.result_json = {
        **dict(import_session.result_json or {}),
        "episode_ids": imported_ids,
        "episode_count": len(imported_ids),
        "imported_source_count": len(imported_ids),
        "failed_source_count": sum(row.status == "failed" for row in ledgers),
    }
    if not failed:
        result = dict(import_session.result_json or {})
        for key in (
            "error_code",
            "error_message",
            "failure_stage",
            "failed_object",
            "failure_error_type",
            "failure_provider_status",
            "failure_provider_code",
            "cleanup_failed",
        ):
            result.pop(key, None)
        import_session.result_json = result


def _mark_source_episode_import_failed(
    db: Session,
    *,
    job: JobRun,
    import_session_id: str,
    batch_id: int,
    candidate_id: str | None,
    episode_id: int | None,
    cleanup_failed: bool,
    failure: dict[str, object] | None = None,
) -> None:
    """Persist an observable source-level failure after rollback/cleanup."""
    detail = job.detail_json if isinstance(job.detail_json, dict) else {}
    failure_context = dict(failure or {})
    if not isinstance(failure_context.get("error_code"), str):
        failure_context = _source_import_failure_context(
            stage="candidate_reservation",
            object_name=None,
            error=ValueError("unknown source package import failure"),
        )
    failure_code = str(failure_context["error_code"])
    failure_message = str(failure_context["error_message"])
    safe_candidate_id = candidate_id or detail.get("candidate_id")
    candidate = None
    if isinstance(safe_candidate_id, str) and safe_candidate_id:
        candidate = db.scalar(
            select(ImportCandidate)
            .where(
                ImportCandidate.id == safe_candidate_id,
                ImportCandidate.import_session_id == import_session_id,
            )
            .with_for_update()
        )
    ledger = None
    if candidate is not None and isinstance(candidate.task_set_source_import_id, int):
        ledger = db.scalar(
            select(TaskSetSourceImport)
            .where(TaskSetSourceImport.id == candidate.task_set_source_import_id)
            .with_for_update()
        )
    if ledger is not None and ledger.status != "imported":
        ledger.status = "failed"
        ledger.error_code = failure_code
        ledger.error_message = failure_message

    episode = None
    if isinstance(episode_id, int) and episode_id > 0:
        episode = db.scalar(select(Episode).where(Episode.id == episode_id).with_for_update())
    if episode is not None and (ledger is None or ledger.status != "imported"):
        episode.workflow_status = "import_failed"
        episode.quality_status = "not_started"

    import_session = db.scalar(
        select(ImportSession).where(ImportSession.id == import_session_id).with_for_update()
    )
    batch = db.get(Batch, batch_id)
    if import_session is None or batch is None:
        return
    if ledger is not None:
        _refresh_source_import_aggregate(db, import_session=import_session, batch=batch)
        import_session.result_json = {
            **dict(import_session.result_json or {}),
            **failure_context,
            "cleanup_failed": cleanup_failed,
        }
    else:
        import_session.status = "failed"
        import_session.result_json = {
            **failure_context,
            "cleanup_failed": cleanup_failed,
        }
        batch.status = "failed"
    enqueue_resource_event(
        db,
        resource=import_session,
        resource_type="import_session",
        event_name="import_session.updated",
        resource_snapshot=import_session_snapshot(import_session),
    )
    enqueue_resource_event(
        db,
        resource=batch,
        resource_type="batch",
        event_name="batch.updated",
        resource_snapshot=batch_snapshot(batch),
    )
    if episode is not None:
        enqueue_resource_event(
            db,
            resource=episode,
            resource_type="episode",
            event_name="episode.updated",
            resource_snapshot=episode_snapshot(episode),
        )


def _create_capture_source_episode(
    db: Session,
    *,
    batch: Batch,
    import_session: ImportSession,
    source: CaptureImportSource,
    index: int,
    collector_profile_id: int | None,
    collection_device_id: int | None,
) -> tuple[Episode, EpisodeArtifact, bool]:
    episode_uid = _episode_uid(import_session.id, source.episode_id, index)
    existing = db.scalar(select(Episode).where(Episode.episode_uid == episode_uid))
    if existing is not None:
        raw_artifact = (
            db.query(EpisodeArtifact)
            .filter(
                EpisodeArtifact.episode_id == existing.id,
                EpisodeArtifact.artifact_type == "raw_source",
            )
            .one()
        )
        return existing, raw_artifact, False
    provenance = project_capture_provenance(
        db,
        workspace_id=batch.workspace_id,
        raw_metadata=source.provenance_metadata
        if source.provenance_metadata is not None
        else source.metadata,
    )
    episode = Episode(
        episode_uid=episode_uid,
        workspace_id=batch.workspace_id,
        task_set_id=batch.task_set_id,
        batch_id=batch.id,
        import_session_id=import_session.id,
        kind="source",
        modality=_modality_for_batch(batch),
        source_fingerprint=source.source_fingerprint,
        workflow_status="importing",
        quality_status="not_started",
        task_label_id=import_session.task_label_id,
        task_language=_import_task_language(db, import_session),
        reported_source_group_key=provenance.source_group_key,
        reported_source_group_name=provenance.source_group_name,
        reported_source_group_status=provenance.source_group_status,
        metadata_json={
            "import": {
                "import_session_id": import_session.id,
                "source_protocol": "quicdata.capture.upload-complete.v1",
                "source_episode_id": source.episode_id,
            },
            "source": _safe_source_metadata(source.metadata),
        },
    )
    db.add(episode)
    db.flush()
    _declare_machine_reported_attribution(
        db,
        episode=episode,
        import_session=import_session,
        provenance=provenance,
    )
    _declare_import_attribution(
        db,
        episode=episode,
        import_session=import_session,
        collector_profile_id=collector_profile_id,
        collection_device_id=collection_device_id,
        include_empty=False,
        include_collector=provenance.collector_hint_status == "missing",
    )
    raw_artifact = EpisodeArtifact(
        episode_id=episode.id,
        import_session_id=import_session.id,
        artifact_type="raw_source",
        storage_role="raw",
        storage_uri=f"nas://pending-source/{episode.id}/{uuid4().hex}",
        manifest_hash=source.source_fingerprint,
        size_bytes=sum(int(identity["size_bytes"]) for identity in source.objects.values()),
        retention_policy="permanent",
        metadata_json={
            "source_protocol": "quicdata.capture.upload-complete.v1",
            "source_episode_id": source.episode_id,
        },
    )
    db.add(raw_artifact)
    db.flush()
    prefix = raw_episode_source_prefix(
        workspace_id=batch.workspace_id,
        task_set_id=batch.task_set_id,
        batch_id=batch.id,
        episode_id=episode.id,
        artifact_id=raw_artifact.id,
    )
    raw_artifact.storage_uri = import_original_storage_uri(prefix)
    db.flush()
    return episode, raw_artifact, True


def _create_legacy_ego_source_episode(
    db: Session,
    *,
    batch: Batch,
    import_session: ImportSession,
    source: LegacyEgoImportSource,
    index: int,
    collector_profile_id: int | None,
    collection_device_id: int | None,
) -> tuple[Episode, EpisodeArtifact, bool]:
    external_episode_id = f"{source.legacy_device_id}:{source.legacy_episode_id}"
    episode_uid = _episode_uid(import_session.id, external_episode_id, index)
    existing = db.scalar(select(Episode).where(Episode.episode_uid == episode_uid))
    if existing is not None:
        if existing.workflow_status in {"importing", "import_failed"}:
            existing.workflow_status = "importing"
            existing.quality_status = "not_started"
        raw_artifact = (
            db.query(EpisodeArtifact)
            .filter(
                EpisodeArtifact.episode_id == existing.id,
                EpisodeArtifact.artifact_type == "raw_source",
            )
            .one()
        )
        return existing, raw_artifact, False
    provenance = project_capture_provenance(
        db,
        workspace_id=batch.workspace_id,
        raw_metadata={"collection_task_id": source.legacy_task_id},
    )
    episode = Episode(
        episode_uid=episode_uid,
        workspace_id=batch.workspace_id,
        task_set_id=batch.task_set_id,
        batch_id=batch.id,
        import_session_id=import_session.id,
        kind="source",
        modality=_modality_for_batch(batch),
        source_fingerprint=source.source_fingerprint,
        workflow_status="importing",
        quality_status="not_started",
        task_label_id=import_session.task_label_id,
        task_language=_import_task_language(db, import_session),
        reported_source_group_key=provenance.source_group_key,
        reported_source_group_name=provenance.source_group_name,
        reported_source_group_status=provenance.source_group_status,
        metadata_json={
            "import": {
                "import_session_id": import_session.id,
                "legacy_ego_task_id": source.legacy_task_id,
                "legacy_device_id": source.legacy_device_id,
                "external_episode_id": source.legacy_episode_id,
            },
            "source": _safe_source_metadata(source.metadata),
        },
    )
    db.add(episode)
    db.flush()
    _declare_import_attribution(
        db,
        episode=episode,
        import_session=import_session,
        collector_profile_id=collector_profile_id,
        collection_device_id=collection_device_id,
    )
    raw_artifact = EpisodeArtifact(
        episode_id=episode.id,
        import_session_id=import_session.id,
        artifact_type="raw_source",
        storage_role="raw",
        storage_uri=f"nas://pending-source/{episode.id}/{uuid4().hex}",
        manifest_hash=source.source_fingerprint,
        size_bytes=sum(identity.size_bytes for identity in source.objects.values()),
        retention_policy="permanent",
        metadata_json={
            "legacy_ego_task_id": source.legacy_task_id,
            "legacy_device_id": source.legacy_device_id,
            "external_episode_id": source.legacy_episode_id,
        },
    )
    db.add(raw_artifact)
    db.flush()
    prefix = raw_episode_source_prefix(
        workspace_id=batch.workspace_id,
        task_set_id=batch.task_set_id,
        batch_id=batch.id,
        episode_id=episode.id,
        artifact_id=raw_artifact.id,
    )
    raw_artifact.storage_uri = import_original_storage_uri(prefix)
    db.flush()
    return episode, raw_artifact, True


def _cleanup_source_package_targets(targets: list[tuple[str, str]]) -> bool:
    """Remove only individual raw objects this failed parse invocation created."""
    if not targets:
        return True
    from data.infra import oss_client

    succeeded = True
    for bucket, key in reversed(targets):
        try:
            if not key.startswith(("raw/v1/", "raw/v2/")):
                raise ValueError("source package cleanup target is invalid")
            oss_client.delete_object(bucket, key)
        except Exception as exc:
            succeeded = False
            logger.warning("source package raw cleanup failed error_type=%s", type(exc).__name__)
    return succeeded


def run_episode_quality(db: Session, job: JobRun) -> dict[str, object]:
    """Perform the first lightweight source quality check for a Batch Episode."""
    if job.kind != EPISODE_QUALITY_JOB_KIND or job.resource_type != "episode":
        raise ValueError("episode quality job resource is invalid")
    episode_id = _positive_int(job.resource_id, "episode_id")
    episode = db.scalar(select(Episode).where(Episode.id == episode_id).with_for_update())
    if episode is None:
        raise ValueError("episode does not exist")
    raw_artifact = (
        db.query(EpisodeArtifact)
        .filter(
            EpisodeArtifact.episode_id == episode.id, EpisodeArtifact.artifact_type == "raw_source"
        )
        .one_or_none()
    )
    if raw_artifact is None:
        raise ValueError("raw source artifact is unavailable")
    source_dir = _materialize_source_artifact(
        raw_artifact, episode=episode, stage=_quality_stage_dir(episode.id, job.id)
    )
    try:
        metadata = _read_metadata(source_dir / "metadata.json")
        data_file = _source_data_file(source_dir, metadata)
        quality_facts = _quality_metrics(metadata, data_file=data_file)
        current = dict(episode.metadata_json or {})
        current["metrics"] = quality_facts.metrics
        current["timing"] = quality_facts.timing
        current["multimodal"] = _multimodal_projection(
            metadata,
            reference_topic=quality_facts.reference_topic,
        )
        current["cut_suggestions"] = quality_facts.cut_suggestions
        if quality_facts.reference_topic is not None:
            current["reference_topic"] = quality_facts.reference_topic
        else:
            current.pop("reference_topic", None)
        current.setdefault("quality", {})["checked_by_job_id"] = job.id
        episode.metadata_json = current
        episode.quality_status = "passed"
        episode.workflow_status = "ready"
        cut_item = _get_or_create_cut_work_item(db, episode=episode, actor_id=job.actor_id)
        preview_job = None
        if quality_facts.reference_topic:
            preview_job = _get_or_create_preview_job(db, episode=episode, actor_id=job.actor_id)
        workspace = db.get(Workspace, episode.workspace_id)
        if workspace is None:
            raise ValueError("episode workspace is unavailable")
        enqueue_resource_event(
            db,
            resource=episode,
            resource_type="episode",
            event_name="episode.updated",
            resource_snapshot=episode_snapshot(episode),
        )
        enqueue_resource_event(
            db,
            resource=cut_item,
            resource_type="work_item",
            event_name="work_item.updated",
            resource_snapshot=work_item_snapshot(cut_item),
        )
        if preview_job is not None:
            enqueue_resource_event(
                db,
                resource=preview_job,
                resource_type="job_run",
                event_name="job_run.updated",
                resource_snapshot=job_run_snapshot(preview_job),
            )
        enqueue_work_queue_invalidated(db, workspace=workspace)
        db.commit()
        result: dict[str, object] = {
            "episode_id": episode.id,
            "quality_status": episode.quality_status,
            "metrics": quality_facts.metrics,
        }
        if preview_job is not None:
            result["_follow_up_job_ids"] = [preview_job.id]
        return result
    except Exception as exc:
        db.rollback()
        failed = db.scalar(select(Episode).where(Episode.id == episode.id).with_for_update())
        if failed is not None:
            failed.quality_status = "failed"
            failed.workflow_status = "failed"
            enqueue_resource_event(
                db,
                resource=failed,
                resource_type="episode",
                event_name="episode.updated",
                resource_snapshot=episode_snapshot(failed),
            )
            workspace = db.get(Workspace, failed.workspace_id)
            if workspace is not None:
                enqueue_work_queue_invalidated(db, workspace=workspace)
            db.commit()
        raise ValueError("episode quality check failed") from exc
    finally:
        shutil.rmtree(source_dir.parent, ignore_errors=True)


def _create_source_episode(
    db: Session,
    *,
    batch: Batch,
    import_session: ImportSession,
    import_artifact: EpisodeArtifact,
    source: ParsedSource,
    index: int,
    source_fingerprint: str,
    collector_profile_id: int | None,
    collection_device_id: int | None,
) -> tuple[Episode, EpisodeArtifact, bool]:
    _lock_source_fingerprint(
        db, task_set_id=batch.task_set_id, source_fingerprint=source_fingerprint
    )
    existing = db.scalar(
        select(Episode)
        .where(
            Episode.task_set_id == batch.task_set_id,
            Episode.kind == "source",
            Episode.source_fingerprint == source_fingerprint,
        )
        .with_for_update()
    )
    if existing is not None:
        return _reuse_source_episode(db, existing=existing, import_session=import_session)

    episode_uid = _episode_uid(import_session.id, source.external_episode_id, index)
    existing = db.scalar(
        select(Episode).where(Episode.episode_uid == episode_uid).with_for_update()
    )
    if existing is not None:
        # A retry of an import created before source directories had their own
        # identity can safely repair that record: the immutable original is
        # being parsed again under the same deterministic Episode UID.
        if existing.source_fingerprint != source_fingerprint:
            existing.source_fingerprint = source_fingerprint
            db.flush()
        return _reuse_source_episode(db, existing=existing, import_session=import_session)
    episode = Episode(
        episode_uid=episode_uid,
        workspace_id=batch.workspace_id,
        task_set_id=batch.task_set_id,
        batch_id=batch.id,
        import_session_id=import_session.id,
        kind="source",
        modality=_modality_for_batch(batch),
        source_fingerprint=source_fingerprint,
        workflow_status="discovered",
        quality_status="pending",
        task_label_id=import_session.task_label_id,
        task_language=_import_task_language(db, import_session),
        metadata_json={
            "import": {
                "import_session_id": import_session.id,
                "import_artifact_id": import_artifact.id,
                "external_episode_id": source.external_episode_id,
            },
            "source": _safe_source_metadata(source.metadata),
        },
    )
    db.add(episode)
    db.flush()
    _declare_import_attribution(
        db,
        episode=episode,
        import_session=import_session,
        collector_profile_id=collector_profile_id,
        collection_device_id=collection_device_id,
    )
    raw_artifact = EpisodeArtifact(
        episode_id=episode.id,
        import_session_id=import_session.id,
        artifact_type="raw_source",
        storage_role="raw",
        storage_uri=f"nas://pending-source/{episode.id}/{uuid4().hex}",
        checksum_sha256=source_fingerprint,
        size_bytes=_tree_size(source.source_dir),
        retention_policy="permanent",
        metadata_json={"external_episode_id": source.external_episode_id},
    )
    db.add(raw_artifact)
    db.flush()
    prefix = raw_episode_source_prefix(
        workspace_id=batch.workspace_id,
        task_set_id=batch.task_set_id,
        batch_id=batch.id,
        episode_id=episode.id,
        artifact_id=raw_artifact.id,
    )
    raw_artifact.storage_uri = import_original_storage_uri(prefix)
    db.flush()
    return episode, raw_artifact, True


def _declare_import_attribution(
    db: Session,
    *,
    episode: Episode,
    import_session: ImportSession,
    collector_profile_id: int | None,
    collection_device_id: int | None,
    include_empty: bool = True,
    include_collector: bool = True,
    include_device: bool = True,
) -> None:
    if episode.kind != "source":
        raise ValueError("import attribution requires a source episode")
    attributions: list[EpisodeCollectorAttribution | EpisodeDeviceAttribution] = []
    if include_collector and (include_empty or collector_profile_id is not None):
        attributions.append(
            EpisodeCollectorAttribution(
                root_source_episode_id=episode.id,
                collector_profile_id=collector_profile_id,
                source="offline_declared",
                created_by_user_id=import_session.owner_user_id,
                note=f"import session {import_session.id}",
            )
        )
    if include_device and (include_empty or collection_device_id is not None):
        attributions.append(
            EpisodeDeviceAttribution(
                root_source_episode_id=episode.id,
                collection_device_id=collection_device_id,
                source="offline_declared",
                created_by_user_id=import_session.owner_user_id,
                note=f"import session {import_session.id}",
            )
        )
    if attributions:
        db.add_all(attributions)
    episode.updated_at = datetime.utcnow()


def _declare_machine_reported_attribution(
    db: Session,
    *,
    episode: Episode,
    import_session: ImportSession,
    provenance: CaptureProvenanceProjection,
) -> None:
    """Append the producer report while preserving the captured identifier."""
    if episode.kind != "source":
        raise ValueError("machine attribution requires a source episode")
    db.add_all(
        (
            EpisodeCollectorAttribution(
                root_source_episode_id=episode.id,
                collector_profile_id=provenance.collector_profile_id,
                source="machine_reported",
                created_by_user_id=import_session.owner_user_id,
                reported_identifier=provenance.collector_reported_identifier,
                match_status=provenance.collector_match_status,
                note="capture metadata",
            ),
            EpisodeDeviceAttribution(
                root_source_episode_id=episode.id,
                collection_device_id=provenance.collection_device_id,
                source="machine_reported",
                created_by_user_id=import_session.owner_user_id,
                reported_identifier=provenance.device_serial,
                match_status=provenance.device_match_status,
                note="capture metadata",
            ),
        )
    )


def _import_attribution_ids(
    job: JobRun,
    *,
    import_session: ImportSession,
) -> tuple[int | None, int | None]:
    detail = job.detail_json if isinstance(job.detail_json, dict) else {}
    override = detail.get("attribution_override", {})
    if not isinstance(override, dict) or set(override) - {
        "collector_profile_id",
        "collection_device_id",
    }:
        raise ValueError("import attribution declaration is invalid")
    collector_profile_id = override.get(
        "collector_profile_id", import_session.default_collector_profile_id
    )
    collection_device_id = override.get(
        "collection_device_id", import_session.default_collection_device_id
    )
    for value in (collector_profile_id, collection_device_id):
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError("import attribution declaration is invalid")
    return collector_profile_id, collection_device_id


def _reuse_source_episode(
    db: Session,
    *,
    existing: Episode,
    import_session: ImportSession,
) -> tuple[Episode, EpisodeArtifact, bool]:
    if existing.task_label_id != import_session.task_label_id:
        raise ValueError("source episode task label conflicts with import session")
    raw_artifact = db.scalar(
        select(EpisodeArtifact)
        .where(
            EpisodeArtifact.episode_id == existing.id,
            EpisodeArtifact.artifact_type == "raw_source",
        )
        .with_for_update()
    )
    if raw_artifact is None:
        raise ValueError("existing source episode has no raw artifact")
    return existing, raw_artifact, False


def _lock_source_fingerprint(db: Session, *, task_set_id: int, source_fingerprint: str) -> None:
    """Serialize source creation for one TaskSet/content identity.

    PostgreSQL is the only supported runtime database.  The database unique
    index remains the final guard, while this transaction-scoped lock prevents
    concurrent worker retries from turning an idempotent re-import into a
    transient parse failure.
    """
    lock_material = f"episode-source:{task_set_id}:{source_fingerprint}".encode("ascii")
    lock_key = int.from_bytes(
        hashlib.sha256(lock_material).digest()[:8], byteorder="big", signed=True
    )
    db.execute(select(func.pg_advisory_xact_lock(lock_key)))


def _get_or_create_quality_job(db: Session, *, episode: Episode, actor_id: int | None) -> JobRun:
    key = _episode_quality_job_key(episode.id)
    existing = db.scalar(select(JobRun).where(JobRun.idempotency_key == key))
    if existing is not None:
        return existing
    job = JobRun(
        id=uuid4().hex,
        kind=EPISODE_QUALITY_JOB_KIND,
        resource_type="episode",
        resource_id=str(episode.id),
        workspace_id=episode.workspace_id,
        task_set_id=episode.task_set_id,
        idempotency_key=key,
        queue=EPISODE_QUALITY_QUEUE,
        actor_id=actor_id,
        status="queued",
        phase="queued",
        detail_json={"episode_id": episode.id},
    )
    db.add(job)
    db.flush()
    return job


def _episode_quality_job_key(episode_id: int) -> str:
    return f"episode-quality:{episode_id}:reference-coverage-v2"


def enqueue_episode_reference_coverage_backfill(
    db: Session,
    *,
    limit: int = 100,
    after_episode_id: int | None = None,
) -> QualityReferenceBackfillPage:
    """Queue a bounded page of previously-ready sources for the V2 reference coverage check."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("reference coverage backfill limit must be between 1 and 1000")
    if after_episode_id is not None and (type(after_episode_id) is not int or after_episode_id < 0):
        raise ValueError("reference coverage backfill cursor must be non-negative")
    query = select(Episode).where(
        Episode.kind == "source",
        Episode.quality_status.in_(("passed", "recovered", "profiled")),
    )
    if after_episode_id is not None:
        query = query.where(Episode.id > after_episode_id)
    rows = list(db.scalars(query.order_by(Episode.id.asc()).limit(limit + 1)))
    page = rows[:limit]
    jobs: list[JobRun] = []
    invalidated_workspaces: set[int] = set()
    for episode in page:
        raw_artifact = db.scalar(
            select(EpisodeArtifact.id).where(
                EpisodeArtifact.episode_id == episode.id,
                EpisodeArtifact.artifact_type == "raw_source",
            )
        )
        if (
            raw_artifact is None
            or db.scalar(
                select(JobRun.id).where(
                    JobRun.idempotency_key == _episode_quality_job_key(episode.id)
                )
            )
            is not None
        ):
            continue
        episode.quality_status = "pending"
        job = _get_or_create_quality_job(db, episode=episode, actor_id=None)
        jobs.append(job)
        enqueue_resource_event(
            db,
            resource=episode,
            resource_type="episode",
            event_name="episode.updated",
            resource_snapshot=episode_snapshot(episode),
        )
        enqueue_resource_event(
            db,
            resource=job,
            resource_type="job_run",
            event_name="job_run.updated",
            resource_snapshot=job_run_snapshot(job),
        )
        invalidated_workspaces.add(episode.workspace_id)
    for workspace_id in invalidated_workspaces:
        workspace = db.get(Workspace, workspace_id)
        if workspace is not None:
            enqueue_work_queue_invalidated(db, workspace=workspace)
    db.commit()
    return QualityReferenceBackfillPage(
        scanned_count=len(page),
        jobs=tuple(jobs),
        next_after_episode_id=page[-1].id if len(rows) > limit and page else None,
    )


def ensure_episode_preview_job(db: Session, *, episode: Episode, actor_id: int | None) -> JobRun:
    """Create one durable source or derived preview JobRun without dispatching it."""
    key = _episode_preview_job_key(episode.id)
    existing = db.scalar(select(JobRun).where(JobRun.idempotency_key == key))
    if existing is not None:
        return existing
    job = JobRun(
        id=uuid4().hex,
        kind=EPISODE_PREVIEW_JOB_KIND,
        resource_type="episode",
        resource_id=str(episode.id),
        workspace_id=episode.workspace_id,
        task_set_id=episode.task_set_id,
        idempotency_key=key,
        queue=EPISODE_PREVIEW_QUEUE,
        actor_id=actor_id,
        status="queued",
        phase="queued",
        detail_json={"episode_id": episode.id},
    )
    db.add(job)
    db.flush()
    return job


def _episode_preview_job_key(episode_id: int) -> str:
    return f"episode-preview:{episode_id}:v2-exact-timeline"


def enqueue_episode_preview_timeline_backfill(
    db: Session,
    *,
    limit: int = 100,
    after_episode_id: int | None = None,
) -> PreviewTimelineBackfillPage:
    """Queue one bounded page of quality-ready Episodes missing exact preview timelines."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("preview timeline backfill limit must be between 1 and 1000")
    if after_episode_id is not None and (type(after_episode_id) is not int or after_episode_id < 0):
        raise ValueError("preview timeline backfill cursor must be non-negative")
    query = select(Episode).where(Episode.quality_status.in_(("passed", "recovered", "profiled")))
    if after_episode_id is not None:
        query = query.where(Episode.id > after_episode_id)
    rows = list(db.scalars(query.order_by(Episode.id.asc()).limit(limit + 1)))
    page = rows[:limit]
    jobs: list[JobRun] = []
    for episode in page:
        artifact = db.scalar(
            select(EpisodeArtifact)
            .where(
                EpisodeArtifact.episode_id == episode.id,
                EpisodeArtifact.artifact_type == "process_preview",
            )
            .order_by(EpisodeArtifact.id.asc())
        )
        if artifact is not None and _artifact_has_exact_preview_timeline(artifact, episode=episode):
            continue
        try:
            _preview_source_and_artifact(db, episode=episode)
        except ValueError:
            continue
        key = _episode_preview_job_key(episode.id)
        if db.scalar(select(JobRun).where(JobRun.idempotency_key == key)) is not None:
            continue
        jobs.append(ensure_episode_preview_job(db, episode=episode, actor_id=None))
    db.commit()
    return PreviewTimelineBackfillPage(
        scanned_count=len(page),
        jobs=tuple(jobs),
        next_after_episode_id=page[-1].id if len(rows) > limit and page else None,
    )


def _get_or_create_preview_job(db: Session, *, episode: Episode, actor_id: int | None) -> JobRun:
    """Compatibility alias for the source-quality follow-up call site."""
    return ensure_episode_preview_job(db, episode=episode, actor_id=actor_id)


def run_episode_preview(db: Session, job: JobRun) -> dict[str, object]:
    """Generate one process preview without changing immutable raw storage."""
    if job.kind != EPISODE_PREVIEW_JOB_KIND or job.resource_type != "episode":
        raise ValueError("episode preview job resource is invalid")
    episode_id = _positive_int(job.resource_id, "episode_id")
    episode = db.scalar(select(Episode).where(Episode.id == episode_id).with_for_update())
    if episode is None:
        raise ValueError("episode does not exist")
    existing = db.scalar(
        select(EpisodeArtifact)
        .where(
            EpisodeArtifact.episode_id == episode.id,
            EpisodeArtifact.artifact_type == "process_preview",
        )
        .order_by(EpisodeArtifact.id.asc())
    )
    if existing is not None:
        existing_operation = db.scalar(
            select(ArtifactOperation).where(
                ArtifactOperation.artifact_id == existing.id,
                ArtifactOperation.operation_kind == "process_preview_publish",
            )
        )
        if existing_operation is not None and existing_operation.status == "published":
            if _artifact_has_exact_preview_timeline(existing, episode=episode):
                return {"episode_id": episode.id, "artifact_id": existing.id}
            return _backfill_existing_preview_timeline(
                db, job=job, episode=episode, artifact=existing
            )
    source_episode, raw_artifact = _preview_source_and_artifact(db, episode=episode)

    stage = _preview_stage_dir(episode.id, job.id)
    preview_path: Path | None = None
    try:
        source_dir = _materialize_source_artifact(raw_artifact, episode=source_episode, stage=stage)
        key = _process_preview_key(episode=episode, job=job)
        digest_path = stage / "preview.mp4"
        preview_path = digest_path
        if not is_under_storage_root(preview_path):
            raise ValueError("episode preview path is outside storage root")
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        if preview_path.exists():
            preview_path.unlink()
        start_ns, end_ns = _preview_source_interval(episode)
        facts = export_episode_preview_facts(
            source_dir,
            preview_path,
            start_ns=start_ns,
            end_ns=end_ns,
        )
        checksum = _file_sha256(preview_path)
        size_bytes = preview_path.stat().st_size
        if size_bytes <= 0:
            raise ValueError("episode preview is empty")

        storage_uri = (
            f"nas://{key}"
            if not uses_cloud_uri_authority()
            else f"oss://{oss_client.bucket_name('process')}/{key}"
        )
        artifact_metadata: dict[str, object] = {
            "media_type": "video/mp4",
            "generated_by_job_id": job.id,
            "reference_topic": facts.camera_topic,
            "frame_count": len(facts.frame_timestamps_ns),
            **_exact_preview_timeline_metadata(episode=episode, facts=facts),
        }
        artifact = existing
        if artifact is None:
            artifact = EpisodeArtifact(
                episode_id=episode.id,
                artifact_type="process_preview",
                storage_role="process",
                storage_uri=storage_uri,
                checksum_sha256=checksum,
                size_bytes=size_bytes,
                manifest_hash=checksum,
                retention_policy="permanent",
                metadata_json=artifact_metadata,
            )
            db.add(artifact)
            db.flush()
        else:
            artifact.metadata_json = artifact_metadata
        operation = ensure_artifact_operation(
            db,
            artifact=artifact,
            job=job,
            operation_kind="process_preview_publish",
            source_path=preview_path,
        )
        db.commit()
        materialize_artifact_operation(db, operation_id=operation.id, source_path=preview_path)
        complete_artifact_operation(db, operation_id=operation.id)
        if episode.kind == "derived":
            _record_derived_preview_metrics(episode, facts=facts)
        enqueue_resource_event(
            db,
            resource=episode,
            resource_type="episode",
            event_name="episode.updated",
            resource_snapshot=episode_snapshot(episode),
        )
        db.commit()
        return {
            "episode_id": episode.id,
            "artifact_id": artifact.id,
            "frame_count": len(facts.frame_timestamps_ns),
        }
    except Exception as exc:
        db.rollback()
        raise ValueError("episode preview generation failed") from exc
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _preview_timeline_limit(episode: Episode) -> int:
    return (
        _MAX_DERIVED_PREVIEW_TIMELINE_FRAMES
        if episode.kind == "derived"
        else _MAX_SOURCE_PREVIEW_TIMELINE_FRAMES
    )


def _exact_preview_timeline_metadata(*, episode: Episode, facts: object) -> dict[str, object]:
    timestamps = getattr(facts, "frame_timestamps_ns", ())
    encoded_fps = getattr(facts, "encoded_fps", None)
    if (
        not isinstance(timestamps, tuple)
        or not 1 <= len(timestamps) <= _preview_timeline_limit(episode)
        or not isinstance(encoded_fps, (int, float))
        or isinstance(encoded_fps, bool)
        or not math.isfinite(float(encoded_fps))
        or not 0 < float(encoded_fps) <= 240
    ):
        raise ValueError("episode preview timeline is unavailable")
    prior: int | None = None
    for timestamp in timestamps:
        if (
            type(timestamp) is not int
            or timestamp < 0
            or (prior is not None and timestamp <= prior)
        ):
            raise ValueError("episode preview timeline is unavailable")
        prior = timestamp
    return {
        "encoded_fps": float(encoded_fps),
        "frame_timestamps_ns": [str(item) for item in timestamps],
    }


def _artifact_has_exact_preview_timeline(artifact: EpisodeArtifact, *, episode: Episode) -> bool:
    metadata = artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
    raw = metadata.get("frame_timestamps_ns")
    encoded_fps = metadata.get("encoded_fps")
    frame_count = metadata.get("frame_count")
    return bool(
        isinstance(raw, list)
        and 1 <= len(raw) <= _preview_timeline_limit(episode)
        and frame_count == len(raw)
        and isinstance(encoded_fps, (int, float))
        and not isinstance(encoded_fps, bool)
        and math.isfinite(float(encoded_fps))
        and 0 < float(encoded_fps) <= 240
    )


def _backfill_existing_preview_timeline(
    db: Session,
    *,
    job: JobRun,
    episode: Episode,
    artifact: EpisodeArtifact,
) -> dict[str, object]:
    source_episode, raw_artifact = _preview_source_and_artifact(db, episode=episode)
    stage = _preview_stage_dir(episode.id, job.id)
    try:
        source_dir = _materialize_source_artifact(raw_artifact, episode=source_episode, stage=stage)
        start_ns, end_ns = _preview_source_interval(episode)
        metadata = (
            dict(artifact.metadata_json or {}) if isinstance(artifact.metadata_json, dict) else {}
        )
        facts = inspect_episode_preview_timeline(
            source_dir,
            camera_topic=str(metadata.get("reference_topic") or "") or None,
            start_ns=start_ns,
            end_ns=end_ns,
        )
        expected_count = metadata.get("frame_count")
        if (
            type(expected_count) is int
            and expected_count > 0
            and expected_count != len(facts.frame_timestamps_ns)
        ):
            raise ValueError("existing preview frame count does not match source timeline")
        metadata.update(
            {
                "generated_by_job_id": job.id,
                "reference_topic": facts.camera_topic,
                "frame_count": len(facts.frame_timestamps_ns),
                **_exact_preview_timeline_metadata(episode=episode, facts=facts),
            }
        )
        artifact.metadata_json = metadata
        enqueue_resource_event(
            db,
            resource=episode,
            resource_type="episode",
            event_name="episode.updated",
            resource_snapshot=episode_snapshot(episode),
        )
        db.commit()
        return {
            "episode_id": episode.id,
            "artifact_id": artifact.id,
            "frame_count": len(facts.frame_timestamps_ns),
            "timeline_backfilled": True,
        }
    except Exception as exc:
        db.rollback()
        raise ValueError("episode preview timeline backfill failed") from exc
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _preview_source_and_artifact(
    db: Session, *, episode: Episode
) -> tuple[Episode, EpisodeArtifact]:
    source = episode
    if episode.kind == "derived":
        if episode.parent_episode_id is None:
            raise ValueError("derived preview source is unavailable")
        source = db.scalar(
            select(Episode).where(Episode.id == episode.parent_episode_id).with_for_update()
        )
        if source is None or source.kind != "source":
            raise ValueError("derived preview source is unavailable")
    elif episode.kind != "source":
        raise ValueError("episode preview source is unavailable")
    if source.quality_status not in {"passed", "recovered", "profiled"}:
        raise ValueError("episode preview source is not quality ready")
    raw_artifact = (
        db.query(EpisodeArtifact)
        .filter(
            EpisodeArtifact.episode_id == source.id, EpisodeArtifact.artifact_type == "raw_source"
        )
        .one_or_none()
    )
    if raw_artifact is None:
        raise ValueError("raw source artifact is unavailable")
    return source, raw_artifact


def _preview_source_interval(episode: Episode) -> tuple[int | None, int | None]:
    if episode.kind == "source":
        return None, None
    start_ns = episode.source_start_ns
    end_ns = episode.source_end_ns
    if type(start_ns) is not int or type(end_ns) is not int or start_ns < 0 or start_ns >= end_ns:
        raise ValueError("derived preview interval is unavailable")
    return start_ns, end_ns


def _record_derived_preview_metrics(episode: Episode, *, facts: object) -> None:
    timestamps = getattr(facts, "frame_timestamps_ns", ())
    if not isinstance(timestamps, tuple) or not timestamps:
        raise ValueError("derived preview timeline is unavailable")
    start_ns, end_ns = _preview_source_interval(episode)
    if start_ns is None or end_ns is None:
        raise ValueError("derived preview interval is unavailable")
    metadata = dict(episode.metadata_json or {}) if isinstance(episode.metadata_json, dict) else {}
    metrics = (
        dict(metadata.get("metrics") or {}) if isinstance(metadata.get("metrics"), dict) else {}
    )
    metrics["reference_frame_count"] = len(timestamps)
    metrics["duration_s"] = round((end_ns - start_ns) / 1_000_000_000, 6)
    metrics["average_rgb_rate_hz"] = (
        round(len(timestamps) / metrics["duration_s"], 6) if metrics["duration_s"] else None
    )
    metadata["metrics"] = metrics
    topic = getattr(facts, "camera_topic", None)
    if isinstance(topic, str) and topic:
        metadata["reference_topic"] = topic
    episode.metadata_json = metadata


def _get_or_create_cut_work_item(
    db: Session, *, episode: Episode, actor_id: int | None
) -> WorkItem:
    existing = (
        db.query(WorkItem)
        .filter(WorkItem.episode_id == episode.id, WorkItem.kind == "cut", WorkItem.generation == 1)
        .one_or_none()
    )
    if existing is not None:
        return existing
    item = WorkItem(
        workspace_id=episode.workspace_id,
        episode_id=episode.id,
        kind="cut",
        status="pending",
        created_by_user_id=actor_id,
        generation=1,
    )
    db.add(item)
    db.flush()
    return item


def _discover_sources(
    original_path: Path, *, original_name: str, stage: Path
) -> list[ParsedSource]:
    suffix = Path(original_name or original_path.name).suffix.lower()
    if suffix == ".zip":
        return _extract_qrdf_zip(original_path, stage=stage / "extract")
    if suffix == ".mcap":
        return [
            _stage_single_mcap(
                original_path, stage=stage / "single_mcap", original_name=original_name
            )
        ]
    raise ValueError("unsupported import original format")


def _stage_duance_episode(
    original_path: Path,
    *,
    import_session: ImportSession,
    stage: Path,
) -> ParsedSource:
    """Build a QRDF source directory from the persisted direct-import declaration."""
    manifest = load_duance_import_manifest(import_session)
    try:
        original_size = original_path.stat().st_size
    except OSError as exc:
        raise ValueError("Duance MCAP original is unavailable") from exc
    if original_size != manifest.data_size_bytes:
        raise ValueError("Duance MCAP size does not match manifest")
    if _file_sha256(original_path) != manifest.data_sha256:
        raise ValueError("Duance MCAP checksum does not match manifest")

    metadata_bytes = manifest.metadata_text.encode("utf-8")
    metadata = json.loads(manifest.metadata_text)
    if not isinstance(metadata, dict):
        raise ValueError("Duance metadata is invalid")
    relative_data_path = PurePosixPath(manifest.data_path)
    if relative_data_path.is_absolute() or any(
        part in {"", ".", ".."} for part in relative_data_path.parts
    ):
        raise ValueError("Duance MCAP path is invalid")

    stage.mkdir(parents=True, exist_ok=True)
    stage_root = stage.resolve()
    data_path = stage.joinpath(*relative_data_path.parts)
    try:
        data_path.resolve().relative_to(stage_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("Duance MCAP path escapes staging") from exc
    data_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = stage / "metadata.json"
    with metadata_path.open("xb") as destination:
        destination.write(metadata_bytes)
        destination.flush()
        os.fsync(destination.fileno())
    shutil.copyfile(original_path, data_path)
    if (
        data_path.stat().st_size != manifest.data_size_bytes
        or _file_sha256(data_path) != manifest.data_sha256
    ):
        raise ValueError("Duance staged MCAP does not match manifest")
    return ParsedSource(manifest.episode_id, stage, metadata)


def _extract_qrdf_zip(archive_path: Path, *, stage: Path) -> list[ParsedSource]:
    stage.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive_path, "r") as opened:
            members = opened.infolist()
            _validate_zip_members(members)
            for member in members:
                if member.is_dir():
                    continue
                target = _archive_member_target(stage, member.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise ValueError("archive contains duplicate member names")
                with opened.open(member, "r") as source, target.open("xb") as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
                    destination.flush()
                    os.fsync(destination.fileno())
    except zipfile.BadZipFile as exc:
        raise ValueError("import original is not a valid ZIP") from exc
    sources = []
    for metadata_path in sorted(stage.rglob("metadata.json")):
        episode_dir = metadata_path.parent
        metadata = _read_metadata(metadata_path)
        try:
            _source_data_file(episode_dir, metadata)
        except ValueError:
            continue
        else:
            sources.append(
                ParsedSource(_external_episode_id(metadata, episode_dir), episode_dir, metadata)
            )
    if not sources:
        raise ValueError("QRDF archive contains no source episodes")
    if len(sources) > _MAX_IMPORT_EPISODES:
        raise ValueError("QRDF archive contains too many source episodes")
    return sources


def _stage_single_mcap(original_path: Path, *, stage: Path, original_name: str) -> ParsedSource:
    stage.mkdir(parents=True, exist_ok=True)
    data_path = stage / "data.mcap"
    shutil.copy2(original_path, data_path)
    metadata = {
        "qrdf_version": "0.2.0",
        "episode_id": Path(original_name or original_path.name).stem[:64] or "source",
        "data_file": "data.mcap",
    }
    (stage / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (stage / "complete.json").write_text(
        json.dumps({"complete": True}, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return ParsedSource(str(metadata["episode_id"]), stage, metadata)


def _publish_source_directory(
    db: Session, *, job: JobRun, source_dir: Path, artifact: EpisodeArtifact
) -> None:
    operation = ensure_artifact_operation(
        db,
        artifact=artifact,
        job=job,
        operation_kind="raw_source_publish",
        source_path=source_dir,
    )
    if operation.status == "published":
        return
    db.commit()
    materialize_artifact_operation(db, operation_id=operation.id, source_path=source_dir)
    complete_artifact_operation(db, operation_id=operation.id)
    db.commit()
    return


def _materialize_original(
    artifact: EpisodeArtifact, *, import_session: ImportSession, stage: Path
) -> Path:
    stage.mkdir(parents=True, exist_ok=True)
    key = _object_key_from_storage_uri(artifact.storage_uri)
    if artifact.storage_uri.startswith("oss://"):
        from data.infra import oss_client

        bucket = oss_client.bucket_name("raw")
        downloaded = oss_client.download_to(stage / "download", bucket, key)
        return downloaded / Path(key).name
    path = (storage_root_path() / key).resolve()
    if not is_under_storage_root(path) or not path.is_file() or path.is_symlink():
        raise ValueError("import original path is unavailable")
    return path


def _materialize_source_artifact(
    artifact: EpisodeArtifact, *, episode: Episode, stage: Path
) -> Path:
    stage.mkdir(parents=True, exist_ok=True)
    key = _object_key_from_storage_uri(artifact.storage_uri)
    if artifact.storage_uri.startswith("oss://"):
        from data.infra import oss_client

        bucket = oss_client.bucket_name("raw")
        return oss_client.download_to(stage / "source", bucket, key)
    source = (storage_root_path() / key).resolve()
    if not is_under_storage_root(source) or not source.is_dir() or source.is_symlink():
        raise ValueError("source artifact path is unavailable")
    target = stage / "source"
    shutil.copytree(source, target)
    return target


def _validate_zip_members(members: list[zipfile.ZipInfo]) -> None:
    if len(members) > settings.ego_archive_max_entries:
        raise ValueError("import archive has too many entries")
    total = 0
    for member in members:
        _validate_zip_member_path(member)
        total += int(member.file_size or 0)
        if total > settings.ego_archive_max_uncompressed_bytes:
            raise ValueError("import archive exceeds uncompressed size limit")
        if (
            member.file_size
            and not member.is_dir()
            and (member.compress_size <= 0 or member.file_size / member.compress_size > 100)
        ):
            raise ValueError("import archive compression ratio is unsafe")


def _validate_zip_member_path(member: zipfile.ZipInfo) -> None:
    raw = member.filename.replace("\\", "/")
    parsed = PurePosixPath(raw)
    if not raw or parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise ValueError("import archive contains an unsafe member path")
    file_type = (member.external_attr >> 16) & 0o170000
    if file_type == stat.S_IFLNK:
        raise ValueError("import archive must not contain symlink members")


def _archive_member_target(root: Path, member_name: str) -> Path:
    relative = PurePosixPath(member_name.replace("\\", "/"))
    target = root.joinpath(*relative.parts)
    try:
        target.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("import archive member escapes extraction sandbox") from exc
    return target


def _read_metadata(path: Path) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("source metadata is unavailable")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("source metadata is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("source metadata is invalid")
    return value


def _quality_metrics(metadata: dict[str, object], *, data_file: Path) -> EpisodeQualityFacts:
    """Scan MCAP records before exposing any quality metrics.

    A sender-controlled FPS must never become a synthetic frame count. We only
    count reference frames seen in the structurally readable MCAP stream.

    Teleop / bare MCAP packages often omit ``sensors.cameras``. In that case the
    reference camera is inferred from metadata topic names or MCAP channel names
    using the same picker as preview export.
    """
    timing = metadata.get("timing") if isinstance(metadata.get("timing"), dict) else {}
    declared_duration_s = (
        _positive_float(timing.get("duration_s")) if isinstance(timing, dict) else None
    )
    declared_reference_topic = _reference_camera_topic(metadata)
    topic_stats: dict[str, dict[str, int]] = {}
    first_timestamp_ns: int | None = None
    last_timestamp_ns: int | None = None
    raw_cut_suggestions: list[RawCutSuggestion] = []
    try:
        with data_file.open("rb") as stream:
            for schema, channel, message in NonSeekingReader(stream).iter_messages(
                log_time_order=False
            ):
                timestamp = int(message.log_time)
                if first_timestamp_ns is None or timestamp < first_timestamp_ns:
                    first_timestamp_ns = timestamp
                if last_timestamp_ns is None or timestamp > last_timestamp_ns:
                    last_timestamp_ns = timestamp
                topic = str(channel.topic or "")
                if topic.startswith("/") and len(topic) <= 512:
                    stats = topic_stats.get(topic)
                    if stats is None:
                        topic_stats[topic] = {
                            "count": 1,
                            "first_ns": timestamp,
                            "last_ns": timestamp,
                        }
                    else:
                        stats["count"] += 1
                        if timestamp < stats["first_ns"]:
                            stats["first_ns"] = timestamp
                        if timestamp > stats["last_ns"]:
                            stats["last_ns"] = timestamp
                if len(raw_cut_suggestions) < MAX_CUT_SUGGESTIONS and schema is not None:
                    suggestion = decode_qr_segment_event(
                        schema_name=schema.name,
                        topic=channel.topic,
                        payload=message.data,
                    )
                    if suggestion is not None:
                        raw_cut_suggestions.append(suggestion)
    except Exception as exc:
        raise ValueError("source MCAP structural scan failed") from exc

    declared_start_ns = (
        _timestamp_ns(timing.get("start_timestamp_ns")) if isinstance(timing, dict) else None
    )
    declared_end_ns = (
        _timestamp_ns(timing.get("end_timestamp_ns")) if isinstance(timing, dict) else None
    )
    if (
        declared_start_ns is not None
        and declared_end_ns is not None
        and declared_start_ns < declared_end_ns
    ):
        start_ns, end_ns = declared_start_ns, declared_end_ns
    elif (
        first_timestamp_ns is not None
        and last_timestamp_ns is not None
        and first_timestamp_ns < last_timestamp_ns
    ):
        start_ns, end_ns = first_timestamp_ns, last_timestamp_ns
    else:
        raise ValueError("source timeline is unavailable")

    reference_topic = _resolve_reference_camera_topic(
        metadata,
        declared_topic=declared_reference_topic,
        discovered_topics=sorted(topic_stats),
    )
    reference_frames = 0
    reference_first_timestamp_ns: int | None = None
    reference_last_timestamp_ns: int | None = None
    if reference_topic:
        stats = topic_stats.get(reference_topic)
        if stats is None:
            raise ValueError("reference camera timeline is unavailable")
        reference_frames = int(stats["count"])
        reference_first_timestamp_ns = int(stats["first_ns"])
        reference_last_timestamp_ns = int(stats["last_ns"])
        if (
            reference_first_timestamp_ns - start_ns > _REFERENCE_TIMELINE_EDGE_TOLERANCE_NS
            or end_ns - reference_last_timestamp_ns > _REFERENCE_TIMELINE_EDGE_TOLERANCE_NS
        ):
            raise ValueError("reference camera timeline does not cover source")

    duration_s = declared_duration_s or _positive_float((end_ns - start_ns) / 1_000_000_000)
    frame_count = reference_frames if reference_topic else None
    average_rgb_rate_hz = None
    if frame_count is not None and duration_s is not None:
        average_rgb_rate_hz = _positive_float(frame_count / duration_s)
    return EpisodeQualityFacts(
        metrics={
            "reference_frame_count": frame_count,
            "duration_s": duration_s,
            "average_rgb_rate_hz": average_rgb_rate_hz,
        },
        timing={
            "start_timestamp_ns": str(start_ns),
            "end_timestamp_ns": str(end_ns),
            "duration_s": round((end_ns - start_ns) / 1_000_000_000, 6),
        },
        reference_topic=reference_topic or None,
        cut_suggestions=project_qr_segment_events(
            events=raw_cut_suggestions,
            start_ns=start_ns,
            end_ns=end_ns,
        ),
    )


def _timestamp_ns(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdigit() and len(value) <= 20:
        return int(value)
    return None


def _reference_camera_topic(metadata: dict[str, object]) -> str:
    sensors = metadata.get("sensors") if isinstance(metadata.get("sensors"), dict) else {}
    cameras = sensors.get("cameras") if isinstance(sensors, dict) else None
    if not isinstance(cameras, list):
        return ""
    for camera in cameras:
        if isinstance(camera, dict):
            topic = camera.get("topic")
            if isinstance(topic, str) and topic.startswith("/") and len(topic) <= 512:
                return topic
    return ""


def _metadata_topic_names(metadata: dict[str, object]) -> list[str]:
    """Return bounded topic names declared on episode metadata, if any."""
    names: list[str] = []
    seen: set[str] = set()
    topics = metadata.get("topics")
    if not isinstance(topics, list):
        return names
    for entry in topics:
        raw: object
        if isinstance(entry, dict):
            raw = entry.get("name") or entry.get("topic")
        else:
            raw = entry
        if not isinstance(raw, str):
            continue
        topic = raw.strip()
        if not topic.startswith("/") or len(topic) > 512 or topic in seen:
            continue
        seen.add(topic)
        names.append(topic)
    return names


def _resolve_reference_camera_topic(
    metadata: dict[str, object],
    *,
    declared_topic: str,
    discovered_topics: list[str],
) -> str:
    """Prefer declared sensors, then metadata topic names, then MCAP channels."""
    from data.integrations.qrdf.episode_metrics import pick_camera_topic_from_names

    if declared_topic:
        return declared_topic
    for candidate_list in (_metadata_topic_names(metadata), discovered_topics):
        picked = pick_camera_topic_from_names(candidate_list)
        if isinstance(picked, str) and picked.startswith("/") and len(picked) <= 512:
            return picked
    return ""


def _multimodal_projection(
    metadata: dict[str, object],
    *,
    reference_topic: str | None = None,
) -> dict[str, list[dict[str, object]]]:
    """Persist the small, validated workbench projection after quality passes.

    This deliberately excludes source device identity, raw topic schemas and
    arbitrary metadata.  The API validates it again before returning it.
    """
    sensors = metadata.get("sensors") if isinstance(metadata.get("sensors"), dict) else {}
    cameras = sensors.get("cameras") if isinstance(sensors, dict) else None
    lowdim = sensors.get("lowdim") if isinstance(sensors, dict) else None
    streams: list[dict[str, object]] = []
    timeseries: list[dict[str, object]] = []
    seen_streams: set[str] = set()
    seen_series: set[str] = set()
    if isinstance(cameras, list):
        for camera in cameras[:32]:
            if not isinstance(camera, dict):
                continue
            topic = _safe_projection_topic(camera.get("topic"))
            if topic is None or topic in seen_streams:
                continue
            streams.append(
                {
                    "id": topic,
                    "kind": "camera",
                    "width": _bounded_projection_int(camera.get("width")),
                    "height": _bounded_projection_int(camera.get("height")),
                    "fps": _positive_float(camera.get("fps")),
                }
            )
            seen_streams.add(topic)
    inferred = _safe_projection_topic(reference_topic)
    if inferred is not None and inferred not in seen_streams and len(streams) < 32:
        streams.insert(
            0,
            {
                "id": inferred,
                "kind": "camera",
                "width": None,
                "height": None,
                "fps": None,
            },
        )
        seen_streams.add(inferred)
    if isinstance(lowdim, list):
        for sensor in lowdim[:32]:
            if not isinstance(sensor, dict):
                continue
            topic = _safe_projection_topic(sensor.get("topic"))
            if topic is None or topic in seen_series:
                continue
            timeseries.append(
                {
                    "id": topic,
                    "kind": "lowdim",
                    "frequency_hz": _positive_float(sensor.get("frequency_hz")),
                }
            )
            seen_series.add(topic)
    return {"streams": streams, "timeseries": timeseries}


def _safe_projection_topic(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    topic = value.strip()
    if (
        not topic.startswith("/")
        or len(topic) > 512
        or "\x00" in topic
        or "\\" in topic
        or "://" in topic
    ):
        return None
    return topic


def _bounded_projection_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= 16_384 else None


def _source_data_file(episode_dir: Path, metadata: dict[str, object]) -> Path:
    raw = metadata.get("data_file") or "data.mcap"
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise ValueError("source data_file is invalid")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("source data_file is invalid")
    root = episode_dir.resolve()
    candidate = root.joinpath(*relative.parts)
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("source data_file escapes its episode") from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise ValueError("source episode data file is unavailable")
    return resolved


def _safe_source_metadata(metadata: dict[str, object]) -> dict[str, object]:
    safe: dict[str, object] = {}
    for key in ("qrdf_version", "episode_id", "data_file"):
        value = metadata.get(key)
        if isinstance(value, str) and len(value) <= 256:
            safe[key] = value
    for key in ("task", "robot", "capture"):
        value = metadata.get(key)
        if isinstance(value, dict):
            safe[key] = _bounded_json(value)
    return safe


def _bounded_json(value: dict[str, object], *, limit: int = 4096) -> dict[str, object]:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(raw.encode("utf-8")) > limit:
        return {}
    return value


def _import_task_language(db: Session, import_session: ImportSession) -> str:
    """Resolve the human-controlled import default, never source metadata."""
    task_label_id = import_session.task_label_id
    if not isinstance(task_label_id, int) or isinstance(task_label_id, bool) or task_label_id <= 0:
        raise ValueError("import session task label is unavailable")
    task_label = db.get(TaskLabel, task_label_id)
    if task_label is None:
        raise ValueError("import session task label is unavailable")
    name = str(task_label.name or "").strip()
    if not name:
        raise ValueError("import session task label is unavailable")
    return name[:1000]


def _external_episode_id(metadata: dict[str, object], episode_dir: Path) -> str:
    value = str(metadata.get("episode_id") or episode_dir.name or "").strip()
    return value[:64] or "source"


def _episode_uid(import_session_id: str, external_episode_id: str, index: int) -> str:
    digest = hashlib.sha256(
        f"{import_session_id}\0{external_episode_id}\0{index}".encode()
    ).hexdigest()
    return f"src_{digest[:32]}"


def _modality_for_batch(batch: Batch) -> str:
    if batch.batch_type in {"ego", "teleop"}:
        return batch.batch_type
    return "qrdf"


def _object_key_from_storage_uri(uri: str) -> str:
    if uri.startswith("nas://"):
        key = uri[len("nas://") :]
    elif uri.startswith("oss://"):
        parts = uri[len("oss://") :].split("/", 1)
        if len(parts) != 2:
            raise ValueError("artifact storage URI is invalid")
        key = parts[1]
    else:
        raise ValueError("artifact storage URI is invalid")
    if not key.startswith(("raw/v1/", "raw/v2/")):
        raise ValueError("artifact is outside raw namespace")
    return key


def _parse_stage_dir(import_session_id: str, job_id: str) -> Path:
    root = import_staging_dir(import_session_id) / "parse" / job_id
    resolved = root.resolve()
    if not is_under_storage_root(resolved):
        raise ValueError("parse stage is outside storage root")
    return resolved


def _quality_stage_dir(episode_id: int, job_id: str) -> Path:
    root = storage_root_path() / "process" / "quality" / str(episode_id) / job_id
    resolved = root.resolve()
    if not is_under_storage_root(resolved):
        raise ValueError("quality stage is outside storage root")
    return resolved


def _preview_stage_dir(episode_id: int, job_id: str) -> Path:
    root = storage_root_path() / "hot" / "episode-preview" / str(episode_id) / job_id
    resolved = root.resolve()
    if not is_under_storage_root(resolved):
        raise ValueError("preview stage is outside storage root")
    return resolved


def _process_preview_key(*, episode: Episode, job: JobRun) -> str:
    prefix = process_episode_run_prefix(
        workspace_id=episode.workspace_id,
        task_set_id=episode.task_set_id,
        batch_id=episode.batch_id,
        episode_id=episode.id,
        job_id=job.id,
    )
    return f"{prefix}/preview.mp4"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(
        path for path in root.rglob("*") if path.is_file() and not path.is_symlink()
    ):
        digest.update(item.relative_to(root).as_posix().encode("utf-8"))
        with item.open("rb") as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
    return digest.hexdigest()


def _tree_size(root: Path) -> int:
    return sum(
        item.stat().st_size for item in root.rglob("*") if item.is_file() and not item.is_symlink()
    )


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} is invalid")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is invalid") from exc
    if number <= 0:
        raise ValueError(f"{field} is invalid")
    return number


def _positive_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
