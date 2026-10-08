"""Schedule isolated QRDF admission for each uploaded source."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from data.config import settings
from data.database import (
    Episode,
    EpisodeCollectorAttribution,
    EpisodeDeviceAttribution,
    JobRun,
)
from data.models.collection_upload import (
    CollectionUploadSession,
    CollectionUploadSessionPackage,
)
from data.models.data_package import DataPackage, PackageIntakeReview
from data.services.capture_provenance import project_capture_provenance
from data.services.client_admission import client_admission_state
from data.services.collection_packages import PackageStateConflictError
from data.services.duance_imports import build_duance_import_manifest
from data.services.episode_admission import (
    current_episode_admission_fact,
    record_episode_admission_fact,
)
from data.services.import_intake import MAX_IMPORT_TOTAL_BYTES
from data.services.job_runs import create_or_get_job_in_transaction
from data.services.package_dashboard_facts import apply_facts, capture_facts

COLLECTION_UPLOAD_PARSE_JOB_KIND = "collection_upload_parse"
COLLECTION_UPLOAD_PARSE_QUEUE = "ingest"


class CollectionUploadParseError(ValueError):
    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code[:64]


def ensure_collection_upload_parse_job(
    db: Session, upload_session: CollectionUploadSession
) -> JobRun:
    """Create the durable ingest job once for an uploaded session."""
    return create_or_get_job_in_transaction(
        db,
        kind=COLLECTION_UPLOAD_PARSE_JOB_KIND,
        resource_type="platform",
        resource_id=upload_session.id,
        idempotency_key=f"collection-upload-parse:{upload_session.id}",
        queue=COLLECTION_UPLOAD_PARSE_QUEUE,
        actor_id=upload_session.created_by_user_id,
        workspace_id=upload_session.workspace_id,
        detail={"upload_session_id": upload_session.id},
    )


def parse_collection_upload_job(db: Session, job: JobRun) -> dict[str, Any]:
    if (
        job.kind != COLLECTION_UPLOAD_PARSE_JOB_KIND
        or job.resource_type != "platform"
        or job.resource_id != (job.detail_json or {}).get("upload_session_id")
    ):
        raise ValueError("collection upload parse job resource is invalid")
    parse_collection_upload_session(db, session_id=job.resource_id)
    session = db.get(CollectionUploadSession, job.resource_id)
    return {
        "upload_session_id": job.resource_id,
        "_follow_up_job_ids": list((session.result_json or {}).get("admission_job_ids", [])),
    }


def _source_id(package_uid: str, source: dict) -> str:
    return hashlib.sha256(f"{package_uid}:{source.get('source_key', '')}".encode()).hexdigest()


def _completed_source_identity(result: dict, package_uid: str, source: dict) -> dict:
    source_id = _source_id(package_uid, source)
    declaration = (result.get("oss_multipart_sources") or {}).get(source_id)
    if declaration is None:
        declaration = result.get("oss_multipart")
    if not isinstance(declaration, dict) or declaration.get("completion_state") != "completed":
        raise CollectionUploadParseError("source_identity_missing")
    identity = dict(declaration.get("provider_identity") or {})
    if (
        identity.get("bucket_role") != "raw"
        or not identity.get("object_key")
        or identity.get("object_key") != source.get("raw_object_key")
        or not (identity.get("version_id") or identity.get("etag"))
        or int(identity.get("size_bytes") or 0) <= 0
    ):
        raise CollectionUploadParseError("source_identity_invalid")
    # Browser-set object metadata is a declaration, not a verified digest.
    identity["sha256"] = None
    return identity


_SOURCE_ADMISSION_KEYS = (
    "source_id",
    "episode_id",
    "status",
    "attempt",
    "error_code",
    "integrity_source",
    "client_admission_fallback",
)


def refresh_upload_admission_counts(db: Session, upload_session: CollectionUploadSession) -> None:
    """Refresh source counts from durable per-source outcomes under session lock."""
    result = dict(upload_session.result_json or {})
    sources = list((result.get("admission_sources") or {}).values())
    failed = [s for s in sources if s.get("status") == "failed"]
    result.update(
        ready_count=sum(s.get("status") == "ready" for s in sources),
        failed_count=len(failed),
        failed=failed,
        pending_count=sum(s.get("status") in {"queued", "running"} for s in sources),
    )
    upload_session.result_json = result
    # These counts include sources without an Episode, so package detail can
    # explain malformed metadata without inventing an Episode identity.
    for link in db.scalars(
        select(CollectionUploadSessionPackage).where(
            CollectionUploadSessionPackage.upload_session_id == upload_session.id
        )
    ):
        package = db.get(DataPackage, link.data_package_id)
        owned = [s for s in sources if s.get("package_uid") == link.package_uid]
        package.qrdf_facts_json = {
            **(package.qrdf_facts_json or {}),
            "source_admission": {
                "ready_count": sum(s.get("status") == "ready" for s in owned),
                "failed_count": sum(s.get("status") == "failed" for s in owned),
                "sources": [{k: s[k] for k in _SOURCE_ADMISSION_KEYS if k in s} for s in owned],
            },
        }


def _schedule_persisted_sources(db: Session, upload_session: CollectionUploadSession) -> None:
    """Commit each source/job independently; QRDF and object I/O run in workers."""
    session_id = upload_session.id
    declarations = _load_declarations(upload_session, fixture_mode=False)
    links = list(
        db.scalars(
            select(CollectionUploadSessionPackage).where(
                CollectionUploadSessionPackage.upload_session_id == session_id
            )
        )
    )
    by_uid = {link.package_uid: link.data_package_id for link in links}
    if set(by_uid) != set(declarations):
        raise CollectionUploadParseError("package_declarations_mismatch")
    upload_session.status = "parsing"
    for package_id in by_uid.values():
        package = db.get(DataPackage, package_id)
        if package.status in {"pending_upload", "uploading"}:
            package.status = "parsing"
    db.commit()
    for package_uid, sources in declarations.items():
        for index, source in enumerate(sources, 1):
            source_id = _source_id(package_uid, source)
            episode_id = None
            try:
                upload_session = db.get(CollectionUploadSession, session_id)
                result = dict(upload_session.result_json or {})
                if source_id in (result.get("admission_sources") or {}):
                    db.commit()
                    continue
                parsed = _parse_persisted_source(
                    upload_session,
                    package_uid=package_uid,
                    source=source,
                    source_count=sum(map(len, declarations.values())),
                    verify_bytes=False,
                )
                if _is_lerobot(source, parsed["metadata"]):
                    raise CollectionUploadParseError("lerobot_not_supported_on_collection_upload")
                identity = None
                legacy_path = None
                if upload_session.upload_mode == "oss_multipart":
                    identity = _completed_source_identity(result, package_uid, source)
                    fingerprint_input = {
                        "object": identity,
                        "metadata_sha256": hashlib.sha256(
                            source["metadata_text"].encode()
                        ).hexdigest(),
                    }
                else:
                    # Compatibility intake has already assembled the bytes. Pin
                    # their server hash before publishing a raw object.
                    staging = _collection_staging_dir(session_id)
                    legacy_path = _safe_staging_path(
                        staging, str(source.get("staging_path") or "upload.bin")
                    )
                    if not legacy_path.is_file() and sum(map(len, declarations.values())) == 1:
                        legacy_path = _safe_staging_path(staging, "upload.bin")
                    if not legacy_path.is_file():
                        raise CollectionUploadParseError("uploaded_data_unavailable")
                    fingerprint_input = {
                        "sha256": _file_sha256(legacy_path),
                        "metadata_sha256": hashlib.sha256(
                            source["metadata_text"].encode()
                        ).hexdigest(),
                    }
                parsed["source_fingerprint"] = hashlib.sha256(
                    json.dumps(fingerprint_input, sort_keys=True).encode()
                ).hexdigest()
                package = db.get(DataPackage, by_uid[package_uid])
                episode, _ = _get_or_create_episode(
                    db,
                    upload_session=upload_session,
                    package=package,
                    parsed=parsed,
                    index=index,
                )
                episode_id = episode.id
                # Release every source/package lock before storage operations.
                db.commit()
                if identity is None:
                    from data.infra.object_storage import StorageObjectRef
                    from data.infra.storage_provider import get_storage_provider

                    ref = StorageObjectRef(
                        "raw",
                        f"raw/v2/collection/{session_id}/{uuid4().hex}/source.mcap",
                        None,
                        "",
                        legacy_path.stat().st_size,
                        fingerprint_input["sha256"],
                    )
                    identity = asdict(
                        get_storage_provider().put_worker_object(ref, str(legacy_path))
                    )
                upload_session = db.scalar(
                    select(CollectionUploadSession)
                    .where(CollectionUploadSession.id == session_id)
                    .with_for_update()
                )
                job = create_or_get_job_in_transaction(
                    db,
                    kind="collection_upload_admission",
                    resource_type="episode",
                    resource_id=episode_id,
                    idempotency_key=f"collection-admission:{session_id}:{source_id}",
                    queue="ingest",
                    workspace_id=upload_session.workspace_id,
                    actor_id=upload_session.created_by_user_id,
                    detail={"upload_session_id": session_id, "source_id": source_id},
                )
                result = dict(upload_session.result_json or {})
                states = dict(result.get("admission_sources") or {})
                states[source_id] = {
                    "source_id": source_id,
                    "package_uid": package_uid,
                    "episode_id": episode_id,
                    "source_fingerprint": parsed["source_fingerprint"],
                    "source_object": identity,
                    "metadata_text": source["metadata_text"],
                    "data_path": source["data_file"]["path"],
                    "declared_sha256": source["data_file"]["sha256"],
                    "job_id": job.id,
                    "status": "queued",
                    "attempt": 0,
                    **client_admission_state(result, source=source),
                }
                result["admission_sources"] = states
                result["admission_job_ids"] = sorted(
                    set(result.get("admission_job_ids", []) + [job.id])
                )
                upload_session.result_json = result
                refresh_upload_admission_counts(db, upload_session)
                db.commit()
            except Exception as exc:  # noqa: BLE001 - independent source failure boundary.
                db.rollback()
                code = getattr(exc, "code", "source_declaration_invalid")
                upload_session = db.scalar(
                    select(CollectionUploadSession)
                    .where(CollectionUploadSession.id == session_id)
                    .with_for_update()
                )
                result = dict(upload_session.result_json or {})
                states = dict(result.get("admission_sources") or {})
                states[source_id] = {
                    "source_id": source_id,
                    "package_uid": package_uid,
                    "episode_id": episode_id,
                    "status": "failed",
                    "error_code": code,
                }
                if episode_id is not None:
                    episode = db.get(Episode, episode_id)
                    current = current_episode_admission_fact(db, episode_id=episode_id)
                    record_episode_admission_fact(
                        db,
                        episode_id=episode_id,
                        attempt=current.attempt + 1 if current else 1,
                        source_fingerprint=episode.source_fingerprint,
                        validation_policy_version="v1",
                        integrity_status="failed",
                        preview_status="failed",
                        output_verification_status="failed",
                        report_ref={
                            "error_code": code,
                            "error_type": type(exc).__name__,
                        },
                        error_code=code,
                    )
                result["admission_sources"] = states
                upload_session.result_json = result
                refresh_upload_admission_counts(db, upload_session)
                db.commit()
        package = db.get(DataPackage, by_uid[package_uid])
        episodes = list(db.scalars(select(Episode).where(Episode.data_package_id == package.id)))
        facts = capture_facts(db, package.id)
        apply_facts(package, facts)
        duration = facts.get("captured_duration_s")
        package.captured_duration_hours = (
            (duration / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if duration is not None
            else None
        )
        if package.status in {"parsing", "ingested"}:
            package.status = "pending_intake_review"
        package.upload_completed_at = package.upload_completed_at or datetime.now(
            timezone.utc
        ).replace(tzinfo=None)
        package.qrdf_facts_json = {
            **(package.qrdf_facts_json or {}),
            "episodes": [
                {
                    "episode_id": ep.episode_uid,
                    "privacy_sensitive": ep.metadata_json.get("privacy_sensitive", False),
                }
                for ep in episodes
            ],
        }
        db.commit()
    upload_session = db.get(CollectionUploadSession, session_id)
    upload_session.status = "succeeded"  # Scheduling finished; source counts express admission.
    upload_session.result_json = {
        **upload_session.result_json,
        "parse_stage": "pending_intake_review",
    }
    db.commit()


def parse_collection_upload_session(db: Session, session_id: str) -> None:
    """Persist production sources independently; retain test-only fixture parsing."""
    upload_session = db.scalar(
        select(CollectionUploadSession)
        .where(CollectionUploadSession.id == session_id)
        .with_for_update()
    )
    if upload_session is None:
        raise LookupError("collection upload session does not exist")
    if upload_session.status == "succeeded":
        return
    if upload_session.status not in {"uploaded", "parsing"}:
        raise PackageStateConflictError("upload_session_not_uploaded")

    if not (upload_session.result_json or {}).get("parse_fixture_mode"):
        _schedule_persisted_sources(db, upload_session)
        return

    try:
        result = dict(upload_session.result_json or {})
        fixture_mode = bool(result.get("parse_fixture_mode"))
        if fixture_mode and not settings.test_mode:
            raise CollectionUploadParseError("parse_fixture_mode_not_allowed")
        declarations = _load_declarations(upload_session, fixture_mode=fixture_mode)
        links = list(
            db.scalars(
                select(CollectionUploadSessionPackage)
                .where(CollectionUploadSessionPackage.upload_session_id == upload_session.id)
                .order_by(CollectionUploadSessionPackage.data_package_id)
                .with_for_update()
            )
        )
        packages = {
            package.id: package
            for package in db.scalars(
                select(DataPackage)
                .where(DataPackage.id.in_([link.data_package_id for link in links]))
                .order_by(DataPackage.id)
                .with_for_update()
            )
        }
        by_uid = {
            link.package_uid: packages[link.data_package_id]
            for link in links
            if link.data_package_id in packages
        }
        if not links or set(by_uid) != set(declarations):
            raise CollectionUploadParseError("package_declarations_mismatch")

        upload_session.status = "parsing"
        upload_session.error_code = ""
        upload_session.error_message = ""
        upload_session.updated_at = datetime.utcnow()
        for package in packages.values():
            _advance_package(db, package.id, ("pending_upload", "uploading"), "parsing")

        episode_ids: list[int] = []
        reused_episode_ids: list[int] = []
        for package_uid, sources in declarations.items():
            package = by_uid[package_uid]
            package_facts: dict[str, object] | None = None
            counted_episode_ids: set[int] = set()
            episode_facts: list[dict[str, object]] = []
            for index, source in enumerate(sources, start=1):
                parsed = _parse_fixture_source(source)
                if _is_lerobot(source, parsed["metadata"]):
                    raise CollectionUploadParseError("lerobot_not_supported_on_collection_upload")
                episode, created = _get_or_create_episode(
                    db,
                    upload_session=upload_session,
                    package=package,
                    parsed=parsed,
                    index=index,
                )
                current_fact = current_episode_admission_fact(
                    db, episode_id=episode.id, for_update=True
                )
                preview_ready = _preview_available(upload_session)
                record_episode_admission_fact(
                    db,
                    episode_id=episode.id,
                    attempt=(current_fact.attempt + 1 if current_fact else 1),
                    source_fingerprint=episode.source_fingerprint,
                    validation_policy_version="v1",
                    integrity_status="passed",
                    preview_status="ready" if preview_ready else "pending",
                    output_verification_status="verified" if preview_ready else "pending",
                    qrdf_profile=str(parsed["modality"]),
                    report_ref={
                        "source": "collection_upload_parse_fixture",
                        "episode_uid": episode.episode_uid,
                    },
                    objects=_fixture_objects(episode) if preview_ready else [],
                )
                episode_ids.append(episode.id)
                if not created:
                    reused_episode_ids.append(episode.id)
                if episode.id in counted_episode_ids:
                    continue
                counted_episode_ids.add(episode.id)
                episode_facts.append(
                    {
                        "episode_id": episode.episode_uid,
                        "privacy_sensitive": parsed["privacy_sensitive"],
                    }
                )
                if package_facts is None:
                    package_facts = _package_qrdf_facts(
                        capture_mode=parsed["capture_mode"],
                        app_version=parsed["capture_app_version"],
                        preview_available=preview_ready,
                    )
            if package_facts is None:
                raise CollectionUploadParseError("package_declaration_empty")
            package_facts["episodes"] = episode_facts
            facts = capture_facts(db, package.id)
            duration = facts.get("captured_duration_s")
            hours = (
                (duration / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if duration is not None
                else None
            )
            now = datetime.utcnow()
            _advance_package(
                db,
                package.id,
                ("parsing",),
                "ingested",
                capture_mode=str(package_facts["capture"]["mode"]),
                qrdf_facts_json=package_facts,
                captured_duration_hours=hours,
                upload_completed_at=package.upload_completed_at or now,
                parse_error_code="",
                parse_error_message="",
                **facts,
            )
            _advance_package(db, package.id, ("ingested",), "pending_intake_review")

        result.update(
            {
                "parse_stage": "pending_intake_review",
                "episode_ids": sorted(set(episode_ids)),
                "reused_episode_ids": sorted(set(reused_episode_ids)),
            }
        )
        upload_session.result_json = result
        upload_session.status = "succeeded"
        upload_session.updated_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        db.rollback()
        code = (
            exc.code
            if isinstance(exc, CollectionUploadParseError)
            else "collection_upload_parse_failed"
        )
        _mark_parse_failed(db, session_id=session_id, code=code)
        raise ValueError(code) from exc


def _advance_package(
    db: Session,
    package_id: int,
    from_statuses: tuple[str, ...],
    to_status: str,
    **fields: object,
) -> None:
    changed = db.execute(
        update(DataPackage)
        .where(
            DataPackage.id == package_id,
            DataPackage.status.in_(from_statuses),
        )
        .values(status=to_status, updated_at=datetime.utcnow(), **fields),
        execution_options={"synchronize_session": False},
    )
    if changed.rowcount != 1:
        raise PackageStateConflictError("package_status_conflict")


def _load_declarations(
    upload_session: CollectionUploadSession, *, fixture_mode: bool
) -> dict[str, list[dict[str, object]]]:
    raw = (upload_session.result_json or {}).get("declarations")
    if fixture_mode and isinstance(raw, list):
        grouped: dict[str, list[dict[str, object]]] = {}
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("package_uid"), str):
                raise CollectionUploadParseError("package_declaration_invalid")
            grouped.setdefault(str(item["package_uid"]), []).append(dict(item))
        return grouped
    if not isinstance(raw, dict):
        raise CollectionUploadParseError("package_declarations_missing")
    declarations: dict[str, list[dict[str, object]]] = {}
    for package_uid, items in raw.items():
        if (
            not isinstance(package_uid, str)
            or not isinstance(items, list)
            or not items
            or any(not isinstance(item, dict) for item in items)
        ):
            raise CollectionUploadParseError("package_declaration_invalid")
        declarations[package_uid] = [dict(item) for item in items]
    return declarations


def _parse_fixture_source(source: dict[str, object]) -> dict[str, Any]:
    start_ns = _nonnegative_int(source.get("start_ns"), "start_ns")
    end_ns = _nonnegative_int(source.get("end_ns"), "end_ns")
    if start_ns >= end_ns:
        raise CollectionUploadParseError("source_timeline_invalid")
    episode_id = str(source.get("episode_id") or "").strip()
    fingerprint = str(source.get("source_fingerprint") or "").strip()
    if not episode_id or not fingerprint:
        raise CollectionUploadParseError("package_declaration_invalid")
    metadata = {
        "episode_id": episode_id,
        "capture": {
            "mode": source.get("capture_mode"),
            "app_version": source.get("capture_app_version"),
        },
        "privacy_sensitive": source.get("privacy_sensitive", False),
        "devices": source.get("devices", []),
    }
    return {
        "episode_id": episode_id,
        "start_ns": start_ns,
        "end_ns": end_ns,
        "capture_mode": str(source.get("capture_mode") or ""),
        "capture_app_version": str(source.get("capture_app_version") or ""),
        "privacy_sensitive": source.get("privacy_sensitive", False),
        "modality": str(source.get("modality") or "qrdf"),
        "source_fingerprint": fingerprint,
        "metadata": metadata,
    }


def _parse_persisted_source(
    upload_session: CollectionUploadSession,
    *,
    package_uid: str,
    source: dict[str, object],
    source_count: int,
    verify_bytes: bool = True,
) -> dict[str, Any]:
    from qrdf.exceptions import IncompatibleVersionError
    from qrdf.models.episode import EpisodeMetadata

    manifest = build_duance_import_manifest(
        source=source.get("source", {}),
        metadata_text=source.get("metadata_text", ""),
        data_file=source.get("data_file", {}),
    )
    try:
        metadata_dict = json.loads(manifest.metadata_text)
        metadata = EpisodeMetadata.model_validate(metadata_dict)
        metadata.check_version_compatible()
    except IncompatibleVersionError as exc:
        raise CollectionUploadParseError("metadata_version_incompatible") from exc
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CollectionUploadParseError("metadata_schema_invalid") from exc
    if verify_bytes:
        _verify_uploaded_data(
            upload_session,
            package_uid=package_uid,
            data_path=manifest.data_path,
            source=source,
            source_count=source_count,
            expected_size=manifest.data_size_bytes,
            expected_sha256=manifest.data_sha256,
        )
    validated_metadata = metadata.model_dump(mode="json", exclude_none=True)
    capture = (
        validated_metadata.get("capture")
        if isinstance(validated_metadata.get("capture"), dict)
        else {}
    )
    return {
        "episode_id": manifest.episode_id,
        "start_ns": manifest.start_ns,
        "end_ns": manifest.end_ns,
        "capture_mode": str(capture.get("mode") or ""),
        "capture_app_version": str(capture.get("app_version") or ""),
        "privacy_sensitive": metadata_dict.get("privacy_sensitive", False),
        "modality": str(validated_metadata.get("modality") or "qrdf"),
        "source_fingerprint": manifest.source_key,
        "metadata": metadata_dict,
    }


def _verify_uploaded_data(
    upload_session: CollectionUploadSession,
    *,
    package_uid: str,
    data_path: str,
    source: dict[str, object],
    source_count: int,
    expected_size: int,
    expected_sha256: str,
) -> None:
    staging = _collection_staging_dir(upload_session.id)
    if source_count == 1 and not any(
        key.startswith("chunked:") for key in (upload_session.result_json or {})
    ):
        path = staging / "upload.bin"
    else:
        expected_staging_path = _source_staging_relative_path(package_uid, data_path, source)
        if source.get("staging_path") != expected_staging_path:
            raise CollectionUploadParseError("source_staging_mapping_invalid")
        path = _safe_staging_path(staging, expected_staging_path)
    if (
        not path.is_file()
        or path.is_symlink()
        or path.stat().st_size != expected_size
        or _file_sha256(path) != expected_sha256
    ):
        raise CollectionUploadParseError("uploaded_data_checksum_mismatch")


def _prepare_uploaded_sources(
    upload_session: CollectionUploadSession,
    declarations: dict[str, list[dict[str, object]]],
) -> None:
    """Ensure declared MCAP payloads exist under staging.

    The Duance SDK uploads plain episode payloads (metadata + MCAP), not ZIP
    archives. Multi-source sessions therefore require one staged file per
    declaration at a service-derived source-specific path. Legacy sessions retain
    ``sources/{package_uid}/{data_path}``. A single-source
    session may still use the session-level ``upload.bin`` blob from chunked
    or OSS intake.
    """
    entries: dict[str, int] = {}
    for package_uid, sources in declarations.items():
        for source in sources:
            data_file = source.get("data_file")
            if not isinstance(data_file, dict):
                raise CollectionUploadParseError("package_declaration_invalid")
            data_path = data_file.get("path")
            size = data_file.get("size_bytes")
            if not isinstance(data_path, str) or type(size) is not int or size <= 0:
                raise CollectionUploadParseError("package_declaration_invalid")
            relative = _source_staging_relative_path(package_uid, data_path, source)
            if source.get("staging_path") != relative or relative in entries:
                raise CollectionUploadParseError("source_staging_mapping_invalid")
            entries[relative] = size
    # Share the same deployment-owned byte budget as chunked/OSS intake.
    if sum(entries.values()) > MAX_IMPORT_TOTAL_BYTES:
        raise CollectionUploadParseError("source_total_size_limit_exceeded")
    if len(entries) <= 1 and not any(
        key.startswith("chunked:") for key in (upload_session.result_json or {})
    ):
        _materialize_upload_blob(upload_session)
        return

    if upload_session.upload_mode == "oss_multipart":
        _materialize_multipart_sources(upload_session, entries)
        return

    staging = _collection_staging_dir(upload_session.id)
    missing = [
        relative for relative in entries if not _safe_staging_path(staging, relative).is_file()
    ]
    if missing:
        raise CollectionUploadParseError("uploaded_data_unavailable")


def _materialize_upload_blob(upload_session: CollectionUploadSession) -> Path:
    staging = _collection_staging_dir(upload_session.id)
    path = staging / "upload.bin"
    if path.is_file() and not path.is_symlink():
        return path
    if upload_session.upload_mode != "oss_multipart":
        raise CollectionUploadParseError("uploaded_data_unavailable")
    from data.infra import oss_client

    declaration = dict((upload_session.result_json or {}).get("oss_multipart") or {})
    provider_identity = declaration.get("provider_identity")
    if isinstance(provider_identity, dict):
        from data.infra.object_storage import StorageObjectRef
        from data.infra.storage_provider import get_storage_provider

        ref = StorageObjectRef(
            "raw",
            str(provider_identity.get("object_key") or declaration.get("object_key") or ""),
            provider_identity.get("version_id"),
            str(provider_identity.get("etag") or ""),
            int(provider_identity.get("size_bytes") or 0),
            str(provider_identity.get("sha256") or "") or None,
        )
        downloaded_path = staging / "parse-source" / Path(ref.object_key).name
        get_storage_provider().download_file(ref, str(downloaded_path))
        staging.mkdir(parents=True, exist_ok=True)
        temporary = staging / ".upload.bin.downloading"
        shutil.copyfile(downloaded_path, temporary)
        os.replace(temporary, path)
        return path
    downloaded = oss_client.download_to(
        staging / "parse-source",
        str(declaration.get("bucket") or ""),
        str(declaration.get("object_key") or ""),
    )
    provider_path = downloaded / Path(str(declaration.get("object_key") or "")).name
    if not provider_path.is_file() or provider_path.is_symlink():
        raise CollectionUploadParseError("uploaded_data_unavailable")
    staging.mkdir(parents=True, exist_ok=True)
    temporary = staging / ".upload.bin.downloading"
    shutil.copyfile(provider_path, temporary)
    os.replace(temporary, path)
    return path


def _materialize_multipart_sources(
    upload_session: CollectionUploadSession, entries: dict[str, int]
) -> None:
    """Materialize every completed direct-upload object into scratch once."""
    from data.infra import oss_client

    result = dict(upload_session.result_json or {})
    declarations = result.get("oss_multipart_sources")
    if not isinstance(declarations, dict):
        # A one-source legacy session is handled by _materialize_upload_blob.
        raise CollectionUploadParseError("uploaded_data_unavailable")
    staging = _collection_staging_dir(upload_session.id)
    for declaration in declarations.values():
        if not isinstance(declaration, dict):
            raise CollectionUploadParseError("uploaded_data_unavailable")
        if declaration.get("completion_state") != "completed":
            raise CollectionUploadParseError("uploaded_data_unavailable")
        package_uid = str(declaration.get("package_uid") or "")
        data_path = str(declaration.get("data_path") or "")
        relative = _source_staging_relative_path(package_uid, data_path)
        destination = _safe_staging_path(staging, relative)
        if destination.is_file() and not destination.is_symlink():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.downloading")
        provider_identity = declaration.get("provider_identity")
        if isinstance(provider_identity, dict):
            from data.infra.object_storage import StorageObjectRef
            from data.infra.storage_provider import get_storage_provider

            ref = StorageObjectRef(
                "raw",
                str(provider_identity.get("object_key") or declaration.get("object_key") or ""),
                provider_identity.get("version_id"),
                str(provider_identity.get("etag") or ""),
                int(provider_identity.get("size_bytes") or 0),
                str(provider_identity.get("sha256") or "") or None,
            )
            get_storage_provider().download_file(ref, str(temporary))
        else:
            downloaded = oss_client.download_to(
                staging / "parse-source",
                str(declaration.get("bucket") or ""),
                str(declaration.get("object_key") or ""),
            )
            provider_path = downloaded / Path(str(declaration.get("object_key") or "")).name
            if not provider_path.is_file() or provider_path.is_symlink():
                raise CollectionUploadParseError("uploaded_data_unavailable")
            shutil.copyfile(provider_path, temporary)
        os.replace(temporary, destination)


def _collection_staging_dir(session_id: str) -> Path:
    from data.services.collection_upload_intake import collection_upload_staging_dir

    return collection_upload_staging_dir(session_id)


def _source_staging_relative_path(
    package_uid: str, data_path: str, source: dict[str, object] | None = None
) -> str:
    source = source or {}
    version = source.get("staging_layout_version", 1)
    if version == 2:
        source_key = source.get("source_key")
        if not isinstance(source_key, str) or not source_key:
            raise CollectionUploadParseError("source_staging_mapping_invalid")
        source_id = hashlib.sha256(f"{package_uid}:{source_key}".encode()).hexdigest()
        relative = Path("sources", package_uid, source_id, *data_path.split("/"))
    elif version == 1:
        relative = Path("sources", package_uid, *data_path.split("/"))
    else:
        raise CollectionUploadParseError("source_staging_mapping_invalid")
    value = relative.as_posix()
    staging = _collection_staging_dir("00000000-0000-0000-0000-000000000000")
    _safe_staging_path(staging, value)
    return value


def _safe_staging_path(staging: Path, relative: str) -> Path:
    unresolved = staging / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise CollectionUploadParseError("source_staging_mapping_invalid")
    for component in [unresolved, *unresolved.parents]:
        if component.is_symlink():
            raise CollectionUploadParseError("source_staging_mapping_invalid")
    candidate = (staging / relative).resolve()
    try:
        candidate.relative_to(staging.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise CollectionUploadParseError("source_staging_mapping_invalid") from exc
    return candidate


def _get_or_create_episode(
    db: Session,
    *,
    upload_session: CollectionUploadSession,
    package: DataPackage,
    parsed: dict[str, Any],
    index: int,
) -> tuple[Episode, bool]:
    db.query(DataPackage).filter(DataPackage.id == package.id).with_for_update().one()
    if (
        db.query(PackageIntakeReview.id)
        .filter(PackageIntakeReview.data_package_id == package.id)
        .first()
    ):
        raise PackageStateConflictError("package_intake_finalized")
    fingerprint = str(parsed["source_fingerprint"])
    lock_material = f"collection-episode:{package.id}:{fingerprint}".encode()
    lock_key = int.from_bytes(hashlib.sha256(lock_material).digest()[:8], "big", signed=True)
    db.execute(select(func.pg_advisory_xact_lock(lock_key)))
    existing = db.scalar(
        select(Episode)
        .where(
            Episode.data_package_id == package.id,
            Episode.kind == "source",
            Episode.source_fingerprint == fingerprint,
        )
        .with_for_update()
    )
    if existing is not None:
        return existing, False
    uid_digest = hashlib.sha256(f"{package.id}\0{fingerprint}\0{index}".encode()).hexdigest()
    metadata = parsed["metadata"]
    provenance = project_capture_provenance(
        db,
        workspace_id=package.workspace_id,
        raw_metadata=metadata,
    )
    duration_s = round((parsed["end_ns"] - parsed["start_ns"]) / 1_000_000_000, 6)
    episode = Episode(
        episode_uid=f"cup_{uid_digest[:32]}",
        workspace_id=package.workspace_id,
        task_set_id=None,
        batch_id=None,
        data_package_id=package.id,
        kind="source",
        modality=str(parsed["modality"])[:32],
        source_fingerprint=fingerprint,
        workflow_status="discovered",
        quality_status="pending",
        annotation_status="pending",
        review_status="pending",
        validity_status="valid",
        reported_source_group_key=provenance.source_group_key,
        reported_source_group_name=provenance.source_group_name,
        reported_source_group_status=provenance.source_group_status,
        metadata_json={
            "collection_upload": {
                "upload_session_id": upload_session.id,
                "external_episode_id": parsed["episode_id"],
            },
            "timing": {
                "start_timestamp_ns": str(parsed["start_ns"]),
                "end_timestamp_ns": str(parsed["end_ns"]),
                "duration_s": duration_s,
            },
            "privacy_sensitive": parsed["privacy_sensitive"],
        },
    )
    db.add(episode)
    db.flush()
    db.add_all(
        [
            EpisodeCollectorAttribution(
                root_source_episode_id=episode.id,
                collector_profile_id=provenance.collector_profile_id,
                source="machine_reported",
                created_by_user_id=upload_session.created_by_user_id,
                reported_identifier=provenance.collector_reported_identifier,
                match_status=provenance.collector_match_status,
                note="collection upload metadata",
            ),
            EpisodeDeviceAttribution(
                root_source_episode_id=episode.id,
                collection_device_id=provenance.collection_device_id,
                source="machine_reported",
                created_by_user_id=upload_session.created_by_user_id,
                reported_identifier=provenance.device_serial,
                match_status=provenance.device_match_status,
                note="collection upload metadata",
            ),
        ]
    )
    return episode, True


def _package_qrdf_facts(
    *,
    capture_mode: str,
    app_version: str,
    preview_available: bool,
) -> dict[str, object]:
    return {
        "capture": {"mode": capture_mode, "app_version": app_version},
        "integrity": {"status": "passed", "algorithm": "sha256"},
        "preview": {"available": preview_available},
    }


def _preview_available(upload_session: CollectionUploadSession) -> bool:
    # Fixture sessions stand in for a completed preview worker in tests. They
    # must still exercise the same review gate as a real generated manifest.
    from data.config import settings
    from data.services.collection_upload_intake import collection_upload_staging_dir

    if settings.test_mode and (upload_session.result_json or {}).get("parse_fixture_mode"):
        return True

    manifest = (
        collection_upload_staging_dir(upload_session.id) / "media" / "preview" / "manifest.json"
    )
    return manifest.is_file() and not manifest.is_symlink()


def _fixture_objects(episode: Episode) -> list[dict[str, Any]]:
    """Deterministic manifest standing in for a completed worker in fixture mode."""
    from data.services.episode_objects import object_entry

    digest = hashlib.sha256(episode.source_fingerprint.encode()).hexdigest()
    base = f"fixture/{episode.episode_uid}"

    def ref(role: str, name: str) -> dict[str, Any]:
        return {
            "bucket_role": role,
            "object_key": f"{role}/{base}/{name}",
            "version_id": "fixture",
            "etag": "fixture",
            "size_bytes": 1,
            "sha256": digest,
        }

    return [
        object_entry(path="data.mcap", kind="data", ref=ref("raw", "data.mcap")),
        object_entry(path="metadata.json", kind="metadata", ref=ref("process", "metadata.json")),
        object_entry(
            path="admission-report.json",
            kind="admission_report",
            ref=ref("process", "admission-report.json"),
        ),
    ]


def _is_lerobot(declaration: dict[str, object], metadata: dict[str, object]) -> bool:
    candidates = (
        declaration.get("source_kind"),
        declaration.get("source_type"),
        declaration.get("format"),
        metadata.get("source_kind"),
        metadata.get("format"),
    )
    return any(
        isinstance(value, str) and value.strip().lower() == "lerobot" for value in candidates
    )


def _mark_parse_failed(db: Session, *, session_id: str, code: str) -> None:
    upload_session = db.scalar(
        select(CollectionUploadSession)
        .where(CollectionUploadSession.id == session_id)
        .with_for_update()
    )
    if upload_session is None:
        return
    package_ids = list(
        db.scalars(
            select(CollectionUploadSessionPackage.data_package_id).where(
                CollectionUploadSessionPackage.upload_session_id == session_id
            )
        )
    )
    db.execute(
        update(DataPackage)
        .where(
            DataPackage.id.in_(package_ids),
            DataPackage.status.in_(("pending_upload", "uploading", "parsing", "ingested")),
        )
        .values(
            status="parse_failed",
            parse_error_code=code[:64],
            parse_error_message=code[:512],
            updated_at=datetime.utcnow(),
        ),
        execution_options={"synchronize_session": False},
    )
    upload_session.status = "failed"
    upload_session.error_code = code[:64]
    upload_session.error_message = code[:512]
    upload_session.updated_at = datetime.utcnow()
    db.commit()


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise CollectionUploadParseError(f"{field}_invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise CollectionUploadParseError(f"{field}_invalid") from exc
    if parsed < 0:
        raise CollectionUploadParseError(f"{field}_invalid")
    return parsed


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()
