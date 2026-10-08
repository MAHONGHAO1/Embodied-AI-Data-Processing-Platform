"""Cloud storage orchestration: publish pipeline boundaries based on storage_mode.

Three modes (see data.services.storage_mode):
- **local**: Canonical paths reside in persistent namespaces under storage_root: raw/, process/, official/, exports/;
  hot/ is used only for in-step staging and can be safely cleaned up after publish.
- **cloud**: Canonical paths are always oss://; transient materialization during steps; no long-term cloud/ mirrors kept by default.
- **hybrid** (default): After local upload, boundary pushes to cloud (real OSS or storage/cloud/ mirror);
  annotation and streaming can be fetched via mirror or materialization.

Design principles:
- **Single package pipeline**: In-step local atomic processing; mode determines whether boundary syncs to cloud or persistent dir.
- **Multi-package pipeline**: Materialize from oss:// (cloud/hybrid) or read local paths directly (local).
- **Terminal cleanup**: hybrid/cloud can purge cloud/ mirrors; local mainly cleans hot staging without deleting persistent namespaces.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from data.config import settings
from data.database import JobRun
from data.infra import oss_client
from data.services.job_runs import create_or_get_job
from data.utils.storage_paths import (
    cloud_path_has_required_authority,
    cloud_path_is_safe,
    is_under_storage_root,
    resolve_cloud_mirror_path,
    resolve_storage_path,
    storage_root_path,
)
from data.utils.storage_uri import is_cloud_uri, parse_storage_uri

logger = logging.getLogger(__name__)
_EXPORT_STAGING_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

OSS_RAW_COPY_JOB_KIND = "collect_oss_raw_copy"
OSS_RAW_COPY_QUEUE = "media"
RAW_STATE_EXTERNAL_OBJECT_REGISTERED = "external_object_registered"
RAW_STATE_RAW_COPY_PENDING = "raw_copy_pending"
RAW_STATE_RAW_COPIED = "raw_copied"
RAW_STATE_CHECKSUM_VERIFIED = "checksum_verified"
RAW_STATE_PROCESSING_READY = "processing_ready"
RAW_STATE_RAW_COPY_FAILED = "raw_copy_failed"


@dataclass(frozen=True)
class RawUploadTarget:
    mode: str
    scheme: str
    bucket: str
    prefix: str
    key: str
    prefix_uri: str
    object_uri: str


def new_process_run_id(task_id: int) -> str:
    """Unique identifier for preprocess/retry run to avoid overwriting OSS keys and local mirrors."""
    return f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"


def cloud_pipeline_enabled() -> bool:
    """Whether pipeline boundary publishes to oss:// namespace.

    local: False (canonical path remains under storage_root).
    cloud / hybrid: True.
    """
    from data.services.storage_mode import uses_cloud_uri_authority

    return uses_cloud_uri_authority()


# hot/ is temporary staging only; local canonical data resides in raw/process/official/exports


def _storage_root_resolved() -> Path:
    return Path(settings.storage_root).expanduser().resolve()


def _local_authority_uri(local_path: Path) -> str:
    """Convert path within storage_root to nas:// or relative canonical URI (no longer falls back to hot/)."""
    from data.utils.storage_uri import to_storage_uri

    root = _storage_root_resolved()
    resolved = Path(local_path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"本地权威路径必须位于 storage_root 内（勿使用 hot/ 作为发布目标）: {local_path}"
        ) from exc
    return to_storage_uri(str(resolved))


def _copy_to_local_dest(src: Path, dest: Path) -> Path:
    with verified_publish_snapshot(src) as snapshot:
        return _copy_verified_to_local_dest(snapshot, dest)


def _copy_verified_to_local_dest(src: Path, dest: Path) -> Path:
    """Copy into persistent storage without buffering large raw files in memory."""
    src = Path(src).resolve()
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and src.resolve() == dest.resolve():
        return dest.resolve()
    if src.is_dir():
        if dest.exists():
            if dest.is_dir():
                shutil.rmtree(dest, ignore_errors=True)
            else:
                dest.unlink(missing_ok=True)
        shutil.copytree(src, dest)
    else:
        temporary = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")
        try:
            with src.open("rb") as input_file, temporary.open("xb") as output_file:
                while block := input_file.read(1024 * 1024):
                    output_file.write(block)
                output_file.flush()
                os.fsync(output_file.fileno())
            os.replace(temporary, dest)
        finally:
            if temporary.exists():
                temporary.unlink()
    return dest.resolve()


@contextmanager
def verified_publish_snapshot(source: Path) -> Iterator[Path]:
    """Materialize a no-symlink, identity-checked private publication snapshot."""
    _require_secure_snapshot_capabilities()
    source = Path(source)
    initial = os.lstat(source)
    if stat.S_ISLNK(initial.st_mode) or not (
        stat.S_ISREG(initial.st_mode) or stat.S_ISDIR(initial.st_mode)
    ):
        raise ValueError("publication source must be a regular file or directory")
    storage_root = _storage_root_resolved()
    resolved_source = source.resolve(strict=False)
    if resolved_source == storage_root:
        raise ValueError("publishing the complete storage root is not allowed")
    snapshot_parent = storage_root / "hot" / "publish-snapshots"
    try:
        snapshot_parent.relative_to(resolved_source)
        snapshot_parent = storage_root / "publish-snapshots"
    except ValueError:
        pass
    snapshot_parent = snapshot_parent.resolve()
    try:
        snapshot_parent.relative_to(storage_root)
    except ValueError as exc:
        raise ValueError("publication snapshot root escaped storage root") from exc
    snapshot_parent.mkdir(parents=True, exist_ok=True)
    root = snapshot_parent / uuid.uuid4().hex
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    snapshot = root / source.name
    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        if stat.S_ISDIR(initial.st_mode):
            source_fd = os.open(source, flags | os.O_DIRECTORY)
            try:
                _assert_same_inode(initial, os.fstat(source_fd))
                snapshot.mkdir(mode=0o700)
                _snapshot_directory(source_fd, snapshot)
                _assert_source_unchanged(initial, os.fstat(source_fd))
            finally:
                os.close(source_fd)
        else:
            source_fd = os.open(source, flags)
            try:
                _assert_same_inode(initial, os.fstat(source_fd))
                _copy_snapshot_file(source_fd, snapshot)
                _assert_source_unchanged(initial, os.fstat(source_fd))
            finally:
                os.close(source_fd)
        yield snapshot
    finally:
        try:
            root.relative_to(snapshot_parent)
        except ValueError:
            logger.error("refusing to clean publication snapshot outside its sandbox")
        else:
            shutil.rmtree(root, ignore_errors=True)


def _snapshot_directory(source_fd: int, destination: Path) -> None:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    for name in sorted(os.listdir(source_fd)):
        if name in {".", ".."} or "/" in name:
            raise ValueError("publication directory contains an invalid entry")
        before = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        target = destination / name
        if stat.S_ISLNK(before.st_mode):
            raise ValueError("publication directory contains a symlink")
        if stat.S_ISDIR(before.st_mode):
            child_fd = os.open(
                name,
                flags | os.O_DIRECTORY,
                dir_fd=source_fd,
            )
            try:
                _assert_same_inode(before, os.fstat(child_fd))
                target.mkdir(mode=0o700)
                _snapshot_directory(child_fd, target)
                _assert_source_unchanged(before, os.fstat(child_fd))
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(before.st_mode):
            child_fd = os.open(name, flags, dir_fd=source_fd)
            try:
                _assert_same_inode(before, os.fstat(child_fd))
                _copy_snapshot_file(child_fd, target)
                _assert_source_unchanged(before, os.fstat(child_fd))
            finally:
                os.close(child_fd)
        else:
            raise ValueError("publication directory contains a non-regular entry")


def _copy_snapshot_file(source_fd: int, destination: Path) -> None:
    with os.fdopen(os.dup(source_fd), "rb") as input_file, destination.open("xb") as output_file:
        while block := input_file.read(1024 * 1024):
            output_file.write(block)
        output_file.flush()
        os.fsync(output_file.fileno())
    os.chmod(destination, 0o600)


def _assert_same_inode(before: os.stat_result, after: os.stat_result) -> None:
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or stat.S_IFMT(before.st_mode) != stat.S_IFMT(after.st_mode)
    ):
        raise ValueError("publication source changed while it was being verified")


def _assert_source_unchanged(before: os.stat_result, after: os.stat_result) -> None:
    _assert_same_inode(before, after)
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
    ):
        raise ValueError("publication source changed while it was being copied")


def _require_secure_snapshot_capabilities() -> None:
    required_constants = ("O_NOFOLLOW", "O_DIRECTORY")
    missing = [name for name in required_constants if not hasattr(os, name)]
    if missing:
        raise RuntimeError("secure publication snapshots require " + ", ".join(missing))
    if (
        os.open not in getattr(os, "supports_dir_fd", set())
        or os.stat not in getattr(os, "supports_dir_fd", set())
        or os.stat not in getattr(os, "supports_follow_symlinks", set())
        or os.listdir not in getattr(os, "supports_fd", set())
    ):
        raise RuntimeError("secure publication snapshots require fd-relative filesystem APIs")


def _upload_verified(source: Path, bucket: str, key: str) -> str:
    with verified_publish_snapshot(source) as snapshot:
        return oss_client.upload_file(snapshot, bucket, key)


def _write_local_sidecar_metadata(base_dir: Path, metadata: dict) -> None:
    base_dir.mkdir(parents=True, exist_ok=True)
    meta_file = base_dir / "metadata.json"
    meta_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


def _is_under_hot(resolved: Path, root: Path) -> bool:
    try:
        return resolved.relative_to(root).parts[:1] == ("hot",)
    except ValueError:
        return False


def plan_raw_upload(
    local_path: Path,
    *,
    upload_id: str,
    task_id: int | None = None,
    workspace_id: int | None = None,
    project_id: int | None = None,
) -> RawUploadTarget:
    from data.services.storage_mode import get_effective_storage_mode

    mode = get_effective_storage_mode()
    ws = f"ws{workspace_id}" if workspace_id else "ws0"
    proj = f"proj{project_id}" if project_id else "proj0"
    task_seg = f"task{task_id}" if task_id else "task0"
    prefix = f"raw/{ws}/{proj}/{task_seg}/{upload_id}"
    key = f"{prefix}/{Path(local_path).name}"
    if mode == "local" or not cloud_pipeline_enabled():
        prefix_uri = _local_authority_uri(_storage_root_resolved() / prefix)
        object_uri = _local_authority_uri(_storage_root_resolved() / key)
        bucket, _ = parse_storage_uri(prefix_uri)
        return RawUploadTarget(mode, "nas", bucket, prefix, key, prefix_uri, object_uri)
    bucket = oss_client.bucket_name("raw")
    return RawUploadTarget(
        mode,
        "oss",
        bucket,
        prefix,
        key,
        f"oss://{bucket}/{prefix}",
        f"oss://{bucket}/{key}",
    )


def publish_raw_upload(
    local_path: Path,
    *,
    upload_id: str,
    task_id: int | None = None,
    workspace_id: int | None = None,
    project_id: int | None = None,
    target: RawUploadTarget | None = None,
) -> str:
    """Data ingestion completed.

    - local: Copy to persistent namespace storage_root/raw/... (does not remain in hot/)
    - hybrid/cloud: Publish to raw bucket oss:// (real OSS or cloud/ mirror)
    """
    from data.services.storage_mode import keep_local_object_mirror

    original_src = Path(local_path)
    expected = plan_raw_upload(
        original_src,
        upload_id=upload_id,
        task_id=task_id,
        workspace_id=workspace_id,
        project_id=project_id,
    )
    target = target or expected
    if target != expected:
        raise ValueError("raw publication target does not match the canonical namespace")
    mode = target.mode
    prefix = target.prefix
    key = target.key

    with verified_publish_snapshot(original_src) as src:
        if target.scheme == "nas":
            root = _storage_root_resolved()
            dest = root / key
            _copy_verified_to_local_dest(src, dest)
            _write_local_sidecar_metadata(
                root / prefix,
                {
                    "upload_id": upload_id,
                    "task_id": task_id,
                    "file_name": original_src.name,
                    "uploaded_at": datetime.utcnow().isoformat() + "Z",
                    "storage_mode": mode,
                },
            )
            uri = _local_authority_uri(dest)
            logger.info("原始数据已发布到本地 raw/ [%s]: %s", mode, uri)
            return uri

        bucket = target.bucket
        uri = oss_client.upload_file(src, bucket, key)
        if mode == "cloud" and not keep_local_object_mirror(mode):
            try:
                mirror = Path(settings.storage_root) / "cloud" / bucket / key
                if mirror.is_file():
                    mirror.unlink(missing_ok=True)
                elif mirror.is_dir():
                    shutil.rmtree(mirror, ignore_errors=True)
            except OSError:
                pass
        _upload_metadata(
            bucket,
            prefix,
            {
                "upload_id": upload_id,
                "task_id": task_id,
                "file_name": original_src.name,
                "uploaded_at": datetime.utcnow().isoformat() + "Z",
                "storage_mode": mode,
            },
        )
        logger.info("原始数据已上云 [%s]: %s", mode, uri)
        return uri


def cleanup_raw_upload_prefix(
    prefix_uri: str,
    *,
    upload_id: str,
    workspace_id: int,
    project_id: int | None,
    task_id: int | None,
    attempt_id: str,
) -> None:
    """Delete one canonical attempt prefix, never a caller-selected provider path."""
    expected_prefix = (
        f"raw/ws{workspace_id}/proj{project_id or 0}/task{task_id or 0}/"
        f"{upload_id}/attempts/{attempt_id}"
    )
    bucket, key = parse_storage_uri(prefix_uri)
    if key.rstrip("/") != expected_prefix:
        raise ValueError("raw cleanup prefix is not the canonical attempt namespace")
    if prefix_uri.startswith("oss://"):
        if bucket != oss_client.bucket_name("raw"):
            raise ValueError("raw cleanup bucket is not canonical")
        oss_client.delete_prefix(bucket, expected_prefix)
        return
    if not prefix_uri.startswith("nas://"):
        raise ValueError("raw cleanup scheme is unsupported")
    root = _storage_root_resolved()
    attempt_root = resolve_storage_path(prefix_uri).resolve()
    expected_root = (root / expected_prefix).resolve()
    if attempt_root != expected_root or attempt_root.is_symlink():
        raise ValueError("raw cleanup target is outside the canonical attempt namespace")
    if attempt_root.exists():
        shutil.rmtree(attempt_root)


def cleanup_raw_upload_attempt(
    raw_uri: str,
    *,
    upload_id: str,
    workspace_id: int | None,
    attempt_id: str,
) -> None:
    """Delete only one attempt-private raw publication after a fencing loss."""
    if workspace_id is None:
        raise ValueError("upload workspace is required for attempt cleanup")
    if not attempt_id or "/" in attempt_id or len(attempt_id) != 64:
        raise ValueError("invalid raw upload attempt id")
    bucket, key = parse_storage_uri(raw_uri)
    marker = f"raw/ws{workspace_id}/"
    attempt_marker = f"/{upload_id}/attempts/{attempt_id}/"
    if not key.startswith(marker) or attempt_marker not in f"/{key}":
        raise ValueError("raw URI is outside the upload attempt namespace")
    prefix = key.rsplit("/", 1)[0]
    if not prefix.endswith(f"/{upload_id}/attempts/{attempt_id}"):
        raise ValueError("raw URI is not an attempt-private object")
    if raw_uri.startswith("oss://"):
        oss_client.delete_prefix(bucket, prefix)
        return

    resolved = resolve_storage_path(raw_uri)
    attempt_root = resolved.parent.resolve()
    storage_root = _storage_root_resolved()
    try:
        attempt_root.relative_to(storage_root / "raw" / f"ws{workspace_id}")
    except ValueError as exc:
        raise ValueError("raw attempt cleanup escaped storage root") from exc
    if attempt_root.name != attempt_id or attempt_root.is_symlink():
        raise ValueError("raw attempt cleanup target is invalid")
    if attempt_root.exists():
        shutil.rmtree(attempt_root)


def import_external_to_raw(
    *,
    src_bucket: str,
    src_key: str,
    task_id: int,
    workspace_id: int | None = None,
    project_id: int | None = None,
    import_id: str | None = None,
) -> str:
    """OSS scan ingestion: copy external bucket objects to dedicated platform raw bucket directory (not retaining original path).

    Forbidden in local mode (no object storage input semantics); available in cloud/hybrid.
    """
    from data.services.storage_mode import allow_oss_import, get_effective_storage_mode

    mode = get_effective_storage_mode()
    if not allow_oss_import(mode):
        raise ValueError(
            "当前为纯本地存储模式，不支持从对象存储导入；请改用本地上传，或将 storage_mode 设为 hybrid/cloud"
        )

    raw_bucket = oss_client.bucket_name("raw")
    ws = f"ws{workspace_id}" if workspace_id else "ws0"
    proj = f"proj{project_id}" if project_id else "proj0"
    imp = import_id or uuid.uuid4().hex
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    file_name = Path(src_key).name
    prefix = f"raw/{ws}/{proj}/task{task_id}/{imp}_{stamp}"
    dst_key = f"{prefix}/{file_name}"
    src_key_norm = src_key.lstrip("/")
    uri = oss_client.copy_object(src_bucket, src_key_norm, raw_bucket, dst_key)
    _upload_metadata(
        raw_bucket,
        prefix,
        {
            "import_id": imp,
            "task_id": task_id,
            "source_bucket": src_bucket,
            "source_key": src_key_norm,
            "file_name": file_name,
            "imported_at": datetime.utcnow().isoformat() + "Z",
            "storage_mode": mode,
        },
    )
    logger.info("OSS 导入已复制到专有目录: %s -> %s", f"oss://{src_bucket}/{src_key_norm}", uri)
    return uri


def enqueue_external_raw_copy_job(
    db: Session,
    task_id: int,
    *,
    actor_id: int | None = None,
) -> JobRun:
    """Register an external OSS object for worker-side immutable raw copy."""
    # Legacy Task-based OSS import remains isolated from Batch / Episode storage.
    # Import lazily so the new artifact materialization path does not depend on
    # the retired Task ORM schema.
    from data.database import Task

    task = db.get(Task, task_id)
    if task is None or task.task_type != "collect" or task.data_source != "oss_import":
        raise ValueError("OSS import task does not exist")
    metadata = dict(task.metadata_json or {})
    collect = dict(metadata.get("collect") or {})
    bucket = str(collect.get("source_bucket") or collect.get("bucket") or "")
    key = str(collect.get("source_key") or collect.get("key") or "")
    if not bucket or not key:
        raise ValueError("OSS import task is missing source object")
    task.metadata_json = _with_raw_ingest_state(
        metadata,
        RAW_STATE_EXTERNAL_OBJECT_REGISTERED,
        source_kind="oss_import",
    )
    db.commit()
    return create_or_get_job(
        db,
        kind=OSS_RAW_COPY_JOB_KIND,
        resource_type="task",
        resource_id=task.id,
        idempotency_key=f"oss-raw-copy:{task.id}:{bucket}:{key}",
        queue=OSS_RAW_COPY_QUEUE,
        actor_id=actor_id,
        detail={"operation": "external_raw_copy"},
    )


def complete_external_raw_copy(
    db: Session,
    task_id: int,
    *,
    operator: str = "system",
) -> dict[str, object]:
    """Copy a registered OSS object into the platform raw namespace in a worker."""
    from data.database import Task, TaskStatus
    from data.services.provenance import assert_collection_attribution_complete
    from data.services.state_machine import transit_task

    task = db.get(Task, task_id)
    if task is None or task.task_type != "collect" or task.data_source != "oss_import":
        raise ValueError("OSS import task does not exist")
    assert_collection_attribution_complete(db, task)
    metadata = dict(task.metadata_json or {})
    collect = dict(metadata.get("collect") or {})
    bucket = str(collect.get("source_bucket") or collect.get("bucket") or "")
    key = str(collect.get("source_key") or collect.get("key") or "")
    if not bucket or not key:
        raise ValueError("OSS import task is missing source object")

    raw_uri = str(collect.get("raw_uri") or "")
    if not raw_uri:
        task.metadata_json = _with_raw_ingest_state(
            task.metadata_json,
            RAW_STATE_RAW_COPY_PENDING,
            source_kind="oss_import",
        )
        db.commit()
        raw_uri = import_external_to_raw(
            src_bucket=bucket,
            src_key=key,
            task_id=task.id,
            workspace_id=task.workspace_id,
            project_id=task.project_id,
        )
        collect.update(
            {
                "raw_uri": raw_uri,
                "format": _infer_external_object_format(key),
                "conversion": "mcap_to_qrdf_pending"
                if _infer_external_object_format(key) == "mcap"
                else "none",
            }
        )
        metadata["collect"] = collect
        task.storage_path = raw_uri
        task.metadata_json = _with_raw_ingest_state(
            metadata,
            RAW_STATE_RAW_COPIED,
            source_kind="oss_import",
        )
        db.commit()

    task.metadata_json = _with_raw_ingest_state(
        task.metadata_json,
        RAW_STATE_CHECKSUM_VERIFIED,
        source_kind="oss_import",
        verification="copy_object",
    )
    db.commit()
    if task.status == TaskStatus.PENDING_COLLECT.value:
        transit_task(db, task, "collect_done", operator, "OSS 导入完成，待接入审核")
    elif task.status != TaskStatus.COLLECT_DONE.value:
        raise ValueError(f"OSS import task has unexpected status: {task.status}")
    task.metadata_json = _with_raw_ingest_state(
        task.metadata_json,
        RAW_STATE_PROCESSING_READY,
        source_kind="oss_import",
    )
    db.commit()
    return {
        "task_id": task.id,
        "raw_uri": raw_uri,
        "format": str(collect.get("format") or "unknown"),
    }


def mark_external_raw_copy_failed(db: Session, task_id: int) -> None:
    """Persist an OSS raw-copy failure without exposing a provider exception."""
    from data.database import Task

    task = db.get(Task, task_id)
    if task is None:
        return
    task.metadata_json = _with_raw_ingest_state(
        task.metadata_json,
        RAW_STATE_RAW_COPY_FAILED,
        source_kind="oss_import",
    )
    db.commit()


def _infer_external_object_format(key: str) -> str:
    import re

    name = str(key).lower().rsplit("/", 1)[-1]
    if name.endswith(".mcap"):
        return "mcap"
    if name.endswith(".qrdf.zip") or name.endswith("_qrdf.zip"):
        return "qrdf"
    if re.match(r"^episode_\d{6}\.zip$", name):
        return "qrdf"
    if name.endswith(".zip"):
        return "qrdf"
    return "unknown"


def _with_raw_ingest_state(metadata: object, state: str, **extra: object) -> dict[str, object]:
    payload = dict(metadata) if isinstance(metadata, dict) else {}
    raw_ingest = dict(payload.get("raw_ingest") or {})
    raw_ingest.update({"state": state, **extra})
    payload["raw_ingest"] = raw_ingest
    return payload


def publish_preprocess_output(
    local_dataset_path: Path,
    *,
    task_id: int,
    workspace_id: int | None = None,
    project_id: int | None = None,
    process_task_id: str | None = None,
    run_id: str | None = None,
) -> str:
    """Publish preprocessing/conversion results.

    - local: Copy to persistent namespace storage_root/process/... (does not remain in hot/)
    - hybrid/cloud: Upload to process bucket
    """
    from data.services.storage_mode import get_effective_storage_mode, keep_local_object_mirror

    mode = get_effective_storage_mode()
    src = Path(local_dataset_path)
    ws = f"ws{workspace_id}" if workspace_id else "ws0"
    proj = f"proj{project_id}" if project_id else "proj0"
    run = run_id or new_process_run_id(task_id)
    base = f"process/{ws}/{proj}/task{task_id}/{run}"
    prefix = f"{base}/output"

    if mode == "local" or not cloud_pipeline_enabled():
        root = _storage_root_resolved()
        dest = root / prefix
        _copy_to_local_dest(src, dest)
        _write_local_sidecar_metadata(
            root / base,
            {
                "process_task_id": process_task_id or f"prc-{task_id}",
                "run_id": run,
                "task_id": task_id,
                "workspace_id": workspace_id,
                "project_id": project_id,
                "finished_at": datetime.utcnow().isoformat() + "Z",
                "storage_mode": mode,
            },
        )
        uri = _local_authority_uri(dest)
        logger.info("处理结果已发布到本地 process/ [%s]: %s", mode, uri)
        return uri

    bucket = oss_client.bucket_name("process")
    uri = _upload_verified(src, bucket, prefix)
    if mode == "cloud" and not keep_local_object_mirror(mode):
        try:
            mirror = Path(settings.storage_root) / "cloud" / bucket / prefix.rstrip("/")
            if mirror.exists():
                shutil.rmtree(mirror, ignore_errors=True)
        except OSError:
            pass
    _upload_metadata(
        bucket,
        base,
        {
            "process_task_id": process_task_id or f"prc-{task_id}",
            "run_id": run,
            "task_id": task_id,
            "workspace_id": workspace_id,
            "project_id": project_id,
            "output_uri": uri,
            "finished_at": datetime.utcnow().isoformat() + "Z",
            "storage_mode": mode,
        },
    )
    logger.info("预处理结果已上云 [%s]: %s", mode, uri)
    return uri


def zip_extract_cache_key(source: str | Path) -> str:
    """Stable key for ZIP extraction cache directory (prefers oss:// URI for terminal cleanup association)."""
    text = str(source).strip()
    if is_cloud_uri(text):
        bucket, key = parse_storage_uri(text)
        digest_input = f"{bucket}/{key.lstrip('/')}"
    else:
        digest_input = str(Path(source).resolve())
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:16]


def zip_extract_hot_dir(source: str | Path) -> Path:
    """QRDF zip extraction cache directory (hot/zip_extract/, not written next to cloud/ mirror)."""
    return Path(settings.storage_root) / "hot" / "zip_extract" / zip_extract_cache_key(source)


def _zip_extract_source_variants(source: str | Path) -> list[str]:
    """Source variants matchable during zip extraction cache cleanup (URI and mirror local path)."""
    text = str(source).strip()
    variants = [text]
    if is_cloud_uri(text):
        try:
            bucket, key = parse_storage_uri(text)
            cloud_root = (Path(settings.storage_root) / "cloud").resolve()
            variants.append(str((cloud_root / bucket / key).resolve()))
            variants.append(str((cloud_root / bucket / key.rstrip("/")).resolve()))
        except Exception:
            pass
    return variants


def cleanup_zip_extract_staging(*sources: str | Path | None) -> list[str]:
    """Clean up extraction cache under hot/zip_extract/ associated with given oss:// or zip path."""
    removed: list[str] = []
    seen_paths: set[str] = set()
    source_refs: set[str] = set()
    extract_root = (Path(settings.storage_root) / "hot" / "zip_extract").resolve()

    def _remove(path: Path) -> None:
        key = str(path.resolve())
        if key in seen_paths or not path.exists():
            return
        seen_paths.add(key)
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
            removed.append(key)
            logger.info("已清理 zip 解压缓存: %s", path)
        except OSError as exc:
            logger.warning("清理 zip 解压缓存失败 %s: %s", path, exc)

    for raw in sources:
        if not raw:
            continue
        variants = _zip_extract_source_variants(raw)
        source_refs.update(variants)
        for source in variants:
            _remove(zip_extract_hot_dir(source).resolve())

    # source tag match (covers historical cache with local path as key)
    if not extract_root.exists() or not source_refs:
        return removed
    for marker in extract_root.glob("*/.source_ref"):
        try:
            ref = marker.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if ref in source_refs:
            _remove(marker.parent.resolve())
    return removed


def _outermost_paths(paths: list[Path]) -> list[Path]:
    """Retain only outermost paths, discarding child paths that will be deleted along with their parent directory."""
    unique = sorted({p.resolve() for p in paths}, key=lambda item: len(item.parts))
    roots: list[Path] = []
    for candidate in unique:
        if any(candidate == root or str(candidate).startswith(str(root) + "/") for root in roots):
            continue
        roots = [
            root
            for root in roots
            if not (root == candidate or str(root).startswith(str(candidate) + "/"))
        ]
        roots.append(candidate)
    return roots


def _expand_cloud_cleanup_roots(bucket: str, key: str, cloud_root: Path) -> list[Path]:
    """Infer local cloud mirror root to delete entirely based on oss key (including metadata sidecars and legacy _qrdf dirs)."""
    norm_key = (key or "").strip().lstrip("/")
    if not norm_key:
        return []

    base = cloud_root / bucket
    roots: list[Path] = []

    def add(rel: str) -> None:
        rel_key = rel.strip("/")
        if rel_key:
            roots.append((base / rel_key).resolve())

    add(norm_key.rstrip("/"))
    if norm_key != norm_key.rstrip("/"):
        add(norm_key)

    parts = norm_key.split("/")
    if not parts:
        return roots

    leaf = parts[-1]

    # raw/ws*/proj*/task*/{session}/file -> entire session directory
    if parts[0] == "raw" and len(parts) >= 6:
        add("/".join(parts[:-1]))
        if leaf.lower().endswith(".zip"):
            add(f"{'/'.join(parts[:-1])}/{Path(leaf).stem}_qrdf")

    # process/.../task*/{run_id}/output -> run directory (including metadata.json)
    if parts[0] == "process" and leaf == "output" and len(parts) >= 6:
        add("/".join(parts[:-1]))

    # exports/.../exp{N}/file -> exp directory (including metadata.json)
    if parts[0] == "exports" and len(parts) >= 2 and parts[-2].startswith("exp"):
        add("/".join(parts[:-1]))

    # datasets/ws*/proj*/{dataset_id}/... -> dataset root directory
    if parts[0] == "datasets" and len(parts) >= 4:
        add("/".join(parts[:4]))

    return roots


def _is_platform_staging_key(key: str) -> bool:
    """Only OSS keys written by the platform pipeline can have their local mirrors cleaned; external scan source paths are excluded."""
    parts = (key or "").strip().lstrip("/").split("/")
    if not parts:
        return False
    head = parts[0]
    if (
        head == "raw"
        and len(parts) >= 5
        and parts[1].startswith("ws")
        and parts[2].startswith("proj")
    ):
        return parts[3].startswith("task")
    if (
        head == "process"
        and len(parts) >= 5
        and parts[1].startswith("ws")
        and parts[2].startswith("proj")
    ):
        return parts[3].startswith("task")
    if (
        head == "datasets"
        and len(parts) >= 4
        and parts[1].startswith("ws")
        and parts[2].startswith("proj")
    ):
        return True
    if (
        head == "exports"
        and len(parts) >= 4
        and parts[1].startswith("ws")
        and parts[2].startswith("proj")
    ):
        return True
    return False


def _filter_cleanable_cloud_uris(*uris: str | None) -> list[str]:
    """Retain only task-dedicated staging paths, excluding shared objects such as external OSS scan sources."""
    result: list[str] = []
    seen: set[str] = set()
    for raw in uris:
        if not raw or not is_cloud_uri(str(raw)):
            continue
        try:
            _, key = parse_storage_uri(str(raw))
        except Exception:
            continue
        if not _is_platform_staging_key(key):
            logger.debug("跳过非平台 staging URI，不清理: %s", raw)
            continue
        if raw not in seen:
            seen.add(raw)
            result.append(str(raw))
    return result


def cleanup_local_staging(*paths: str | Path | None) -> list[str]:
    """Clean up local staging (hot/ only) after single-package step completion, preserving canonical data such as raw/process/official/exports."""
    root = _storage_root_resolved()
    removed: list[str] = []
    for raw in paths:
        if not raw or is_cloud_uri(str(raw)):
            continue
        try:
            p = Path(str(raw))
            resolved = p.resolve() if p.is_absolute() else (root / p).resolve()
        except OSError:
            continue
        if resolved != root and root not in resolved.parents:
            continue
        # Only allow cleaning hot/ staging; skip persistent namespaces and cloud mirrors
        if not _is_under_hot(resolved, root):
            logger.debug("跳过非 hot 暂存清理: %s", resolved)
            continue
        if not resolved.exists():
            continue
        try:
            if resolved.is_dir():
                shutil.rmtree(resolved, ignore_errors=True)
            else:
                resolved.unlink(missing_ok=True)
            removed.append(str(resolved))
            logger.info("已清理本地暂存: %s", resolved)
        except OSError as exc:
            logger.warning("清理本地暂存失败 %s: %s", resolved, exc)
    return removed


def cleanup_cloud_mirror(*storage_paths: str | None) -> list[str]:
    """Clean up OSS local mirror under storage/cloud/ to release disk space after terminal task state (approved/rejected/failed/terminated).

    Accepts one or more oss:// URIs or local paths. In addition to exact URI paths, deletes according to pipeline conventions:
    - raw upload session directory, legacy cloud sidecar extracted ``{stem}_qrdf``
    - process run directory (including metadata.json)
    - export exp directory (including metadata.json)
    - official dataset root directory

    After deletion, cleans up empty parent directories up to the cloud bucket root.
    Upon retry, materialize_for_processing re-downloads from OSS to a temporary directory without depending on cloud/ mirror.

    Refuses cleanup when real OSS is not available (mirror is the canonical copy).
    """
    from data.services.storage_mode import can_purge_cloud_mirrors

    if not can_purge_cloud_mirrors():
        logger.info("跳过 cloud 镜像清理：真实 OSS 不可达，本地镜像为权威副本")
        return []

    cloud_root = (Path(settings.storage_root) / "cloud").resolve()
    removed: list[str] = []
    delete_roots: list[Path] = []

    def _remove_empty_parents(path: Path) -> None:
        """Clean empty parent directories upwards, stopping at the cloud bucket root."""
        parent = path.parent.resolve()
        while (
            str(parent).startswith(str(cloud_root))
            and parent != cloud_root
            and parent.parent != cloud_root
        ):
            try:
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
                    removed.append(str(parent))
                    logger.info("已清理空目录: %s", parent)
                    parent = parent.parent
                else:
                    break
            except OSError:
                break

    for raw in storage_paths:
        if not raw:
            continue

        if is_cloud_uri(raw):
            try:
                bucket, key = parse_storage_uri(raw)
                if not _is_platform_staging_key(key):
                    logger.debug("跳过非平台 staging URI，不清理 cloud 镜像: %s", raw)
                    continue
                delete_roots.extend(_expand_cloud_cleanup_roots(bucket, key, cloud_root))
            except Exception:
                logger.debug("无法解析 cloud URI，跳过: %s", raw)
                continue
            continue

        local_resolved = Path(raw).resolve()
        if str(local_resolved).startswith(str(cloud_root) + "/") or local_resolved == cloud_root:
            delete_roots.append(local_resolved)
            continue
        # No longer match cloud/ via filename rglob to avoid mistakenly deleting files of other tasks with identical names

    for path in _outermost_paths(delete_roots):
        if not str(path).startswith(str(cloud_root)):
            continue
        if not path.exists():
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
            removed.append(str(path))
            logger.info("已清理 cloud 镜像: %s", path)
            _remove_empty_parents(path)
        except OSError as exc:
            logger.warning("清理 cloud 镜像失败 %s: %s", path, exc)

    if removed:
        try:
            from data.security.audit import emit_audit_event

            emit_audit_event(
                "storage.cleanup",
                resource="cloud_mirror",
                detail={"removed_count": len(removed), "paths": removed[:20]},
            )
        except Exception:
            logger.debug("storage.cleanup audit skipped", exc_info=True)
    return removed


def cleanup_task_previews(task_or_id) -> list[str]:
    """Clean up preview cache directories bound to task (previews/task{id}/, previews/topics/task{id}/)."""
    task_id = task_or_id if isinstance(task_or_id, int) else getattr(task_or_id, "id", None)
    if not task_id:
        return []

    previews_root = (Path(settings.storage_root) / "previews").resolve()
    removed: list[str] = []
    for rel in (f"task{task_id}", Path("topics") / f"task{task_id}"):
        path = (previews_root / rel).resolve()
        if not str(path).startswith(str(previews_root)) or not path.exists():
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
            removed.append(str(path))
            logger.info("已清理任务预览缓存: %s", path)
        except OSError as exc:
            logger.warning("清理任务预览缓存失败 %s: %s", path, exc)
    return removed


def cleanup_export_staging(export_job_id: int | str) -> list[str]:
    """Clean up local working directory under exports/ for specified export task (callable on success/failure/retry)."""
    job_id = str(export_job_id).strip()
    if not _EXPORT_STAGING_JOB_ID.fullmatch(job_id):
        logger.warning("refused export staging cleanup with invalid job id")
        return []
    export_dir = Path(settings.storage_root) / "exports"
    removed: list[str] = []
    if not export_dir.is_dir():
        return removed
    for path in sorted(export_dir.glob(f"export_{job_id}_*")):
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.is_file():
                path.unlink(missing_ok=True)
            removed.append(str(path.resolve()))
            logger.info("已清理导出暂存: %s", path)
        except OSError as exc:
            logger.warning("清理导出暂存失败 %s: %s", path, exc)
    return removed


def cleanup_task_terminal_storage(task) -> dict[str, list[str]]:
    """Task terminal state/termination: release all cloud local mirrors (including official) and preview cache.

    Only deletes copies under storage/cloud/, does not delete OSS cloud objects.
    """
    uris = _filter_cleanable_cloud_uris(*collect_all_task_cloud_uris(task))
    cloud = cleanup_cloud_mirror(*uris)
    zip_extract = cleanup_zip_extract_staging(*uris)
    previews = cleanup_task_previews(task)
    return {"cloud": cloud, "zip_extract": zip_extract, "previews": previews}


def collect_all_task_cloud_uris(task) -> list[str]:
    """Collect all oss:// URIs associated with task for terminal cleanup of local cloud/ mirrors (including official bucket)."""
    uris = list(collect_task_intermediate_uris(task))
    sp = task.storage_path
    if sp and is_cloud_uri(str(sp)):
        uris.append(str(sp))

    meta = dict(task.metadata_json or {})
    for key in ("official_uri", "raw_uri", "cloud_uri"):
        val = meta.get(key)
        if val and is_cloud_uri(str(val)):
            uris.append(str(val))

    audit_meta = meta.get("audit") or {}
    for key in ("source_uri", "dataset_storage"):
        val = audit_meta.get(key)
        if val and is_cloud_uri(str(val)):
            uris.append(str(val))

    seen: set[str] = set()
    result: list[str] = []
    for uri in uris:
        if uri and uri not in seen:
            seen.add(uri)
            result.append(uri)
    return result


def cleanup_export_job_cloud_mirrors(
    *,
    export_uri: str | None = None,
    source_storage_paths: list[str] | None = None,
) -> list[str]:
    """Clean up export bucket and materialized source dataset local cloud/ mirrors after export task finishes."""
    uris: list[str] = []
    if export_uri and is_cloud_uri(export_uri):
        uris.append(export_uri)
    for raw in source_storage_paths or []:
        if raw and is_cloud_uri(str(raw)):
            uris.append(str(raw))
    seen: set[str] = set()
    deduped: list[str] = []
    for uri in uris:
        if uri not in seen:
            seen.add(uri)
            deduped.append(uri)
    deduped = _filter_cleanable_cloud_uris(*deduped)
    removed = cleanup_cloud_mirror(*deduped)
    removed.extend(cleanup_zip_extract_staging(*deduped))
    return removed


def collect_preprocess_uris(task) -> list[str]:
    """Collect OSS URIs of preprocessing outputs in task metadata (for cleaning old runs before retry)."""
    preprocess = (task.metadata_json or {}).get("preprocess") or {}
    uris: list[str] = []
    for key in ("qrdf_dataset_path", "cloud_uri"):
        val = preprocess.get(key)
        if val and is_cloud_uri(str(val)):
            uris.append(str(val))
    storage_path = task.storage_path
    if storage_path and is_cloud_uri(str(storage_path)) and "/process/" in str(storage_path):
        uris.append(str(storage_path))
    seen: set[str] = set()
    result: list[str] = []
    for uri in uris:
        if uri not in seen:
            seen.add(uri)
            result.append(uri)
    return result


def collect_raw_stage_uris(task) -> list[str]:
    """Collect task raw stage output URIs (local mirrors can be cleaned once entering annotation)."""
    meta = dict(task.metadata_json or {})
    collect = meta.get("collect") or {}
    uris: list[str] = []
    for key in ("raw_uri", "storage_uri"):
        val = collect.get(key)
        if val and is_cloud_uri(str(val)):
            uris.append(str(val))
    # Compatibility with legacy field
    for key in ("raw_uri", "cloud_uri"):
        val = meta.get(key)
        if val and is_cloud_uri(str(val)):
            uris.append(str(val))
    return _filter_cleanable_cloud_uris(*uris)


def collect_process_stage_uris(task) -> list[str]:
    """Collect task process stage output URIs (local mirrors can be cleaned once approved into official)."""
    preprocess = (task.metadata_json or {}).get("preprocess") or {}
    uris: list[str] = []
    for key in ("qrdf_dataset_path", "cloud_uri"):
        val = preprocess.get(key)
        if val and is_cloud_uri(str(val)):
            uris.append(str(val))
    sp = task.storage_path
    if sp and is_cloud_uri(str(sp)) and "/process/" in str(sp):
        uris.append(str(sp))
    return _filter_cleanable_cloud_uris(*uris)


def collect_task_intermediate_uris(task) -> list[str]:
    """Collect intermediate pipeline data OSS URIs generated during task lifecycle for terminal cleanup.

    Only returns URIs for intermediate buckets such as raw/process; does not return final artifacts such as official datasets or export results.
    """
    meta = dict(task.metadata_json or {})
    uris: list[str] = []

    # Ingestion stage: only clean artifacts copied to dedicated raw directory (do not clean external OSS scan source paths)
    collect = meta.get("collect") or {}
    if collect.get("raw_uri"):
        uris.append(collect["raw_uri"])
    elif collect.get("storage_uri"):
        uris.append(collect["storage_uri"])

    # Compatibility with legacy field
    for key in ("raw_uri", "cloud_uri"):
        val = meta.get(key)
        if val:
            uris.append(val)

    # Preprocessing output
    preprocess = meta.get("preprocess") or {}
    for key in ("qrdf_dataset_path", "cloud_uri"):
        val = preprocess.get(key)
        if val:
            uris.append(val)

    # MCAP -> QRDF conversion output
    mcap = meta.get("mcap_to_qrdf") or {}
    for key in ("cloud_uri", "storage_path"):
        val = mcap.get(key)
        if val:
            uris.append(val)

    # Intermediate path in task.storage_path (non-official/export final artifact)
    sp = task.storage_path
    if sp and is_cloud_uri(sp):
        raw_bucket = oss_client.bucket_name("raw")
        process_bucket = oss_client.bucket_name("process")
        if raw_bucket in sp or process_bucket in sp:
            uris.append(sp)

    seen: set[str] = set()
    result: list[str] = []
    for uri in uris:
        if uri and uri not in seen:
            seen.add(uri)
            result.append(uri)
    return result


@contextmanager
def materialize_for_processing(storage_path: str | None) -> Iterator[Path | None]:
    """Materialize cloud or local storage_path into a local path for atomic SDK processing.

    - local / non-oss://: Resolve directly without creating copies.
    - hybrid: Prioritize cloud/ mirror, fallback to downloading from OSS into temporary directory if missing.
    - cloud: Skip long-term mirror by default (prefer_temp), pull directly from OSS to temporary directory.
    Temporary directories are automatically cleaned up upon context exit.
    """
    if not storage_path:
        yield None
        return

    if is_cloud_uri(storage_path):
        from data.services.storage_mode import prefer_temp_materialize

        try:
            bucket, key = parse_storage_uri(storage_path)
        except ValueError:
            yield None
            return
        if not cloud_path_has_required_authority(bucket, key):
            yield None
            return
        # Materialized directory must reside within storage_root; otherwise downstream resolve_storage_path / QRDF parsing will reject it
        root = storage_root_path()
        materialize_root = (root / "hot" / "materialize").resolve()
        if not is_under_storage_root(materialize_root, root=root):
            yield None
            return
        materialize_root.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="quicdata_cloud_", dir=str(materialize_root)))
        try:
            if not is_under_storage_root(tmp, root=materialize_root):
                yield None
                return
            use_mirror = not prefer_temp_materialize()
            mirror_key = key.rstrip("/")
            mirror = resolve_cloud_mirror_path(bucket, mirror_key)
            alternate_mirror = resolve_cloud_mirror_path(bucket, key)
            if mirror is None or alternate_mirror is None:
                yield None
                return
            if not mirror.exists():
                mirror = alternate_mirror
                mirror_key = key
            if (
                use_mirror
                and mirror.exists()
                and (mirror.is_file() or any(mirror.iterdir()))
                and oss_client.mirror_is_complete(bucket, key, mirror)
            ):
                oss_client.copy_local_mirror_to(
                    tmp,
                    bucket,
                    mirror_key,
                    include_source_root=True,
                )
            else:
                oss_client.download_to(tmp, bucket, key)
            resolved = _resolve_downloaded(tmp, key)
            if resolved is None or not is_under_storage_root(resolved, root=tmp.resolve()):
                yield None
            else:
                yield resolved.resolve()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return

    yield resolve_storage_path(storage_path)


def _resolve_downloaded(tmp: Path, key: str) -> Path | None:
    """Locate actual data root (single file or directory) after download."""
    if not tmp.exists():
        return None
    entries = list(tmp.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    if len(entries) == 1 and entries[0].is_file():
        return entries[0]
    # When key ends with filename, download_to might land directly under tmp
    name = Path(key).name
    candidate = tmp / name
    if candidate.exists():
        return candidate
    return tmp if any(tmp.iterdir()) else None


def _today() -> str:
    return datetime.utcnow().strftime("%Y%m%d")


def _upload_metadata(bucket: str, prefix: str, metadata: dict) -> None:
    """Write metadata.json sidecar file."""
    tmp = Path(tempfile.mkdtemp())
    try:
        meta_file = tmp / "metadata.json"
        meta_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        _upload_verified(meta_file, bucket, f"{prefix.rstrip('/')}/metadata.json")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def publish_annotation_patch(
    local_qrdf_root: Path,
    *,
    source_cloud_uri: str,
    task_id: int,
) -> str:
    """Upload only added/modified annotation files after approval without re-uploading the entire dataset.

    local mode or non-oss:// source: already written to disk locally, returns canonical path directly.
    """
    from data.services.storage_mode import uses_cloud_uri_authority
    from data.utils.storage_uri import is_cloud_uri as _is_cloud

    if not uses_cloud_uri_authority() or not _is_cloud(source_cloud_uri):
        logger.info("标注结果保留本地权威路径: %s", source_cloud_uri or local_qrdf_root)
        return source_cloud_uri or _local_authority_uri(Path(local_qrdf_root))

    from data.infra import oss_client as _oss
    from data.utils.storage_uri import parse_storage_uri

    src_bucket, src_key = parse_storage_uri(source_cloud_uri)
    prefix = src_key.rstrip("/")

    # Only upload small annotation-related files (JSON / JSONL)
    annotation_suffixes = (".json", ".jsonl")
    uploaded: list[str] = []
    for item in local_qrdf_root.rglob("*"):
        if not item.is_file():
            continue
        if not item.name.endswith(annotation_suffixes):
            continue
        rel = item.relative_to(local_qrdf_root).as_posix()
        key = f"{prefix}/{rel}"
        _oss.upload_file(item, src_bucket, key)
        uploaded.append(rel)

    _upload_metadata(
        src_bucket,
        prefix,
        {
            "task_id": task_id,
            "annotation_patched_at": datetime.utcnow().isoformat() + "Z",
            "patched_files": uploaded,
        },
    )
    logger.info("标注增量上传完成: %s ( %d 个文件)", source_cloud_uri, len(uploaded))
    return source_cloud_uri


def promote_to_official_dataset(
    source_storage_path: str,
    *,
    dataset_id: str,
    task_id: int,
    workspace_id: int | None = None,
    project_id: int | None = None,
    metadata: dict | None = None,
) -> str:
    """Approval -> official namespace.

    local: Copy to storage_root/official/... and return local canonical URI.
    hybrid/cloud: Copy/upload to quic-qrdf official bucket.
    """
    from data.services.storage_mode import get_effective_storage_mode, uses_cloud_uri_authority

    mode = get_effective_storage_mode()
    ws = f"ws{workspace_id}" if workspace_id else "ws0"
    proj = f"proj{project_id}" if project_id else "proj0"
    dst_prefix = f"datasets/{ws}/{proj}/{dataset_id}"

    if mode == "local" or not uses_cloud_uri_authority():
        root = Path(settings.storage_root).resolve()
        dest = root / "official" / ws / proj / dataset_id
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if is_cloud_uri(source_storage_path):
            # Theoretically local mode should not have oss://; fallback: materialize then copy
            with materialize_for_processing(source_storage_path) as local_ds:
                if not local_ds or not local_ds.exists():
                    raise FileNotFoundError(f"无法定位审核数据源: {source_storage_path}")
                if local_ds.is_dir():
                    shutil.copytree(local_ds, dest)
                else:
                    dest.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(local_ds, dest / local_ds.name)
        else:
            local_path = resolve_storage_path(source_storage_path)
            if not local_path or not local_path.exists():
                raise FileNotFoundError(f"无法定位审核数据源: {source_storage_path}")
            if local_path.is_dir():
                shutil.copytree(local_path, dest)
            else:
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local_path, dest / local_path.name)
        meta = metadata or {}
        meta.update(
            {
                "dataset_id": dataset_id,
                "task_id": task_id,
                "approved_at": datetime.utcnow().isoformat() + "Z",
                "source_uri": source_storage_path,
                "storage_mode": mode,
            }
        )
        (dest / "metadata.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        uri = _local_authority_uri(dest)
        logger.info("正式数据集已本地入库 [%s]: %s", mode, uri)
        return uri

    official_bucket = oss_client.bucket_name("official")

    if is_cloud_uri(source_storage_path):
        src_bucket, src_key = parse_storage_uri(source_storage_path)
        if (
            not cloud_path_is_safe(src_bucket, src_key)
            or resolve_cloud_mirror_path(src_bucket, src_key.rstrip("/")) is None
        ):
            raise ValueError("provider object path is invalid")
        uri = oss_client.copy_prefix(src_bucket, src_key, official_bucket, dst_prefix)
    else:
        local_path = resolve_storage_path(source_storage_path)
        if not local_path or not local_path.exists():
            raise FileNotFoundError(f"无法定位审核数据源: {source_storage_path}")
        uri = _upload_verified(local_path, official_bucket, dst_prefix)

    meta = metadata or {}
    meta.update(
        {
            "dataset_id": dataset_id,
            "task_id": task_id,
            "approved_at": datetime.utcnow().isoformat() + "Z",
            "source_uri": source_storage_path,
            "storage_mode": mode,
        }
    )
    _upload_metadata(official_bucket, dst_prefix, meta)
    logger.info("正式数据集已入库 [%s]: %s", mode, uri)
    return f"oss://{official_bucket}/{dst_prefix}/"


def publish_export_output(
    local_export_path: Path,
    *,
    export_job_id: int | str,
    workspace_id: int | None = None,
    project_id: int | None = None,
    dataset_id: int | None = None,
    version: str | None = None,
    lerobot_version: str | None = None,
    template: str | None = None,
    export_task_id: str | None = None,
) -> str:
    """Export completed -> export bucket or local exports/ persistent namespace."""
    from data.services.storage_mode import get_effective_storage_mode, uses_cloud_uri_authority

    mode = get_effective_storage_mode()
    src = Path(local_export_path)
    ws = f"ws{workspace_id}" if workspace_id else "ws0"
    proj = f"proj{project_id}" if project_id else "proj0"
    ds = f"ds{dataset_id}" if dataset_id else "ds0"
    lr_ver = lerobot_version or ("native" if template == "qrdf" else "v3.0")
    ver = version or "v0"
    tpl = template or "generic"
    prefix = f"exports/{ws}/{proj}/{ds}/{ver}/{lr_ver}/{tpl}/exp{export_job_id}"
    key = f"{prefix}/{src.name}"

    if mode == "local" or not uses_cloud_uri_authority():
        root = _storage_root_resolved()
        dest = root / key
        _copy_to_local_dest(src, dest)
        _write_local_sidecar_metadata(
            root / prefix,
            {
                "export_task_id": export_task_id or f"exp-{export_job_id}",
                "export_job_id": export_job_id,
                "workspace_id": workspace_id,
                "project_id": project_id,
                "dataset_id": dataset_id,
                "version": version,
                "lerobot_version": lerobot_version,
                "template": template,
                "finished_at": datetime.utcnow().isoformat() + "Z",
                "storage_mode": mode,
            },
        )
        uri = _local_authority_uri(dest)
        logger.info("导出结果已发布到本地 exports/ [%s]: %s", mode, uri)
        return uri

    bucket = oss_client.bucket_name("export")
    uri = _upload_verified(src, bucket, key)
    _upload_metadata(
        bucket,
        prefix,
        {
            "export_task_id": export_task_id or f"exp-{export_job_id}",
            "export_job_id": export_job_id,
            "workspace_id": workspace_id,
            "project_id": project_id,
            "dataset_id": dataset_id,
            "version": version,
            "lerobot_version": lerobot_version,
            "template": template,
            "finished_at": datetime.utcnow().isoformat() + "Z",
            "storage_mode": mode,
        },
    )
    logger.info("导出结果已上云 [%s]: %s", mode, uri)
    return uri


def make_dataset_id(project_id: int, qrdf_id: int) -> str:
    return f"ds-p{project_id}-{qrdf_id:06d}"
