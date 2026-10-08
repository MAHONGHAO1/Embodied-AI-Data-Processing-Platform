"""Durable catalog export materialization and verified object delivery."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tarfile
import tempfile
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from data.config import settings
from data.database import JobRun
from data.infra.object_storage import StorageNotReady, StorageObjectRef
from data.infra.storage_provider import get_storage_provider
from data.models.catalog_dataset import CatalogDatasetExport, CatalogDatasetVersion
from data.services.job_runs import (
    NonRetryableJobError,
    create_or_get_job_in_transaction,
)


class CatalogExportError(NonRetryableJobError):
    """A terminal failure of one immutable catalog export attempt."""


def _export_detail(
    output: Path, *, export_format: str, size_bytes: int, sha256: str, file_count: int
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "media_type": "application/gzip",
        "extension": ".tar.gz",
        "manifest_summary": {
            "file_count": file_count,
            "size_bytes": size_bytes,
            "sha256": sha256,
        },
    }
    if export_format == "lerobot_3_0":
        info_path = output / "meta" / "info.json"
        if not info_path.is_file() or info_path.stat().st_size > 1024 * 1024:
            raise CatalogExportError("LeRobot info.json is missing or too large")
        raw = info_path.read_bytes()
        info_json = raw.decode("utf-8")
        if not isinstance(json.loads(info_json), dict):
            raise CatalogExportError("LeRobot info.json must be an object")
        detail["lerobot_metadata"] = {
            "schema": "quicstudio.lerobot-export-metadata.v1",
            "archive_sha256": sha256,
            "info_sha256": hashlib.sha256(raw).hexdigest(),
            "info_json": info_json,
        }
    return detail


def catalog_export_download(
    db: Session,
    *,
    export_id: int,
    url_ttl_seconds: int = 3600,
    oss_network: str = "internal",
) -> dict:
    """Sign only an existing, verified delivery; never materialize on a GET."""
    if oss_network not in {"internal", "public"} or not 1 <= url_ttl_seconds <= 604800:
        raise ValueError("invalid export download options")
    export = db.get(CatalogDatasetExport, export_id)
    if export is None:
        raise LookupError("catalog export does not exist")
    if export.status != "succeeded":
        raise CatalogExportError("catalog export is not ready for download")
    try:
        ref = StorageObjectRef(**(export.manifest_json or {})["artifact"])
        _require_complete_identity(ref)
    except (KeyError, TypeError, ValueError) as exc:
        raise CatalogExportError("catalog export has no verified artifact reference") from exc
    if (
        ref.bucket_role != "export"
        or ref.object_key != f"{export.output_prefix}/dataset.tar.gz"
        or ref.size_bytes != export.size_bytes
        or ref.sha256 != export.sha256
    ):
        raise CatalogExportError("catalog export artifact identity differs from its delivery")
    endpoint = (
        settings.storage_endpoint
        if oss_network == "internal"
        else (settings.storage_browser_endpoint or settings.oss_browser_endpoint)
    )
    if not endpoint or not endpoint.strip():
        raise StorageNotReady(f"oss_{oss_network}_endpoint_not_configured")
    # The selected endpoint participates in signing; signed URLs are never rewritten.
    provider = get_storage_provider(
        settings.model_copy(update={"storage_browser_endpoint": endpoint})
    )
    _assert_identity(ref, provider.head(ref))
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=url_ttl_seconds)
    return {
        "export_id": export.id,
        "version_id": export.version_id,
        "format": export.format,
        "url": provider.sign_get(ref, expires=url_ttl_seconds),
        "expires_at": expires_at.isoformat(),
        "url_ttl_seconds": url_ttl_seconds,
        "oss_network": oss_network,
        "size_bytes": ref.size_bytes,
        "sha256": ref.sha256,
        "media_type": "application/gzip",
        "filename": f"catalog-version-{export.version_id}-{export.format}.tar.gz",
    }


def _attempt_number(db: Session, version_id: int, export_format: str) -> int:
    value = db.scalar(
        select(func.max(CatalogDatasetExport.attempt)).where(
            CatalogDatasetExport.version_id == version_id,
            CatalogDatasetExport.format == export_format,
        )
    )
    return int(value or 0) + 1


def enqueue_catalog_export(
    db: Session,
    *,
    version_id: int,
    export_format: str,
    idempotency_key: str,
    actor_id: int | None = None,
    snapshot_id: str | None = None,
    snapshot_json: Mapping[str, Any] | None = None,
):
    """Create one queued export and JobRun in the caller's transaction."""
    version = db.get(CatalogDatasetVersion, version_id)
    if version is None:
        raise LookupError("catalog dataset version does not exist")
    if export_format not in {"qrdf_0_2", "lerobot_3_0"}:
        raise CatalogExportError("invalid export format")
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 255:
        raise ValueError("idempotency_key must be non-empty and at most 255 characters")
    existing = db.scalar(
        select(CatalogDatasetExport).where(CatalogDatasetExport.idempotency_key == key)
    )
    if existing is not None:
        if existing.version_id != version_id or existing.format != export_format:
            raise ValueError("idempotency_key is already used for another export")
        job = db.scalar(
            select(JobRun).where(
                JobRun.resource_type == "artifact",
                JobRun.resource_id == str(existing.id),
                JobRun.kind == "catalog_export",
            )
        )
        return existing, job, False

    frozen_snapshot = deepcopy(
        dict(snapshot_json if snapshot_json is not None else (version.source_snapshot_json or {}))
    )
    frozen_id = str(snapshot_id or _snapshot_hash(frozen_snapshot))
    if frozen_id != _snapshot_hash(frozen_snapshot):
        raise ValueError("catalog source snapshot hash does not match content")
    attempt = _attempt_number(db, version_id, export_format)
    prefix = f"catalog/{version.dataset_id}/v{version.version}/{export_format}/attempt-{attempt}-{uuid4().hex}"
    export = CatalogDatasetExport(
        version_id=version_id,
        format=export_format,
        status="queued",
        idempotency_key=key,
        input_snapshot_id=frozen_id,
        input_snapshot_json=frozen_snapshot,
        attempt=attempt,
        output_prefix=prefix,
        output_plan_json={
            "media_type": "application/gzip",
            "extension": ".tar.gz",
            "prefix": prefix,
            "input_snapshot_id": frozen_id,
        },
    )
    db.add(export)
    db.flush()
    job = create_or_get_job_in_transaction(
        db,
        kind="catalog_export",
        resource_type="artifact",
        resource_id=str(export.id),
        idempotency_key=f"catalog-export:{key}",
        queue="export",
        actor_id=actor_id,
        detail={
            "export_id": export.id,
            "version_id": version_id,
            "format": export_format,
            "attempt": attempt,
            "input_snapshot_id": version.source_snapshot_id,
        },
    )
    return export, job, True


def retry_catalog_export(db: Session, export_id: int, *, actor_id: int | None = None):
    """Preserve a failed attempt and explicitly create a new output attempt."""
    source = db.get(CatalogDatasetExport, export_id)
    if source is None:
        raise LookupError("catalog export does not exist")
    if source.status != "failed":
        raise ValueError("only failed catalog exports may be retried")
    return enqueue_catalog_export(
        db,
        version_id=source.version_id,
        export_format=source.format,
        idempotency_key=f"{source.idempotency_key}:retry:{int(source.attempt) + 1}",
        actor_id=actor_id,
        snapshot_id=source.input_snapshot_id,
        snapshot_json=source.input_snapshot_json or {},
    )


def run_catalog_export(db: Session, job) -> dict[str, Any]:
    """Materialize, validate, upload, and persist one export attempt."""
    export_id = int((job.detail_json or {}).get("export_id") or job.resource_id)
    export = db.get(CatalogDatasetExport, export_id)
    if export is None:
        raise NonRetryableJobError("catalog export row is missing")
    if export.status == "succeeded" and export.oss_uri:
        return _result(export)
    version = db.get(CatalogDatasetVersion, export.version_id)
    if version is None:
        raise NonRetryableJobError("catalog export version is missing")
    export.status = "running"
    export.error_code = ""
    export.error_message = ""
    db.flush()
    uploaded: list[StorageObjectRef] = []
    planned_refs = [
        StorageObjectRef("export", f"{export.output_prefix}/dataset.tar.gz", None, "", 0, None),
        StorageObjectRef("export", f"{export.output_prefix}/completion.json", None, "", 0, None),
    ]
    export.cleanup_json = {"planned_refs": [asdict(ref) for ref in planned_refs]}
    root: Path | None = None
    provider: Any | None = None
    try:
        if export.input_snapshot_id != _snapshot_hash(export.input_snapshot_json or {}):
            raise CatalogExportError("frozen source snapshot hash mismatch")
        provider = get_storage_provider()
        root = _make_scratch_root()
        output = root / "dataset"
        archive = root / "dataset.tar.gz"
    except Exception as exc:
        export.status = "failed"
        export.error_code = "catalog_export_failed"
        export.error_message = str(exc)[:1000]
        db.flush()
        if isinstance(exc, NonRetryableJobError):
            raise
        raise CatalogExportError(str(exc) or "catalog export setup failed") from exc
    try:
        output.mkdir()
        source_kind = str(getattr(version.dataset, "source_kind", "qrdf_assets"))
        if export.format == "lerobot_3_0" and source_kind == "qrdf_assets":
            qrdf_source = root / "qrdf-source"
            _materialize_snapshot(
                export.input_snapshot_json or {},
                qrdf_source,
                provider,
                source_kind=source_kind,
                export_format="qrdf_0_2",
            )
            _convert_qrdf_to_lerobot(qrdf_source, output)
        else:
            _materialize_snapshot(
                export.input_snapshot_json or {},
                output,
                provider,
                source_kind=source_kind,
                export_format=export.format,
            )
        _enforce_scratch_budget(root)
        _validate_materialized(output, export.format)
        _write_export_manifest(output, version, export)
        with tarfile.open(archive, "w:gz") as handle:
            for path in sorted(output.rglob("*")):
                if path.is_symlink():
                    raise CatalogExportError("export contains a symlink")
                if path.is_file():
                    handle.add(path, arcname=path.relative_to(output).as_posix())
        _enforce_scratch_budget(root)
        size, digest = archive.stat().st_size, _sha256_file(archive)
        artifact_ref = StorageObjectRef(
            "export", f"{export.output_prefix}/dataset.tar.gz", None, "", size, digest
        )
        try:
            persisted = provider.put_worker_object(artifact_ref, str(archive))
        except Exception as put_exc:
            returned_ref = getattr(put_exc, "ref", None) or getattr(put_exc, "object_ref", None)
            if isinstance(returned_ref, StorageObjectRef):
                uploaded.append(returned_ref)
            raise
        uploaded.append(persisted)
        verified = _verify_output_identity(
            provider, persisted, expected_size=size, expected_sha256=digest
        )
        uploaded[-1] = verified
        files_manifest = _file_manifest(output)
        marker_payload = {
            "schema": "quicstudio.catalog-export-complete.v1",
            "export_id": export.id,
            "attempt": export.attempt,
            "format": export.format,
            "input_snapshot_id": export.input_snapshot_id,
            "artifact": asdict(verified),
            "size_bytes": size,
            "sha256": digest,
            "files": files_manifest,
        }
        marker_path = root / "completion.json"
        marker_path.write_text(json.dumps(marker_payload, sort_keys=True) + "\n", encoding="utf-8")
        marker_ref = StorageObjectRef(
            "export",
            f"{export.output_prefix}/completion.json",
            None,
            "",
            marker_path.stat().st_size,
            _sha256_file(marker_path),
        )
        try:
            marker_persisted = provider.put_worker_object(marker_ref, str(marker_path))
        except Exception as put_exc:
            returned_ref = getattr(put_exc, "ref", None) or getattr(put_exc, "object_ref", None)
            if isinstance(returned_ref, StorageObjectRef):
                uploaded.append(returned_ref)
            raise
        uploaded.append(marker_persisted)
        marker_verified = _verify_output_identity(
            provider,
            marker_persisted,
            expected_size=marker_ref.size_bytes,
            expected_sha256=marker_ref.sha256 or "",
        )
        uploaded[-1] = marker_verified
        export.status = "succeeded"
        export.checksum = digest
        export.size_bytes = size
        export.sha256 = digest
        export.oss_uri = provider.object_uri(verified)
        export.manifest_json = {
            "artifact": asdict(verified),
            "completion_marker": asdict(marker_verified),
            "files": files_manifest,
        }
        export.detail_json = _export_detail(
            output,
            export_format=export.format,
            size_bytes=size,
            sha256=digest,
            file_count=len(files_manifest),
        )
        db.flush()
        return _result(export)
    except Exception as exc:
        # A provider may commit before raising (or before returning a malformed
        # identity). Reconcile each planned key so cleanup never broad-deletes
        # an attempt prefix and no successful put is forgotten.
        seen_keys = {ref.object_key for ref in uploaded}
        for planned in planned_refs:
            if planned.object_key in seen_keys:
                continue
            try:
                discovered = provider.head(planned)
            except Exception:
                continue
            uploaded.append(discovered)
            seen_keys.add(discovered.object_key)
        cleanup_errors = []
        for ref in reversed(uploaded):
            try:
                provider.delete_exact(ref)
            except Exception as cleanup_exc:
                cleanup_errors.append({"ref": asdict(ref), "error": str(cleanup_exc)[:500]})
        export.status = "failed"
        export.error_code = "catalog_export_failed"
        export.error_message = str(exc)[:1000]
        export.cleanup_json = {
            "planned_refs": [asdict(ref) for ref in planned_refs],
            "deleted_refs": [
                asdict(ref)
                for ref in uploaded
                if asdict(ref) not in [item["ref"] for item in cleanup_errors]
            ],
            "orphan_refs": cleanup_errors,
        }
        db.flush()
        if isinstance(exc, NonRetryableJobError):
            raise
        raise CatalogExportError(str(exc) or "catalog export failed") from exc
    finally:
        shutil.rmtree(root, ignore_errors=True)


def recover_catalog_export_delivery(db: Session, export_id: int) -> CatalogDatasetExport:
    """Reconcile a completion marker after a worker/DB commit interruption."""
    export = db.get(CatalogDatasetExport, export_id)
    if export is None:
        raise LookupError("catalog export does not exist")
    if export.status == "succeeded":
        return export
    marker_key = f"{export.output_prefix}/completion.json"
    artifact_key = f"{export.output_prefix}/dataset.tar.gz"
    provider: Any | None = None
    scratch: Path | None = None
    try:
        if export.input_snapshot_id != _snapshot_hash(export.input_snapshot_json or {}):
            raise CatalogExportError("frozen source snapshot hash mismatch")
        provider = get_storage_provider()
        scratch = _make_scratch_root()
        marker = provider.head(StorageObjectRef("export", marker_key, None, "", 0, None))
        _require_complete_identity(marker)
        path = scratch / "completion.json"
        _enforce_scratch_budget(scratch, additional_bytes=marker.size_bytes)
        downloaded_marker = provider.download_file(marker, str(path))
        _assert_identity(marker, downloaded_marker)
        _enforce_scratch_budget(scratch)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            payload.get("schema") != "quicstudio.catalog-export-complete.v1"
            or payload.get("format") != export.format
            or int(payload.get("export_id")) != export.id
            or int(payload.get("attempt")) != export.attempt
            or payload.get("input_snapshot_id") != export.input_snapshot_id
        ):
            raise CatalogExportError("completion marker identity mismatch")
        artifact = StorageObjectRef(**dict(payload.get("artifact") or {}))
        _require_complete_identity(artifact)
        if artifact.bucket_role != "export" or artifact.object_key != artifact_key:
            raise CatalogExportError("completion marker artifact key mismatch")
        verified = _verify_output_identity(
            provider,
            artifact,
            expected_size=int(payload["size_bytes"]),
            expected_sha256=str(payload["sha256"]),
        )
        files = payload.get("files")
        if not isinstance(files, list) or not files:
            raise CatalogExportError("completion marker manifest is incomplete")
        seen_paths: set[str] = set()
        for item in files:
            if (
                not isinstance(item, Mapping)
                or not isinstance(item.get("path"), str)
                or not isinstance(item.get("size_bytes"), int)
                or item.get("size_bytes", -1) < 0
                or not isinstance(item.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None
            ):
                raise CatalogExportError("completion marker files manifest is invalid")
            relative = PurePosixPath(item["path"])
            if (
                relative.is_absolute()
                or any(part in {"", ".", ".."} for part in relative.parts)
                or item["path"] in seen_paths
            ):
                raise CatalogExportError("completion marker files manifest path is invalid")
            seen_paths.add(item["path"])
        artifact_path = scratch / "dataset.tar.gz"
        _enforce_scratch_budget(scratch, additional_bytes=verified.size_bytes)
        downloaded_artifact = provider.download_file(verified, str(artifact_path))
        _assert_identity(verified, downloaded_artifact)
        if (
            artifact_path.stat().st_size != verified.size_bytes
            or _sha256_file(artifact_path) != verified.sha256
        ):
            raise CatalogExportError("downloaded export artifact bytes changed")
        _enforce_scratch_budget(scratch)
        unpacked = scratch / "dataset"
        _enforce_scratch_budget(scratch)
        _extract_safe_tar(artifact_path, unpacked, expected_members=files)
        _enforce_scratch_budget(scratch)
        _validate_materialized(unpacked, export.format)
        actual_files = _file_manifest(unpacked)
        expected_files = [
            {
                "path": str(item["path"]),
                "size_bytes": int(item["size_bytes"]),
                "sha256": str(item["sha256"]),
            }
            for item in files
        ]
        if actual_files != sorted(expected_files, key=lambda item: item["path"]):
            raise CatalogExportError("completion marker files manifest does not match artifact")
        export.status = "succeeded"
        export.size_bytes = verified.size_bytes
        export.sha256 = str(payload["sha256"])
        export.checksum = export.sha256
        export.oss_uri = provider.object_uri(verified)
        export.manifest_json = {
            "artifact": asdict(verified),
            "completion_marker": asdict(marker),
            "files": files,
        }
        export.detail_json = _export_detail(
            unpacked,
            export_format=export.format,
            size_bytes=export.size_bytes,
            sha256=export.sha256,
            file_count=len(files),
        )
        db.commit()
        db.refresh(export)
        return export
    except Exception as exc:
        db.rollback()
        refs: list[StorageObjectRef] = []
        if provider is not None:
            for key in (artifact_key, marker_key):
                try:
                    refs.append(provider.head(StorageObjectRef("export", key, None, "", 0, None)))
                except Exception:
                    continue
        cleanup_errors = []
        for ref in refs:
            try:
                if provider is not None:
                    provider.delete_exact(ref)
            except Exception as cleanup_exc:
                cleanup_errors.append({"ref": asdict(ref), "error": str(cleanup_exc)[:500]})
        export.status = "failed"
        export.error_code = "catalog_export_recovery_failed"
        export.error_message = str(exc)[:1000]
        export.cleanup_json = {
            "deleted_refs": [
                asdict(ref)
                for ref in refs
                if asdict(ref) not in [item["ref"] for item in cleanup_errors]
            ],
            "orphan_refs": cleanup_errors,
            "reason": str(exc)[:500],
        }
        db.commit()
        raise CatalogExportError(str(exc)) from exc
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


def _result(export: CatalogDatasetExport) -> dict[str, Any]:
    return {
        "export_id": export.id,
        "status": export.status,
        "oss_uri": export.oss_uri,
        "size_bytes": export.size_bytes,
        "sha256": export.sha256,
        "attempt": export.attempt,
    }


def _snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _make_scratch_root() -> Path:
    configured = Path(str(getattr(settings, "scratch_root", ""))).expanduser()
    if not configured.is_absolute():
        configured = Path.cwd() / configured
    probe = configured if configured.is_absolute() else configured.absolute()
    while probe != probe.parent:
        if probe.is_symlink():
            raise CatalogExportError("scratch root contains a symlink")
        probe = probe.parent
    configured = configured.resolve()
    configured.mkdir(parents=True, exist_ok=True)
    budget = int(getattr(settings, "scratch_max_bytes", 0) or 0)
    if budget <= 0:
        raise CatalogExportError("scratch budget is unavailable")
    return Path(tempfile.mkdtemp(prefix="catalog-export-", dir=str(configured)))


def _scratch_root_for(path: Path) -> Path:
    root = Path(str(getattr(settings, "scratch_root", ""))).expanduser()
    if not root.is_absolute():
        root = (Path.cwd() / root).absolute()
    probe = Path(path).absolute()
    while probe != probe.parent:
        if probe.is_symlink():
            raise CatalogExportError("scratch path contains a symlink")
        probe = probe.parent
    root_probe = root.absolute()
    while root_probe != root_probe.parent:
        if root_probe.is_symlink():
            raise CatalogExportError("scratch root contains a symlink")
        root_probe = root_probe.parent
    root = root.resolve()
    candidate = Path(path).resolve()
    if candidate != root and root not in candidate.parents:
        raise CatalogExportError("scratch path escaped configured root")
    return root


def _enforce_scratch_budget(root: Path, *, additional_bytes: int = 0) -> None:
    budget = int(getattr(settings, "scratch_max_bytes", 0) or 0)
    used = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            raise CatalogExportError("scratch tree contains a symlink")
        if path.is_file():
            used += path.stat().st_size
    if used + int(additional_bytes) > budget:
        raise CatalogExportError("catalog export scratch budget exceeded")


def _materialize_snapshot(
    snapshot: Mapping[str, Any],
    destination: Path,
    provider: Any,
    *,
    source_kind: str,
    export_format: str,
) -> None:
    if source_kind == "lerobot_direct":
        if export_format != "lerobot_3_0":
            raise CatalogExportError("LeRobot direct datasets cannot export QRDF")
        objects = snapshot.get("objects")
        if not isinstance(objects, list) or not objects:
            raise CatalogExportError("frozen LeRobot object manifest is missing")
        for item in objects:
            _download_snapshot_ref(provider, item, destination)
        marker = snapshot.get("marker")
        if isinstance(marker, Mapping):
            _download_snapshot_ref(provider, marker, destination)
        return
    if export_format == "lerobot_3_0":
        raise CatalogExportError("QRDF to LeRobot conversion requires the SDK adapter")
    assets = snapshot.get("assets")
    if not isinstance(assets, list) or not assets:
        raise CatalogExportError("frozen catalog source snapshot has no assets")
    episodes_root = destination / "episodes"
    episodes_root.mkdir(parents=True)
    manifest = {"splits": {"train": [], "val": [], "test": []}, "episodes": []}
    episode_index = 1
    for asset in assets:
        source = asset.get("source_snapshot") if isinstance(asset, Mapping) else None
        if not isinstance(source, Mapping):
            raise CatalogExportError("frozen asset source snapshot is missing")
        if asset.get("snapshot_id") and asset["snapshot_id"] != _snapshot_hash(source):
            raise CatalogExportError("frozen asset source snapshot hash mismatch")
        for episode in source.get("episodes", []):
            if not isinstance(episode, Mapping):
                raise CatalogExportError("frozen episode snapshot is invalid")
            episode_id = f"episode_{episode_index:06d}"
            annotated = bool(source.get("annotation_enabled")) or "effective_segments" in episode
            # The full source exists only in worker scratch, never in a
            # delivered annotated dataset or training video directory.
            episode_dest = (
                destination.parent / f"annotation-source-{uuid4().hex}"
                if annotated
                else episodes_root / episode_id
            )
            episode_dest.mkdir()
            files = [item for item in episode.get("files", []) if isinstance(item, Mapping)]
            if not files:
                raise CatalogExportError("frozen episode objects are missing")
            for item in files:
                _download_snapshot_ref(provider, item, episode_dest)
            metadata = episode_dest / "metadata.json"
            if not metadata.is_file():
                raise CatalogExportError("frozen metadata object is missing")
            try:
                from qrdf.models.episode import EpisodeMetadata

                parsed = EpisodeMetadata.load(metadata)
                if annotated:
                    episode_index = _materialize_approved_segments(
                        episode,
                        episode_dest,
                        episodes_root=episodes_root,
                        manifest=manifest,
                        episode_index=episode_index,
                    )
                    shutil.rmtree(episode_dest)
                    continue
                parsed.episode_id = episode_id
                parsed.save(metadata)
                _materialize_annotation(
                    episode_dest, parsed, episode.get("annotation_revision") or {}
                )
            except Exception as exc:
                raise CatalogExportError("frozen episode metadata is invalid") from exc
            manifest["splits"]["train"].append(episode_id)
            frozen_annotation = episode.get("annotation_revision") or {}
            annotation_info = {
                "status": "materialized"
                if isinstance(frozen_annotation, Mapping) and frozen_annotation.get("payload")
                else "absent",
                "reason": None
                if isinstance(frozen_annotation, Mapping) and frozen_annotation.get("payload")
                else "frozen_snapshot_has_no_annotation_revision",
                "revision_id": frozen_annotation.get("revision_id")
                if isinstance(frozen_annotation, Mapping)
                else None,
            }
            manifest["episodes"].append(
                {
                    "episode_id": episode_id,
                    "source_episode_id": episode.get("episode_id"),
                    "source_fingerprint": episode.get("source_fingerprint"),
                    "annotation_revision": frozen_annotation,
                    "annotation": annotation_info,
                }
            )
            episode_index += 1
    if not manifest["episodes"]:
        raise CatalogExportError("frozen catalog snapshot has no effective samples")
    try:
        from qrdf.models.dataset import DatasetManifest

        dataset_manifest = DatasetManifest(dataset_name="quicstudio-catalog-export")
        for episode_id in manifest["splits"]["train"]:
            dataset_manifest.add_episode(episode_id, split="train")
        dataset_manifest.save(destination / "dataset.json")
    except Exception as exc:
        raise CatalogExportError("canonical QRDF manifest could not be written") from exc
    (destination / "quicdata_manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def _materialize_approved_segments(
    snapshot: Mapping[str, Any],
    source: Path,
    *,
    episodes_root: Path,
    manifest: dict,
    episode_index: int,
) -> int:
    from data.integrations.qrdf.annotated_segment_export import (
        approved_segments,
        output_intervals,
        verify_source_episode,
        write_approved_interval,
    )

    segments = approved_segments(snapshot)
    verify_source_episode(source, snapshot)
    for interval in output_intervals(segments):
        episode_id = f"episode_{episode_index:06d}"
        destination = episodes_root / episode_id
        materialized = write_approved_interval(
            source, destination, episode_id=episode_id, snapshot=snapshot, interval=interval
        )
        _materialize_annotation(
            destination, materialized["metadata"], materialized["annotation_revision"]
        )
        provenance = materialized["provenance"]
        manifest["splits"]["train"].append(episode_id)
        manifest["episodes"].append(
            {
                "episode_id": episode_id,
                **provenance,
                "annotation_revision": {
                    "id": snapshot["annotation_revision"]["id"],
                    "version": snapshot["annotation_revision"].get("version"),
                },
                "annotation": {
                    "status": "materialized",
                    "reason": None,
                    "revision_id": snapshot["annotation_revision"]["id"],
                },
            }
        )
        _enforce_scratch_budget(_scratch_root_for(destination))
        episode_index += 1
    return episode_index


def _materialize_annotation(episode_path: Path, metadata: Any, revision: Mapping[str, Any]) -> None:
    payload = revision.get("payload") if isinstance(revision, Mapping) else None
    if not payload:
        return
    try:
        from qrdf.annotations import write_episode_annotation
        from qrdf.models.annotation import EpisodeAnnotation

        data_file = metadata.resolve_data_file(episode_path)
        data_sha = _sha256_file(data_file)
        annotation_payload = dict(payload)
        annotation_payload.setdefault("qrdf_version", metadata.qrdf_version)
        source = dict(annotation_payload.get("source") or {})
        source["episode_id"] = metadata.episode_id
        source["data_sha256"] = data_sha
        annotation_payload["source"] = source
        annotation = EpisodeAnnotation.model_validate(annotation_payload)
        from qrdf.reader.episode import Episode

        write_episode_annotation(Episode(episode_path, metadata=metadata), annotation)
    except Exception as exc:
        raise CatalogExportError("frozen episode annotation could not be materialized") from exc


def _download_snapshot_ref(
    provider: Any, item: Mapping[str, Any], destination: Path, *, output_name: str | None = None
) -> Path:
    raw_ref = item.get("ref") if isinstance(item, Mapping) else None
    path_value = (
        output_name or (item.get("path") or item.get("relative_path"))
        if isinstance(item, Mapping)
        else None
    )
    if not isinstance(raw_ref, Mapping) or not isinstance(path_value, str):
        raise CatalogExportError("frozen provider reference is invalid")
    safe = PurePosixPath(path_value)
    if (
        "\x00" in path_value
        or not safe.parts
        or safe.is_absolute()
        or any(part in {"", ".", ".."} for part in safe.parts)
    ):
        raise CatalogExportError("frozen provider path is unsafe")
    role = str(raw_ref.get("bucket_role") or "")
    if role not in {"raw", "process", "export"}:
        raise CatalogExportError("frozen provider bucket role is invalid")
    raw_size = raw_ref.get("size_bytes")
    expected = StorageObjectRef(
        role,
        str(raw_ref.get("object_key") or ""),
        raw_ref.get("version_id"),
        str(raw_ref.get("etag") or ""),
        int(raw_size) if raw_size is not None else -1,
        raw_ref.get("sha256"),
    )
    _require_complete_identity(expected)
    root = _scratch_root_for(destination)
    _enforce_scratch_budget(root, additional_bytes=expected.size_bytes)
    target = destination / safe
    target.parent.mkdir(parents=True, exist_ok=True)
    headed = provider.head(expected)
    _assert_identity(expected, headed)
    actual = provider.download_file(expected, str(target))
    _assert_identity(expected, actual)
    return target


def _extract_safe_tar(
    archive: Path, destination: Path, *, expected_members: list[Mapping[str, Any]]
) -> None:
    try:
        with tarfile.open(archive, "r") as handle:
            members = handle.getmembers()
            expected = {
                str(item.get("path")): item
                for item in expected_members
                if isinstance(item, Mapping)
            }
            actual_names = {member.name for member in members if member.isfile()}
            if len(actual_names) != sum(1 for member in members if member.isfile()):
                raise CatalogExportError("process archive contains duplicate members")
            if expected and actual_names != set(expected):
                raise CatalogExportError("process archive member manifest changed")
            destination.mkdir(parents=True, exist_ok=True)
            for member in members:
                name = PurePosixPath(member.name)
                if (
                    name.is_absolute()
                    or any(part in {"", ".", ".."} for part in name.parts)
                    or member.issym()
                    or member.islnk()
                ):
                    raise CatalogExportError("process archive contains an unsafe member")
            handle.extractall(destination, filter="data")
            for name, expected_item in expected.items():
                extracted = destination / PurePosixPath(name)
                expected_size = expected_item.get("size_bytes")
                if (
                    not isinstance(expected_size, int)
                    or extracted.stat().st_size != expected_size
                    or _sha256_file(extracted) != str(expected_item.get("sha256") or "")
                ):
                    raise CatalogExportError("process archive member identity changed")
    except CatalogExportError:
        raise
    except Exception as exc:
        raise CatalogExportError("process archive is unreadable") from exc


def _validate_materialized(path: Path, export_format: str) -> None:
    if export_format == "qrdf_0_2":
        from qrdf.validator.validator import QRDFValidator

        report = QRDFValidator().validate_canonical_dataset(path, check_annotation=True)
    else:
        from qrdf.converters.validate_lerobot import validate_lerobot_dataset

        report = validate_lerobot_dataset(path)
    if not bool(report.ok if hasattr(report, "ok") else not report.error_count):
        summary = report.summary() if hasattr(report, "summary") else "SDK validation failed"
        raise CatalogExportError(f"{export_format} SDK validation failed: {summary}")


def _convert_qrdf_to_lerobot(source: Path, destination: Path) -> None:
    """Use the vendored converter for QRDF -> native LeRobot output."""
    from qrdf.converters.published_sample_lerobot import (
        SliceExportOptions,
        SliceExportRequest,
        export_episode_slices_to_lerobot_v3,
    )
    from qrdf.reader.reader import QRDFReader

    # ``run_catalog_export`` prepares its output directory before selecting
    # the format-specific materializer.  Accept that owned empty directory
    # when this converter is invoked through the durable export handler.
    destination.mkdir(parents=True, exist_ok=True)
    reader = QRDFReader(source)
    provenance = {}
    manifest_path = source / "quicdata_manifest.json"
    if manifest_path.is_file():
        try:
            provenance = {
                str(item.get("episode_id")): item
                for item in json.loads(manifest_path.read_text(encoding="utf-8")).get(
                    "episodes", []
                )
            }
        except Exception:
            provenance = {}
    requests = []
    export_modes = set()
    for position, episode_id in enumerate(reader.list_episodes()):
        episode = reader.load_episode(episode_id)
        timing = episode.metadata.timing
        start_ns = int(timing.start_timestamp_ns or 0)
        end_ns = int(timing.end_timestamp_ns or 0)
        if end_ns <= start_ns:
            raise CatalogExportError("QRDF episode timing is incomplete for LeRobot conversion")
        provenance_row = provenance.get(episode_id, {})
        task = str(
            provenance_row.get("description")
            or (
                episode.metadata.task.language or episode.metadata.task.name
                if episode.metadata.task is not None
                else "catalog export"
            )
        )
        from qrdf.registry.topic_layout import resolve_eef_reference_topic
        from qrdf.registry.topics import list_rgb_topics

        topics = episode.list_topics()
        descriptor = episode.metadata.embodiment
        has_policy_actions = descriptor is not None and any(
            representation.observation
            and representation.action
            and representation.policy_io_contract.action_mode != "none"
            and any(
                feature.semantic not in {"rgb", "depth"}
                for feature in representation.policy_io_contract.observations
            )
            and any(
                feature.semantic not in {"rgb", "depth"}
                for feature in representation.policy_io_contract.actions
            )
            for representation in descriptor.representations.values()
        )
        if has_policy_actions or (descriptor is None and resolve_eef_reference_topic(topics)):
            export_modes.add("manipulation")
        elif list_rgb_topics(topics):
            export_modes.add("ego_rgb")
        else:
            raise CatalogExportError("QRDF episode has no supported training representation")
        source_fingerprint = str(
            provenance.get(episode_id, {}).get("source_fingerprint")
            or episode.metadata.extensions.get("source_fingerprint")
            or _sha256_file(episode.path / episode.metadata.data_file)
        )
        requests.append(
            SliceExportRequest(
                sample_id=episode_id,
                source_qrdf_path=source,
                source_fingerprint=source_fingerprint,
                source_episode_id=episode_id,
                start_ns=start_ns,
                end_ns=end_ns,
                task=task,
                split="train",
                position=position,
            )
        )
    if not requests:
        raise CatalogExportError("QRDF dataset has no episodes for LeRobot conversion")
    if len(export_modes) != 1:
        raise CatalogExportError("QRDF episodes have incompatible training representations")
    try:
        max_duration = max(
            (request.end_ns - request.start_ns) / 1_000_000_000 for request in requests
        )
        export_episode_slices_to_lerobot_v3(
            requests,
            destination,
            options=SliceExportOptions(
                export_mode=next(iter(export_modes)), max_sample_duration_s=max(300.0, max_duration)
            ),
        )
        if manifest_path.is_file():
            shutil.copyfile(manifest_path, destination / "quicdata_manifest.json")
    except Exception as exc:
        raise CatalogExportError("QRDF to LeRobot conversion failed") from exc


def _write_export_manifest(
    path: Path, version: CatalogDatasetVersion, export: CatalogDatasetExport
) -> None:
    (path / "quicstudio_export.json").write_text(
        json.dumps(
            {
                "version_id": version.id,
                "attempt": export.attempt,
                "input_snapshot_id": export.input_snapshot_id,
                "format": export.format,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _file_manifest(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": p.relative_to(root).as_posix(),
            "size_bytes": p.stat().st_size,
            "sha256": _sha256_file(p),
        }
        for p in sorted(root.rglob("*"))
        if p.is_file()
    ]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_complete_identity(ref: StorageObjectRef) -> None:
    # OSS buckets without versioning still freeze identity through ETag,
    # size and SHA-256. Providers condition the download on that ETag. When a
    # version was recorded, _assert_identity additionally requires it to match.
    if (
        ref.bucket_role not in {"raw", "process", "export"}
        or not ref.object_key
        or not ref.etag
        or ref.size_bytes < 0
        or not ref.sha256
        or re.fullmatch(r"[0-9a-f]{64}", str(ref.sha256)) is None
    ):
        raise CatalogExportError("provider object identity is incomplete")


def _assert_identity(expected: StorageObjectRef, actual: StorageObjectRef) -> None:
    _require_complete_identity(expected)
    _require_complete_identity(actual)
    if actual.bucket_role != expected.bucket_role or actual.object_key != expected.object_key:
        raise CatalogExportError("provider identity changed during download")
    if actual.version_id != expected.version_id:
        raise CatalogExportError("provider version changed during download")
    if actual.etag != expected.etag:
        raise CatalogExportError("provider ETag changed during download")
    if actual.size_bytes != expected.size_bytes:
        raise CatalogExportError("provider size changed during download")
    if actual.sha256 != expected.sha256:
        raise CatalogExportError("provider SHA-256 changed during download")


def _verify_output_identity(
    provider: Any, ref: StorageObjectRef, *, expected_size: int, expected_sha256: str
) -> StorageObjectRef:
    _require_complete_identity(ref)
    if ref.size_bytes != expected_size or ref.sha256 != expected_sha256:
        raise CatalogExportError("provider output identity mismatch")
    head = provider.head(ref)
    _require_complete_identity(head)
    if (
        head.bucket_role != ref.bucket_role
        or head.object_key != ref.object_key
        or head.size_bytes != expected_size
        or head.version_id != ref.version_id
        or head.etag != ref.etag
    ):
        raise CatalogExportError("provider output identity mismatch")
    if head.sha256 != expected_sha256:
        raise CatalogExportError("provider output SHA-256 mismatch")
    return StorageObjectRef(
        head.bucket_role,
        head.object_key,
        head.version_id,
        head.etag,
        head.size_bytes,
        expected_sha256,
    )


__all__ = [
    "CatalogExportError",
    "enqueue_catalog_export",
    "recover_catalog_export_delivery",
    "retry_catalog_export",
    "run_catalog_export",
]
