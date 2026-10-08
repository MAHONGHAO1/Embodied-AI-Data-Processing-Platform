"""QRDF admission orchestration for object-storage backed uploads.

This module deliberately contains no validation rules of its own.  The
vendored QRDF validator remains the source of truth; this adapter only
classifies its structured issues, creates previews, and persists process
artifacts through the provider-neutral storage interface.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from qrdf.validator.validator import QRDFValidator
from sqlalchemy import select

from data import bootstrap  # noqa: F401  # ensure the vendored SDK is importable
from data.database import EpisodeArtifact
from data.infra import oss_client
from data.infra.object_storage import StorageObjectRef
from data.integrations.qrdf.preview_export import mp4_is_browser_ready
from data.services.client_admission import NON_BLOCKING_ISSUE_CODES

BLOCKING_ISSUE_CODES = frozenset({"MISSING_EGO_RGB", "MCAP_UNREADABLE"})
# Deferred to batch-build QC: frame alignment and robot channel readiness must
# not block intake.  The same set is advertised to clients as
# ``non_blocking_issue_codes``.
TRAINING_READINESS_ISSUE_CODES = NON_BLOCKING_ISSUE_CODES


@dataclass(frozen=True)
class QRDFAdmissionResult:
    """Serializable conclusion of one source admission attempt."""

    ok: bool
    integrity_status: str
    preview_status: str
    output_verification_status: str
    report: dict[str, Any]
    issues: tuple[dict[str, Any], ...]
    qrdf_profile: str
    objects: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "integrity_status": self.integrity_status,
            "preview_status": self.preview_status,
            "output_verification_status": self.output_verification_status,
            "report": self.report,
            "issues": list(self.issues),
            "qrdf_profile": self.qrdf_profile,
            "objects": list(self.objects),
        }


def _record_verified_source_artifact(
    db: Any,
    *,
    episode_id: int,
    source: dict[str, Any],
    source_fingerprint: str,
) -> EpisodeArtifact | None:
    """Publish the verified raw object so fetch manifests can hand out URLs."""

    object_key = str(source.get("object_key") or "")
    checksum = str(source.get("sha256") or "")
    if not object_key or not checksum:
        return None
    bucket = oss_client.bucket_name(str(source.get("bucket_role") or "raw"))
    storage_uri = f"oss://{bucket}/{object_key}"
    existing = db.scalar(
        # storage_uri is globally unique: one verified object stays one artifact.
        select(EpisodeArtifact).where(EpisodeArtifact.storage_uri == storage_uri)
    )
    if existing is not None:
        return existing
    artifact = EpisodeArtifact(
        episode_id=episode_id,
        artifact_type="raw_source",
        storage_role="raw",
        storage_uri=storage_uri,
        checksum_sha256=checksum,
        size_bytes=int(source.get("size_bytes") or 0),
        manifest_hash=source_fingerprint,
        retention_policy="permanent",
        metadata_json={
            "source_protocol": "quicdata.collection.upload.v1",
            "relative_path": str(source.get("relative_path") or ""),
        },
    )
    db.add(artifact)
    db.flush()
    return artifact


def _issue_dict(issue: Any) -> dict[str, Any]:
    return {
        "severity": str(issue.severity),
        "code": str(issue.code),
        "message": str(issue.message),
        "path": issue.path,
        "topic": issue.topic,
    }


def _is_rgb_episode(episode: Any) -> bool:
    streams = list(getattr(episode, "list_streams", list)() or [])
    if any(getattr(stream, "rgb_topic", None) for stream in streams):
        return True
    topics = list(getattr(episode, "list_topics", list)() or [])
    return any("rgb" in str(topic).lower() or "image" in str(topic).lower() for topic in topics)


def _preview_artifacts_valid(
    episode_path: Path, episode: Any, *, expected_source: dict[str, str] | None = None
) -> tuple[bool, dict[str, Any]]:
    """Verify manifest, timeline and playable media after SDK generation."""
    manifest_path = episode_path / "media" / "preview" / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return False, {"error_code": "PREVIEW_MANIFEST_MISSING"}
    try:
        manifest = episode.load_rgb_preview_manifest()
        if manifest is None or not manifest.streams:
            return False, {"error_code": "PREVIEW_MANIFEST_INVALID"}
        # A manifest is only useful when it describes the exact source that
        # was validated.  The SDK owns the fingerprint calculation; we only
        # compare it with the bytes in the worker scratch directory.
        # Server mode fingerprints the scratch bytes; client-precheck mode has
        # no local MCAP and passes the declared digests instead.
        if expected_source is None:
            metadata_path = episode_path / "metadata.json"
            expected_source = {
                "data_mcap_sha256": _sha256_file(Path(episode.mcap_path)),
                "metadata_sha256": _sha256_file(metadata_path),
            }
        source = manifest.source.model_dump(mode="json")
        if source != expected_source:
            return False, {"error_code": "PREVIEW_MEDIA_STALE"}
        required_topics = {
            str(stream.rgb_topic)
            for stream in (episode.list_streams() or [])
            if getattr(stream, "rgb_topic", None)
        }
        manifest_topics = {str(stream.topic) for stream in manifest.streams}
        if required_topics and not required_topics.issubset(manifest_topics):
            return False, {"error_code": "PREVIEW_STREAM_MISSING"}
        if required_topics and not required_topics.issubset(
            set(manifest.parameters.requested_topics)
        ):
            return False, {"error_code": "PREVIEW_MANIFEST_INVALID"}
        streams: list[dict[str, Any]] = []
        for stream in manifest.streams:
            # SDK timestamp/PTS drops are valid timeline entries. Corrupt RGB
            # frames still block preview admission, even in a playable stream.
            permitted_drops = {
                "duplicate_timestamp",
                "timestamp_decreased",
                "unrepresentable_pts",
            }
            if stream.status not in {"complete", "partial"} or (
                stream.status == "partial"
                and not set(stream.failure_reasons).issubset(permitted_drops)
            ):
                return False, {
                    "error_code": "PREVIEW_STREAM_INCOMPLETE",
                    "topic": stream.topic,
                }
            if not stream.video_path or not stream.timeline_path:
                return False, {
                    "error_code": "PREVIEW_ARTIFACT_MISSING",
                    "topic": stream.topic,
                }
            video = (episode_path / "media" / "preview" / stream.video_path).resolve()
            timeline = (episode_path / "media" / "preview" / stream.timeline_path).resolve()
            preview_root = (episode_path / "media" / "preview").resolve()
            if preview_root not in video.parents or preview_root not in timeline.parents:
                return False, {
                    "error_code": "PREVIEW_ARTIFACT_OUTSIDE_ROOT",
                    "topic": stream.topic,
                }
            if not video.is_file() or not timeline.is_file() or not mp4_is_browser_ready(video):
                return False, {
                    "error_code": "PREVIEW_MEDIA_UNREADABLE",
                    "topic": stream.topic,
                }
            try:
                timeline_payload = json.loads(timeline.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return False, {
                    "error_code": "PREVIEW_TIMELINE_INVALID",
                    "topic": stream.topic,
                }
            if not isinstance(timeline_payload, dict) or not isinstance(
                timeline_payload.get("entries"), list
            ):
                return False, {
                    "error_code": "PREVIEW_TIMELINE_INVALID",
                    "topic": stream.topic,
                }
            entries = timeline_payload["entries"]
            previous_timestamp: int | None = None
            previous_pts: int | None = None
            first_timestamp = None
            encoded_count = 0
            dropped_count = 0
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("timestamp_ns"), str):
                    return False, {
                        "error_code": "PREVIEW_TIMELINE_INVALID",
                        "topic": stream.topic,
                    }
                timestamp = entry["timestamp_ns"]
                if not timestamp.isdecimal():
                    return False, {
                        "error_code": "PREVIEW_TIMELINE_INVALID",
                        "topic": stream.topic,
                    }
                timestamp_value = int(timestamp)
                # SDK dropped entries may share a timestamp. Preserve source
                # timestamp_ns exactly, while requiring the encoded frame
                # sequence itself to be ordered.
                if entry.get("kind") == "frame":
                    if previous_timestamp is not None and timestamp_value <= previous_timestamp:
                        return False, {
                            "error_code": "PREVIEW_TIMELINE_INVALID",
                            "topic": stream.topic,
                        }
                    previous_timestamp = timestamp_value
                    if first_timestamp is None:
                        first_timestamp = timestamp
                    encoded_count += 1
                    if (
                        not isinstance(entry.get("video_pts_us"), str)
                        or not entry["video_pts_us"].isdecimal()
                    ):
                        return False, {
                            "error_code": "PREVIEW_TIMELINE_INVALID",
                            "topic": stream.topic,
                        }
                    frame_index = entry.get("frame_index")
                    if not isinstance(frame_index, int) or isinstance(frame_index, bool):
                        return False, {
                            "error_code": "PREVIEW_TIMELINE_INVALID",
                            "topic": stream.topic,
                        }
                    if frame_index != encoded_count - 1:
                        return False, {
                            "error_code": "PREVIEW_TIMELINE_INVALID",
                            "topic": stream.topic,
                        }
                    pts = int(entry["video_pts_us"])
                    if previous_pts is not None and pts <= previous_pts:
                        return False, {
                            "error_code": "PREVIEW_TIMELINE_INVALID",
                            "topic": stream.topic,
                        }
                    previous_pts = pts
                elif entry.get("kind") == "dropped":
                    if entry.get("reason") not in permitted_drops:
                        return False, {
                            "error_code": "PREVIEW_TIMELINE_INVALID",
                            "topic": stream.topic,
                        }
                    dropped_count += 1
                else:
                    return False, {
                        "error_code": "PREVIEW_TIMELINE_INVALID",
                        "topic": stream.topic,
                    }
            if encoded_count != int(stream.encoded_frame_count):
                return False, {
                    "error_code": "PREVIEW_TIMELINE_INVALID",
                    "topic": stream.topic,
                }
            if stream.topic != timeline_payload.get("topic"):
                return False, {
                    "error_code": "PREVIEW_TIMELINE_INVALID",
                    "topic": stream.topic,
                }
            source_frame_count = getattr(stream, "source_frame_count", None)
            dropped_frame_count = getattr(stream, "dropped_frame_count", None)
            if (
                source_frame_count is not None
                and int(source_frame_count) != encoded_count + dropped_count
            ):
                return False, {
                    "error_code": "PREVIEW_TIMELINE_INVALID",
                    "topic": stream.topic,
                }
            if dropped_frame_count is not None and int(dropped_frame_count) != dropped_count:
                return False, {
                    "error_code": "PREVIEW_TIMELINE_INVALID",
                    "topic": stream.topic,
                }
            if (
                not encoded_count
                or stream.first_timestamp_ns != first_timestamp
                or stream.last_timestamp_ns != str(previous_timestamp)
                or (stream.status == "complete") != (dropped_count == 0)
            ):
                return False, {
                    "error_code": "PREVIEW_TIMELINE_INVALID",
                    "topic": stream.topic,
                }
            streams.append(
                {
                    "topic": stream.topic,
                    "video": video.relative_to(preview_root).as_posix(),
                    "timeline": timeline.relative_to(preview_root).as_posix(),
                    "frame_count": encoded_count,
                    "dropped_frame_count": dropped_count,
                }
            )
        return True, {
            "artifact_id": manifest.artifact_id,
            "generator_version": manifest.generator_version,
            "source": source,
            "parameters": manifest.parameters.model_dump(mode="json"),
            "streams": streams,
        }
    except Exception:  # noqa: BLE001 - SDK manifest errors are source-local conclusions.
        return False, {"error_code": "PREVIEW_MANIFEST_INVALID"}


def _report_payload(report: Any) -> dict[str, Any]:
    issues = [_issue_dict(issue) for issue in report.issues]
    return {
        "ok": bool(report.ok),
        "error_count": int(report.error_count),
        "warning_count": int(report.warning_count),
        "issues": issues,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _qrdf_runtime_facts() -> dict[str, str]:
    """Return SDK identity without making a development checkout a dependency."""
    import qrdf
    from qrdf.version import QRDF_VERSION

    commit = ""
    vendor_root = Path(getattr(qrdf, "__file__", "")).resolve().parent.parent
    git_dir = vendor_root / ".git"
    if git_dir.exists():
        try:
            commit = subprocess.check_output(
                ["git", "-C", str(vendor_root), "rev-parse", "HEAD"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=2,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            commit = ""
    digest = hashlib.sha256()
    package_root = vendor_root / "qrdf"
    for file in sorted(package_root.rglob("*")):
        if (
            file.is_file()
            and "__pycache__" not in file.parts
            and file.suffix != ".pyc"
            and file.name != ".DS_Store"
        ):
            digest.update(file.relative_to(package_root).as_posix().encode() + b"\0")
            digest.update(file.read_bytes())
    package_sha256 = digest.hexdigest()
    if not commit:
        # This pinned manifest was verified against the source checkout before
        # vendoring. A different deployment SDK must supply new provenance;
        # never substitute the QuicStudio repository's HEAD for QRDF's commit.
        provenance = json.loads(Path(__file__).with_name("sdk_provenance.json").read_text())
        if provenance["package_sha256"] != package_sha256:
            raise AdmissionSourceError("QRDF_PROVENANCE_MISMATCH")
        commit = provenance["source_commit"]
    return {
        "sdk_version": str(QRDF_VERSION),
        "source_commit": commit,
        "package_sha256": package_sha256,
    }


def _publish_process_objects(
    path: Path,
    report_path: Path,
    media_facts: dict[str, Any],
    process_provider: Any,
) -> list[dict[str, Any]]:
    """Upload metadata, report and preview files as individual process objects."""
    from data.services.episode_objects import object_entry

    topics_by_file: dict[str, tuple[str, str]] = {}
    for stream in media_facts.get("streams", []):
        topics_by_file[stream["video"]] = (stream["topic"], "preview_video")
        topics_by_file[stream["timeline"]] = (stream["topic"], "preview_timeline")
    files: list[tuple[Path, str, str, str | None]] = [
        (path / "metadata.json", "metadata.json", "metadata", None),
        (report_path, "admission-report.json", "admission_report", None),
    ]
    preview_root = path / "media" / "preview"
    if media_facts.get("streams"):
        files.append(
            (
                preview_root / "manifest.json",
                "media/preview/manifest.json",
                "preview_manifest",
                None,
            )
        )
        for relative, (topic, kind) in sorted(topics_by_file.items()):
            files.append((preview_root / relative, f"media/preview/{relative}", kind, topic))

    prefix = f"process/v2/qrdf/{uuid4().hex}"
    entries: list[dict[str, Any]] = []
    for local, relative, kind, topic in files:
        sha256 = _sha256_file(local)
        ref = StorageObjectRef(
            "process", f"{prefix}/{relative}", None, "", local.stat().st_size, sha256
        )
        persisted = process_provider.put_worker_object(ref, str(local))
        entries.append(
            object_entry(
                path=relative,
                kind=kind,
                topic=topic,
                ref={**asdict(persisted), "sha256": sha256},
            )
        )
    return entries


def admit_qrdf_episode(
    episode_path: str | Path,
    *,
    source_fingerprint: str = "",
    process_provider: Any | None = None,
    validation_policy_version: str = "v1",
    generate_preview: bool = True,
    validation_representation: str | None = None,
    preview_topics: list[str] | None = None,
    preview_max_edge: int = 1280,
    preview_keyframe_interval_s: float = 1.0,
    report_extra: dict[str, Any] | None = None,
) -> QRDFAdmissionResult:
    """Validate one materialized QRDF episode and optionally publish process output.

    Validation and preview generation run entirely within ``episode_path``.  A
    process object is uploaded only after all required checks have passed; a
    failed source therefore cannot poison another source in the same package.
    """
    del validation_policy_version  # retained in the caller's persisted report
    path = Path(episode_path).resolve()
    validator = QRDFValidator()
    report_obj = validator.validate_episode(
        path,
        validation_representation=validation_representation,
    )
    issues = [_issue_dict(issue) for issue in report_obj.issues]
    structural_block = any(
        item["severity"] == "ERROR" and item["code"] not in TRAINING_READINESS_ISSUE_CODES
        for item in issues
    )
    # The explicit blocking set documents the contract while all unknown ERRORs
    # remain fail-closed.  Training readiness errors are retained but do not
    # make structure admission fail.
    structural_block = any(
        item["severity"] == "ERROR"
        and (
            item["code"] in BLOCKING_ISSUE_CODES
            or item["code"] not in TRAINING_READINESS_ISSUE_CODES
        )
        for item in issues
    )
    episode = None
    preview_status = "not_applicable"
    output_status = "pending"
    objects: list[dict[str, Any]] = []
    media_facts: dict[str, Any] = {}
    if not structural_block:
        try:
            from qrdf.reader.episode import Episode

            episode = Episode(path)
            if _is_rgb_episode(episode):
                preview_status = "running"
                if generate_preview:
                    episode.generate_rgb_previews(
                        camera_topics=preview_topics,
                        max_edge=preview_max_edge,
                        keyframe_interval_s=preview_keyframe_interval_s,
                    )
                valid, media_facts = _preview_artifacts_valid(path, episode)
                preview_status = "ready" if valid else "failed"
            else:
                preview_status = "not_applicable"
                media_facts = {"status": "not_applicable"}
            output_status = (
                "verified" if preview_status in {"ready", "not_applicable"} else "failed"
            )
        except Exception as exc:  # noqa: BLE001 - SDK encoders expose several error families.
            preview_status = "failed"
            output_status = "failed"
            media_facts = {
                "error_code": "PREVIEW_GENERATION_FAILED",
                "error_type": type(exc).__name__,
            }
    else:
        output_status = "failed"

    admission_ok = not structural_block and preview_status in {
        "ready",
        "not_applicable",
    }
    if admission_ok and process_provider is None:
        # Preview generation can be inspected by a pure adapter test, but a
        # worker fact is not output-verified until the process object has been
        # uploaded and HEAD-verified by the storage provider.
        output_status = "pending"
    # Run the media checks after generation as well.  QRDF intentionally emits
    # media findings as warnings; preserving them in the report lets review
    # distinguish a stale/undecodable preview from a structural MCAP failure.
    if not structural_block:
        try:
            media_report_obj = validator.validate_episode(
                path,
                check_media=True,
                validation_representation=validation_representation,
            )
            media_report = _report_payload(media_report_obj)
        except Exception as exc:  # noqa: BLE001 - preserve media validation failures in the report.
            media_report = {
                "ok": False,
                "issues": [
                    {
                        "severity": "ERROR",
                        "code": "MEDIA_VALIDATION_FAILED",
                        "message": str(exc),
                        "path": str(path),
                        "topic": None,
                    }
                ],
            }
    else:
        media_report = None
    runtime_facts = _qrdf_runtime_facts()
    report = _report_payload(report_obj)
    report["media"] = media_facts
    report["media_validation"] = media_report
    report["qrdf_runtime"] = runtime_facts
    report["source_fingerprint"] = source_fingerprint
    report.update(report_extra or {})
    if admission_ok and process_provider is not None:
        report_path = path / ".qrdf-admission-report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        try:
            objects = _publish_process_objects(path, report_path, media_facts, process_provider)
        finally:
            report_path.unlink(missing_ok=True)
    return QRDFAdmissionResult(
        ok=admission_ok,
        integrity_status="passed" if not structural_block else "failed",
        preview_status=preview_status,
        output_verification_status=output_status,
        report=report,
        issues=tuple(issues),
        qrdf_profile="qrdf",
        objects=tuple(objects),
    )


run_qrdf_admission = admit_qrdf_episode
validate_and_preview_episode = admit_qrdf_episode


def _safe_extract_tar(archive: Path, destination: Path, *, max_bytes: int = 2 * 1024**3) -> Path:
    """Extract a worker object without path traversal, links, or budget bypass."""
    destination.mkdir(parents=True, exist_ok=True)
    written = 0
    with tarfile.open(archive, "r:*") as tar:
        members = tar.getmembers()
        for member in members:
            name = Path(member.name)
            target = (destination / name).resolve()
            try:
                target.relative_to(destination.resolve())
            except ValueError as exc:
                raise ValueError("QRDF worker object escapes scratch directory") from exc
            if member.issym() or member.islnk() or member.isdev():
                raise ValueError("QRDF worker object contains a symbolic link")
            if member.isfile():
                written += int(member.size)
                if written > max_bytes:
                    raise ValueError("QRDF worker object exceeds scratch budget")
                source = tar.extractfile(member)
                if source is None:
                    raise ValueError("QRDF worker object member cannot be read")
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
            elif member.isdir():
                target.mkdir(parents=True, exist_ok=True)
    return destination


def _find_episode_root(root: Path) -> Path:
    """Locate a QRDF metadata root while retaining archive relative paths."""
    metadata = [
        candidate for candidate in root.rglob("metadata.json") if not candidate.is_symlink()
    ]
    if len(metadata) != 1:
        raise ValueError("QRDF worker object must contain exactly one metadata.json")
    episode_root = metadata[0].parent.resolve()
    try:
        episode_root.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("QRDF metadata escapes scratch directory") from exc
    return episode_root


def run_qrdf_admission_worker(db: Any, job: Any) -> dict[str, Any]:
    """Claim briefly, do object/SDK I/O without a transaction, then fence commit."""
    from sqlalchemy import func, select

    from data.config import settings
    from data.database import Episode, JobRun
    from data.infra.storage_provider import get_storage_provider
    from data.models.collection_upload import CollectionUploadSession
    from data.models.data_package import DataPackage, PackageIntakeReview
    from data.services.collection_upload_parse import refresh_upload_admission_counts
    from data.services.episode_admission import (
        current_episode_admission_fact,
        record_episode_admission_fact,
    )
    from data.services.episode_objects import object_entry
    from data.services.job_runs import LeaseOwnershipLost, NonRetryableJobError

    job_id, token, worker_id = job.id, job.lease_token, job.lease_worker_id
    delivery_attempt = job.attempt_count
    detail = dict(job.detail_json or {})
    if set(detail) - {"upload_session_id", "source_id"}:
        db.rollback()
        raise ValueError("admission job overrides are forbidden")
    session_id, source_id = detail["upload_session_id"], detail["source_id"]
    episode_id = int(job.resource_id)

    def fence(*, claimed: bool):
        locked_job = db.scalar(
            select(JobRun)
            .where(
                JobRun.id == job_id,
                JobRun.kind == "collection_upload_admission",
                JobRun.resource_type == "episode",
                JobRun.resource_id == str(episode_id),
                JobRun.status == "running",
                JobRun.lease_token == token,
                JobRun.lease_worker_id == worker_id,
                JobRun.attempt_count == delivery_attempt,
                JobRun.lease_expires_at > func.clock_timestamp(),
            )
            .with_for_update()
        )
        if not token or locked_job is None:
            db.rollback()
            raise LeaseOwnershipLost("admission job lease lost")
        session = db.scalar(
            select(CollectionUploadSession)
            .where(CollectionUploadSession.id == session_id)
            .with_for_update()
        )
        state = dict((session.result_json.get("admission_sources") or {}).get(source_id) or {})
        probe = db.get(Episode, episode_id)
        if probe is not None and probe.data_package_id is not None:
            db.scalar(
                select(DataPackage).where(DataPackage.id == probe.data_package_id).with_for_update()
            )
            if (
                db.query(PackageIntakeReview.id)
                .filter(PackageIntakeReview.data_package_id == probe.data_package_id)
                .first()
            ):
                raise NonRetryableJobError("package_intake_finalized")
        episode = db.scalar(select(Episode).where(Episode.id == episode_id).with_for_update())
        if (
            state.get("job_id") != job_id
            or state.get("episode_id") != episode_id
            or episode is None
            or episode.source_fingerprint != state.get("source_fingerprint")
        ):
            db.rollback()
            raise LeaseOwnershipLost("admission source identity changed")
        if claimed and (
            state.get("lease_token") != token
            or state.get("attempt") != attempt
            or state.get("source_object") != source_object
        ):
            db.rollback()
            raise LeaseOwnershipLost("admission source attempt changed")
        current = current_episode_admission_fact(db, episode_id=episode_id, for_update=True)
        if claimed and current is not None and current.attempt >= attempt:
            db.rollback()
            raise LeaseOwnershipLost("newer admission conclusion exists")
        return session, state, current

    session, state, current = fence(claimed=False)
    if state.get("status") in {"ready", "failed"}:
        db.commit()
        return {"episode_id": episode_id, "status": state["status"]}
    if state.get("status") == "running" and state.get("lease_token") == token:
        db.rollback()
        raise LeaseOwnershipLost("source attempt is already executing")
    attempt = max(int(state.get("attempt") or 0), current.attempt if current else 0) + 1
    source_object = dict(state["source_object"])
    source_fingerprint = state["source_fingerprint"]
    state.update(status="running", attempt=attempt, lease_token=token)
    session.result_json = {
        **session.result_json,
        "admission_sources": {
            **session.result_json["admission_sources"],
            source_id: state,
        },
    }
    db.commit()

    def check_owned():
        fence(claimed=True)
        db.commit()

    provider = None
    result = None
    uploaded_objects = []
    verified_source = dict(source_object)
    error_code = ""
    integrity_source = "server"
    report_extra: dict[str, Any] = {"integrity_source": "server"}
    try:
        # No job-supplied filesystem paths or artifact keys are accepted.
        parent = Path(settings.storage_root).absolute() / "admission-scratch"
        for component in [parent, *parent.parents]:
            if component.is_symlink():
                raise AdmissionSourceError("SOURCE_SCRATCH_UNSAFE")
        parent.mkdir(parents=True, exist_ok=True)
        data_path = Path(state["data_path"])
        if (
            data_path.is_absolute()
            or ".." in data_path.parts
            or "\\" in str(data_path)
            or str(data_path) == "metadata.json"
        ):
            raise AdmissionSourceError("SOURCE_PATH_UNSAFE")
        metadata = json.loads(state["metadata_text"])
        if metadata.get("data_file") != data_path.as_posix():
            raise AdmissionSourceError("SOURCE_PATH_UNSAFE")
        ref = StorageObjectRef(**source_object)
        from data.services.import_intake import MAX_IMPORT_TOTAL_BYTES

        if ref.bucket_role != "raw" or not ref.object_key or not (ref.version_id or ref.etag):
            raise AdmissionSourceError("SOURCE_IDENTITY_INVALID")
        if not 0 < ref.size_bytes <= MAX_IMPORT_TOTAL_BYTES:
            raise AdmissionSourceError("SOURCE_SIZE_INVALID")
        provider = get_storage_provider()

        class FencedProvider:
            def put_worker_object(self, ref, path):
                check_owned()
                try:
                    persisted = provider.put_worker_object(ref, path)
                except Exception as exc:
                    raise AdmissionSourceError("PROCESS_UPLOAD_FAILED") from exc
                uploaded_objects.append(persisted)
                return persisted

        client_outcome = None
        if state.get("client_admission"):
            from data.integrations.qrdf.client_admission import (
                ClientAdmissionFallback,
                admit_client_prechecked,
            )

            with tempfile.TemporaryDirectory(prefix="client-", dir=parent) as client_temporary:
                try:
                    client_outcome = admit_client_prechecked(
                        Path(client_temporary),
                        state=state,
                        source_ref=ref,
                        source_fingerprint=source_fingerprint,
                        provider=provider,
                        process_provider=FencedProvider(),
                        check_owned=check_owned,
                    )
                except ClientAdmissionFallback as fallback:
                    for persisted in uploaded_objects:
                        provider.delete_exact(persisted)
                    uploaded_objects.clear()
                    report_extra["client_admission_fallback"] = fallback.reason
                    if fallback.detail:
                        report_extra["client_admission_fallback_detail"] = fallback.detail
        if client_outcome is not None:
            result, verified_source = client_outcome
            integrity_source = "client"
        else:
            with tempfile.TemporaryDirectory(prefix="source-", dir=parent) as temporary:
                root = Path(temporary)
                check_owned()
                destination = root / data_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    downloaded = provider.download_file(ref, str(destination))
                except Exception as exc:
                    raise AdmissionSourceError("SOURCE_DOWNLOAD_FAILED") from exc
                if (
                    downloaded.object_key != ref.object_key
                    or downloaded.bucket_role != ref.bucket_role
                    or (ref.version_id and downloaded.version_id != ref.version_id)
                    or (ref.etag and downloaded.etag != ref.etag)
                    or downloaded.size_bytes != ref.size_bytes
                    or destination.stat().st_size != ref.size_bytes
                    or destination.is_symlink()
                ):
                    raise AdmissionSourceError("SOURCE_IDENTITY_CHANGED")
                verified_source = {
                    **asdict(downloaded),
                    "sha256": _sha256_file(destination),
                    "relative_path": data_path.as_posix(),
                }
                if verified_source["sha256"] != state["declared_sha256"]:
                    raise AdmissionSourceError("SOURCE_DECLARED_HASH_MISMATCH")
                (root / "metadata.json").write_text(state["metadata_text"], encoding="utf-8")
                check_owned()
                result = admit_qrdf_episode(
                    root,
                    source_fingerprint=source_fingerprint,
                    process_provider=FencedProvider(),
                    report_extra=report_extra,
                )
    except LeaseOwnershipLost:
        for persisted in uploaded_objects:
            provider.delete_exact(persisted)
        raise
    except Exception as exc:  # noqa: BLE001 - persist one failed source, not a failed session.
        for persisted in uploaded_objects:
            provider.delete_exact(persisted)
        uploaded_objects.clear()
        error_code = getattr(exc, "code", "SOURCE_PROCESSING_FAILED")
        integrity_source = "server"
        result = QRDFAdmissionResult(
            ok=False,
            integrity_status="failed",
            preview_status="failed",
            output_verification_status="failed",
            report={"error_code": error_code, "error_type": type(exc).__name__, **report_extra},
            issues=(),
            qrdf_profile="qrdf",
        )
    try:
        session, latest, _ = fence(claimed=True)
        error_code = error_code or str(result.report.get("media", {}).get("error_code") or "")
        error_code = error_code or next(
            (
                str(i["code"])
                for i in result.issues
                if i["severity"] == "ERROR" and i["code"] not in TRAINING_READINESS_ISSUE_CODES
            ),
            "",
        )
        record_episode_admission_fact(
            db,
            episode_id=episode_id,
            attempt=attempt,
            source_fingerprint=source_fingerprint,
            validation_policy_version="v1",
            integrity_status=result.integrity_status,
            preview_status=result.preview_status,
            output_verification_status=result.output_verification_status,
            qrdf_profile=result.qrdf_profile,
            report_ref=result.report,
            objects=[
                object_entry(
                    path=verified_source["relative_path"], kind="data", ref=verified_source
                ),
                *result.objects,
            ]
            if result.output_verification_status == "verified"
            else [],
            error_code=error_code,
            integrity_source=integrity_source,
        )
        if result.output_verification_status == "verified":
            # External tools fetch the exact verified bytes through the fetch
            # manifest, which reads published Episode artifacts.
            _record_verified_source_artifact(
                db,
                episode_id=episode_id,
                source=verified_source,
                source_fingerprint=source_fingerprint,
            )
        latest.update(
            status="ready" if result.output_verification_status == "verified" else "failed",
            error_code=error_code,
            integrity_source=integrity_source,
        )
        if report_extra.get("client_admission_fallback"):
            latest["client_admission_fallback"] = report_extra["client_admission_fallback"]
        session.result_json = {
            **session.result_json,
            "admission_sources": {
                **session.result_json["admission_sources"],
                source_id: latest,
            },
        }
        refresh_upload_admission_counts(db, session)
        db.commit()
    except Exception:
        db.rollback()
        for persisted in uploaded_objects:
            provider.delete_exact(persisted)
        raise
    return {"episode_id": episode_id, "attempt": attempt, "admission": result.to_dict()}


class AdmissionSourceError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


__all__ = [
    "BLOCKING_ISSUE_CODES",
    "TRAINING_READINESS_ISSUE_CODES",
    "QRDFAdmissionResult",
    "admit_qrdf_episode",
    "run_qrdf_admission",
    "run_qrdf_admission_worker",
    "validate_and_preview_episode",
]
