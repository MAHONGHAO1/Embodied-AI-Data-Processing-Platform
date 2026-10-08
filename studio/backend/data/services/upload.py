"""Chunked upload service (SQLite + Redis dual-write cache)."""

import hashlib
import os
import re
import shutil
import tarfile
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from filelock import FileLock
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from data.config import get_runtime_config, settings
from data.database import JobRun, Project, Task, TaskStatus, UploadMergeAttempt, UploadSession
from data.infra.redis_client import redis_service
from data.services.cloud_storage import (
    RawUploadTarget,
    cleanup_raw_upload_prefix,
    plan_raw_upload,
    publish_raw_upload,
)
from data.services.job_runs import LeaseOwnershipLost, create_or_get_job, database_now
from data.services.provenance import assert_collection_attribution_complete
from data.services.state_machine import transit_task
from data.services.workspace_scope import resolve_task_workspace_id

UPLOAD_STREAM_BLOCK_BYTES = 1024 * 1024
UPLOAD_MERGE_JOB_KIND = "collect_upload_merge"
UPLOAD_MERGE_QUEUE = "media"
MAX_UPLOAD_CHUNKS = 10_000
MAX_UPLOAD_CHUNK_BYTES = 64 * 1024 * 1024
MAX_UPLOAD_TOTAL_BYTES = 6 * 1024 * 1024 * 1024
RAW_STATE_RAW_COPY_PENDING = "raw_copy_pending"
RAW_STATE_RAW_COPIED = "raw_copied"
RAW_STATE_CHECKSUM_VERIFIED = "checksum_verified"
RAW_STATE_PROCESSING_READY = "processing_ready"
RAW_STATE_RAW_COPY_FAILED = "raw_copy_failed"


def validate_file_extension(file_name: str) -> None:
    allowed = get_runtime_config().get("storage", {}).get("allowed_extensions") or []
    if not allowed:
        return
    name_lower = file_name.lower()
    for ext in sorted(allowed, key=len, reverse=True):
        if name_lower.endswith(str(ext).lower()):
            return
    raise ValueError(f"不支持的文件格式，允许: {', '.join(allowed)}")


def _list_archive_names(archive: Path) -> list[str]:
    """List file names inside archive (zip/tar), returning empty on failure."""
    name = archive.name.lower()
    try:
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive, "r") as zf:
                return zf.namelist()
        if name.endswith((".tar", ".tar.gz", ".tgz")):
            with tarfile.open(archive, "r:*") as tf:
                return [m.name for m in tf.getmembers()]
    except Exception:
        pass
    return []


def _archive_is_qrdf(archive: Path) -> bool:
    """Detect whether archive contains a QRDF dataset or single episode package structure."""
    episode_id_re = re.compile(r"(^|/)episode_\d{6}(/|$)")
    names = _list_archive_names(archive)
    if not names:
        return False
    has_dataset_json = False
    has_episodes_dir = False
    has_episode_metadata = False
    has_data_mcap = False
    for n in names:
        base = n.rstrip("/")
        lower = base.lower()
        if base.endswith("dataset.json") or lower.endswith("/dataset.json"):
            has_dataset_json = True
        if "/episodes/" in n or n.startswith("episodes/") or n.rstrip("/") == "episodes":
            has_episodes_dir = True
        if lower.endswith("metadata.json") and episode_id_re.search(n):
            has_episode_metadata = True
        if lower.endswith("metadata.json") and base.count("/") <= 1:
            # Root-level or single-tier metadata.json (common single episode package layout)
            has_episode_metadata = True
        if lower.endswith("data.mcap"):
            has_data_mcap = True
    if has_dataset_json or has_episodes_dir:
        return True
    # Single episode package: metadata.json + data.mcap (dataset.json optional)
    if has_episode_metadata and has_data_mcap:
        return True
    return False


# QRDF archive filename tag convention (hit directly identifies as QRDF, avoiding decompression for namelist parsing)
# Only supports zip: aligned with decompression capability of resolve_dataset_path's _extract_zip_dataset
_QRDF_NAME_SUFFIXES = (".qrdf.zip", "_qrdf.zip")
_EPISODE_ZIP_NAME = re.compile(r"^episode_\d{6}\.zip$", re.IGNORECASE)


def detect_upload_format(local_path: Path) -> str:
    """Identify upload file format: qrdf / mcap / unknown.

    Prioritize identification by filename tags (e.g., xxx.qrdf.zip / episode_000001.zip),
    fallback to inspecting archive contents if no match.
    """
    name = local_path.name.lower()
    if name.endswith(".mcap"):
        return "mcap"
    if any(name.endswith(m) for m in _QRDF_NAME_SUFFIXES):
        return "qrdf"
    if _EPISODE_ZIP_NAME.match(local_path.name):
        return "qrdf"
    if name.endswith(".zip") and _archive_is_qrdf(local_path):
        return "qrdf"
    return "unknown"


class UploadService:
    def __init__(self) -> None:
        # Resolve paths for each operation: tests and runtime config can change
        # storage_root after this process has imported the module.
        self._ensure_storage_directories()

    @property
    def root(self) -> Path:
        return Path(settings.storage_root).expanduser().resolve()

    @property
    def chunk_dir(self) -> Path:
        return self.root / "chunks"

    @property
    def hot_dir(self) -> Path:
        return self.root / "hot"

    def _ensure_storage_directories(self) -> None:
        self.chunk_dir.mkdir(parents=True, exist_ok=True)
        self.hot_dir.mkdir(parents=True, exist_ok=True)

    def init_upload(
        self,
        db: Session,
        file_md5: str,
        total_chunks: int,
        file_name: str,
        task_id: int | None = None,
        *,
        owner_user_id: int,
        workspace_id: int | None,
    ) -> dict:
        safe_file_name = _safe_upload_file_name(file_name)
        validate_file_extension(safe_file_name)
        if total_chunks <= 0 or total_chunks > MAX_UPLOAD_CHUNKS:
            raise ValueError("total_chunks exceeds the upload limit")
        if not _is_md5_digest(file_md5):
            raise ValueError("file_md5 must be a canonical MD5 digest")
        file_md5 = file_md5.lower()
        if task_id is not None:
            task = db.get(Task, task_id)
            if task is None:
                raise ValueError("upload task does not exist")
            task_workspace_id = resolve_task_workspace_id(db, task)
            if task_workspace_id is None or task_workspace_id != workspace_id:
                raise ValueError("upload workspace does not match task workspace")
        self._ensure_storage_directories()
        resume_key = _upload_resume_key(
            file_md5=file_md5,
            total_chunks=total_chunks,
            file_name=safe_file_name,
            task_id=task_id,
            owner_user_id=owner_user_id,
            workspace_id=workspace_id,
        )

        existing = (
            db.query(UploadSession)
            .filter(
                UploadSession.resume_key == resume_key,
            )
            .order_by(UploadSession.created_at.desc())
            .first()
        )
        if existing:
            progress = self.get_upload_progress(existing)
            return {
                "upload_id": existing.id,
                "uploaded_chunks": progress["uploaded_chunks"],
                "resumed": True,
            }

        upload_id = uuid.uuid4().hex
        session = UploadSession(
            id=upload_id,
            file_name=safe_file_name,
            file_md5=file_md5,
            total_chunks=total_chunks,
            uploaded_chunks=[],
            task_id=task_id,
            owner_user_id=owner_user_id,
            workspace_id=workspace_id,
            resume_key=resume_key,
            status="uploading",
        )
        db.add(session)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.scalar(
                select(UploadSession).where(UploadSession.resume_key == resume_key)
            )
            if existing is None:
                raise
            progress = self.get_upload_progress(existing)
            return {
                "upload_id": existing.id,
                "uploaded_chunks": progress["uploaded_chunks"],
                "resumed": True,
            }
        redis_service.set_upload_status(
            upload_id,
            {"uploaded_chunks": [], "total_chunks": total_chunks, "status": "uploading"},
        )
        return {"upload_id": upload_id, "uploaded_chunks": [], "resumed": False}

    def save_chunk(self, db: Session, upload_id: str, chunk_index: int, content: bytes) -> dict:
        """Compatibility helper for callers that already own bounded bytes."""
        session, chunk_path, uploaded = self._prepare_chunk(db, upload_id, chunk_index)
        if chunk_path is None:
            return {"chunk_index": chunk_index, "uploaded_count": len(uploaded), "skipped": True}
        if not isinstance(content, bytes):
            raise ValueError("上传分片必须是二进制内容")
        self._validate_chunk_size(db, session, chunk_index, len(content))
        return self._write_prepared_chunk(db, session, chunk_path, uploaded, chunk_index, content)

    def _write_prepared_chunk(
        self,
        db: Session,
        session: UploadSession,
        chunk_path: Path,
        uploaded: list[int],
        chunk_index: int,
        content: bytes,
    ) -> dict:
        digest = hashlib.sha256()
        temporary = _temporary_path(chunk_path)
        try:
            with temporary.open("xb") as destination:
                destination.write(content)
                digest.update(content)
                destination.flush()
                os.fsync(destination.fileno())
            with self._session_write_lock(session):
                self._validate_chunk_size(db, session, chunk_index, len(content))
                try:
                    os.link(temporary, chunk_path)
                except FileExistsError:
                    result = self._record_uploaded_chunk(
                        db,
                        session,
                        chunk_index,
                        uploaded,
                        size_bytes=chunk_path.stat().st_size,
                        sha256=_sha256_file(chunk_path),
                    )
                    result["skipped"] = True
                    return result
                return self._record_uploaded_chunk(
                    db,
                    session,
                    chunk_index,
                    uploaded,
                    size_bytes=len(content),
                    sha256=digest.hexdigest(),
                )
        finally:
            if temporary.exists():
                temporary.unlink()

    async def save_chunk_stream(
        self, db: Session, upload_id: str, chunk_index: int, upload
    ) -> dict:
        """Persist one multipart chunk without buffering the complete request body."""
        session, chunk_path, uploaded = self._prepare_chunk(db, upload_id, chunk_index)
        if chunk_path is None:
            return {"chunk_index": chunk_index, "uploaded_count": len(uploaded), "skipped": True}
        digest = hashlib.sha256()
        total_bytes = 0
        existing_bytes = self._uploaded_size_bytes(session, exclude_index=chunk_index)
        max_total_bytes = self._max_total_bytes(db, session)
        temporary = _temporary_path(chunk_path)
        try:
            with temporary.open("xb") as destination:
                while True:
                    block = await upload.read(UPLOAD_STREAM_BLOCK_BYTES)
                    if not block:
                        break
                    if not isinstance(block, bytes):
                        raise ValueError("上传分片必须是二进制内容")
                    next_size = total_bytes + len(block)
                    if next_size > MAX_UPLOAD_CHUNK_BYTES:
                        raise ValueError("upload chunk exceeds the chunk size limit")
                    if existing_bytes + next_size > max_total_bytes:
                        raise ValueError("upload total exceeds the session size limit")
                    destination.write(block)
                    digest.update(block)
                    total_bytes += len(block)
                destination.flush()
                os.fsync(destination.fileno())
            with self._session_write_lock(session):
                self._validate_chunk_size(db, session, chunk_index, total_bytes)
                try:
                    os.link(temporary, chunk_path)
                except FileExistsError:
                    result = self._record_uploaded_chunk(
                        db,
                        session,
                        chunk_index,
                        uploaded,
                        size_bytes=chunk_path.stat().st_size,
                        sha256=_sha256_file(chunk_path),
                    )
                    result["skipped"] = True
                    return result
                return self._record_uploaded_chunk(
                    db,
                    session,
                    chunk_index,
                    uploaded,
                    size_bytes=total_bytes,
                    sha256=digest.hexdigest(),
                )
        finally:
            if temporary.exists():
                temporary.unlink()

    @contextmanager
    def _session_write_lock(self, session: UploadSession):
        lock_path = self._chunk_path(session, 0).parent / ".upload.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(lock_path)):
            yield

    def _uploaded_size_bytes(
        self, session: UploadSession, *, exclude_index: int | None = None
    ) -> int:
        total = 0
        for index in session.uploaded_chunks or []:
            if index == exclude_index:
                continue
            path = self._chunk_path(session, index)
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        return total

    def _max_total_bytes(self, db: Session, session: UploadSession) -> int:
        task = db.get(Task, session.task_id) if session.task_id is not None else None
        if task is not None and str(task.task_type or "").strip().lower() == "ego":
            return settings.ego_archive_max_upload_bytes
        return MAX_UPLOAD_TOTAL_BYTES

    def _validate_chunk_size(
        self,
        db: Session,
        session: UploadSession,
        chunk_index: int,
        size_bytes: int,
    ) -> None:
        if size_bytes > MAX_UPLOAD_CHUNK_BYTES:
            raise ValueError("upload chunk exceeds the chunk size limit")
        if self._committed_chunk_size_bytes(
            session, exclude_index=chunk_index
        ) + size_bytes > self._max_total_bytes(db, session):
            raise ValueError("upload total exceeds the session size limit")

    def _committed_chunk_size_bytes(
        self, session: UploadSession, *, exclude_index: int | None = None
    ) -> int:
        chunk_folder = self._chunk_path(session, 0).parent
        if not chunk_folder.exists():
            return 0
        total = 0
        for path in chunk_folder.iterdir():
            if not path.name.isdigit() or int(path.name) == exclude_index:
                continue
            if not path.is_file() or path.is_symlink():
                raise ValueError("upload chunk storage contains an invalid entry")
            total += path.stat().st_size
        return total

    def _prepare_chunk(self, db: Session, upload_id: str, chunk_index: int):
        session = db.get(UploadSession, upload_id)
        if not session:
            raise ValueError("upload_id 不存在")
        if session.status != "uploading":
            raise ValueError("上传会话已结束，无法继续上传")
        if chunk_index < 0 or chunk_index >= session.total_chunks:
            raise ValueError("chunk_index 超出上传范围")

        uploaded: list[int] = list(session.uploaded_chunks or [])
        chunk_path = self._chunk_path(session, chunk_index)
        if chunk_index in uploaded:
            if chunk_path.exists():
                return session, None, uploaded

        chunk_path.parent.mkdir(parents=True, exist_ok=True)
        return session, chunk_path, uploaded

    def _record_uploaded_chunk(
        self,
        db: Session,
        session: UploadSession,
        chunk_index: int,
        uploaded: list[int],
        *,
        size_bytes: int,
        sha256: str,
    ) -> dict:
        try:
            current = db.scalar(
                select(UploadSession)
                .where(UploadSession.id == session.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if current is None:
                raise ValueError("upload_id 不存在")
            if current.status != "uploading":
                raise ValueError("上传会话已结束，无法继续上传")
            current_uploaded = list(current.uploaded_chunks or [])
            if chunk_index in current_uploaded:
                uploaded = current_uploaded
            else:
                uploaded = sorted([*current_uploaded, chunk_index])
                current.uploaded_chunks = uploaded
            db.commit()
        except Exception:
            db.rollback()
            raise

        redis_uploaded = redis_service.add_upload_chunk(
            session.id, chunk_index, session.total_chunks
        )
        if redis_uploaded:
            uploaded = redis_uploaded

        return {
            "chunk_index": chunk_index,
            "uploaded_count": len(uploaded),
            "size_bytes": size_bytes,
            "sha256": sha256,
            "skipped": False,
        }

    def _chunk_path(self, session: UploadSession, chunk_index: int) -> Path:
        self._ensure_storage_directories()
        chunk_root = self.chunk_dir.resolve()
        candidate = (chunk_root / session.id / str(chunk_index)).resolve()
        try:
            candidate.relative_to(chunk_root)
        except ValueError as exc:
            raise ValueError("上传分片路径无效") from exc
        return candidate

    def get_upload_progress(self, session: UploadSession) -> dict:
        upload_id = session.id
        cached = redis_service.get_upload_status(upload_id)
        if cached:
            uploaded = cached.get("uploaded_chunks") or redis_service.get_upload_chunks(upload_id)
            total = cached.get("total_chunks") or session.total_chunks
            progress = round(len(uploaded) / total * 100) if total else 0
            return {
                "uploaded_chunks": uploaded,
                "total_chunks": total,
                "progress": progress,
                "source": "redis",
            }
        uploaded = session.uploaded_chunks or []
        progress = round(len(uploaded) / session.total_chunks * 100) if session.total_chunks else 0
        return {
            "uploaded_chunks": uploaded,
            "total_chunks": session.total_chunks,
            "progress": progress,
            "source": "database",
        }

    def merge_chunks(
        self,
        db: Session,
        upload_id: str,
        file_md5: str,
        *,
        job_id: str,
        lease_token: str,
    ) -> dict:
        session = db.get(UploadSession, upload_id)
        if not session:
            raise ValueError("upload_id 不存在")
        if session.file_md5 != file_md5:
            raise ValueError("MD5 校验失败")
        if not _is_md5_digest(session.file_md5):
            raise ValueError("stored upload digest is invalid")
        self._ensure_storage_directories()
        _assert_upload_merge_owner(db, session, job_id=job_id, lease_token=lease_token)
        dest = self._merged_file_path(session, job_id, lease_token)
        if session.status not in {"merge_pending", "merging"}:
            raise ValueError("上传会话状态不允许合并")
        dest.parent.mkdir(parents=True, exist_ok=True)

        uploaded = session.uploaded_chunks or []
        redis_chunks = redis_service.get_upload_chunks(upload_id)
        if redis_chunks:
            uploaded = redis_chunks
        if len(uploaded) != session.total_chunks:
            raise ValueError(f"分片不完整: {len(uploaded)}/{session.total_chunks}")

        md5 = hashlib.md5(usedforsecurity=False)
        sha256 = hashlib.sha256()
        size_bytes = 0
        temporary = _temporary_path(dest)
        try:
            with temporary.open("xb") as destination:
                for index in range(session.total_chunks):
                    _assert_upload_merge_owner(db, session, job_id=job_id, lease_token=lease_token)
                    chunk_path = self._chunk_path(session, index)
                    if not chunk_path.is_file() or chunk_path.is_symlink():
                        raise ValueError(f"分片 {index} 缺失，无法合并")
                    with chunk_path.open("rb") as source:
                        while block := source.read(UPLOAD_STREAM_BLOCK_BYTES):
                            destination.write(block)
                            md5.update(block)
                            sha256.update(block)
                            size_bytes += len(block)
                destination.flush()
                os.fsync(destination.fileno())
            if size_bytes == 0:
                raise ValueError("合并文件为空，上传数据无效")
            if md5.hexdigest() != session.file_md5.lower():
                raise ValueError("MD5 校验失败")
            os.replace(temporary, dest)
        finally:
            if temporary.exists():
                temporary.unlink()

        return {
            "storage_path": str(dest),
            "file_name": session.file_name,
            "size_bytes": size_bytes,
            "sha256": sha256.hexdigest(),
            "already_merged": False,
        }

    def _merged_file_path(self, session: UploadSession, job_id: str, lease_token: str) -> Path:
        _require_attempt_token(lease_token)
        hot_root = self.hot_dir.resolve()
        candidate = (
            hot_root
            / "upload_merges"
            / session.id
            / _attempt_id(job_id, lease_token)
            / _safe_upload_file_name(session.file_name)
        ).resolve()
        try:
            candidate.relative_to(hot_root)
        except ValueError as exc:
            raise ValueError("上传合并路径无效") from exc
        return candidate


def enqueue_upload_merge_job(
    db: Session,
    upload_id: str,
    file_md5: str,
    *,
    actor_id: int | None = None,
) -> JobRun:
    """Register a durable merge/raw-publication job without reading upload bytes."""
    session = db.get(UploadSession, upload_id)
    if session is None:
        raise ValueError("upload_id 不存在")
    if not _is_md5_digest(file_md5) or not _is_md5_digest(session.file_md5):
        raise ValueError("MD5 digest is invalid")
    if session.file_md5 != file_md5:
        raise ValueError("MD5 校验失败")
    uploaded = list(session.uploaded_chunks or [])
    if len(uploaded) != session.total_chunks:
        raise ValueError(f"分片不完整: {len(uploaded)}/{session.total_chunks}")
    if session.status not in {"uploading", "merge_pending", "merging", "merged"}:
        raise ValueError("上传会话状态不允许合并")

    task = db.get(Task, session.task_id) if session.task_id else None
    if _completed_upload_merge_result(session, task) is None:
        session.status = "merge_pending"
        if task is not None:
            task.metadata_json = _with_raw_ingest_state(
                task.metadata_json,
                RAW_STATE_RAW_COPY_PENDING,
                source_kind="browser_upload",
            )
        db.commit()
    job = create_or_get_job(
        db,
        kind=UPLOAD_MERGE_JOB_KIND,
        resource_type="upload_session",
        resource_id=session.id,
        idempotency_key=f"upload-merge:{session.id}:{session.file_md5}",
        queue=UPLOAD_MERGE_QUEUE,
        actor_id=actor_id,
        detail={"operation": "merge_raw_copy"},
    )
    if session.merge_job_id != job.id:
        session.merge_job_id = job.id
        db.commit()
    return job


def complete_upload_merge(
    db: Session,
    upload_id: str,
    *,
    job_id: str,
    lease_token: str,
    operator: str = "system",
) -> dict[str, object]:
    """Merge chunks and publish immutable raw data from a media worker."""
    session = db.get(UploadSession, upload_id)
    if session is None:
        raise ValueError("upload_id 不存在")
    task = db.get(Task, session.task_id) if session.task_id else None
    completed = _completed_upload_merge_result(session, task)
    if completed is not None:
        _assert_job_attempt(db, job_id, lease_token, upload_id=upload_id)
        _cleanup_upload_merge_staging(session, job_id=job_id, lease_token=lease_token)
        return completed
    begin_upload_merge_attempt(db, upload_id, job_id, lease_token)
    session = db.get(UploadSession, upload_id)
    task = db.get(Task, session.task_id) if session.task_id else None
    if task is not None:
        assert_collection_attribution_complete(db, task)
    workspace_id, project_id = _resolve_upload_namespace(db, session, task)
    merged = upload_service.merge_chunks(
        db,
        upload_id,
        session.file_md5,
        job_id=job_id,
        lease_token=lease_token,
    )
    local_file = Path(str(merged["storage_path"]))
    file_name = str(merged["file_name"])
    fmt = detect_upload_format(local_file)
    if task is not None and task.task_type == "ego" and fmt != "qrdf":
        raise ValueError("EGO task upload must be a QRDF ZIP archive")
    _assert_upload_merge_owner(db, session, job_id=job_id, lease_token=lease_token)
    attempt_upload_id = f"{session.id}/attempts/{_attempt_id(job_id, lease_token)}"
    target = plan_raw_upload(
        local_file,
        upload_id=attempt_upload_id,
        task_id=session.task_id,
        workspace_id=workspace_id,
        project_id=project_id,
    )
    _prepare_attempt_publication(
        db,
        upload_id,
        job_id=job_id,
        lease_token=lease_token,
        target=target,
    )
    storage_uri = ""
    try:
        storage_uri = publish_raw_upload(
            local_file,
            upload_id=attempt_upload_id,
            task_id=session.task_id,
            workspace_id=workspace_id,
            project_id=project_id,
            target=target,
        )
        _record_attempt_publication(
            db,
            upload_id,
            job_id=job_id,
            lease_token=lease_token,
            raw_uri=storage_uri,
        )
        result = {
            "upload_id": session.id,
            "task_id": session.task_id,
            "format": fmt,
            "raw_uri": storage_uri,
            "file_name": file_name,
        }
        _commit_upload_merge_winner(
            db,
            upload_id,
            job_id=job_id,
            lease_token=lease_token,
            operator=operator,
            result=result,
            sha256=str(merged.get("sha256") or ""),
        )
    except Exception:
        cleanup_upload_merge_attempt(
            db,
            upload_id,
            job_id=job_id,
            lease_token=lease_token,
        )
        raise

    cleanup_upload_merge_attempt(
        db,
        upload_id,
        job_id=job_id,
        lease_token=lease_token,
    )
    _cleanup_upload_chunks_if_winner(db, upload_id, lease_token=lease_token)
    redis_service.set_upload_status(upload_id, {"status": "merged", "storage_uri": storage_uri})
    redis_service.clear_upload_cache(upload_id)
    return result


def _completed_upload_merge_result(
    session: UploadSession, task: Task | None
) -> dict[str, object] | None:
    if session.status != "merged" or not session.raw_uri:
        return None
    result = dict(session.merge_result_json or {})
    if not result:
        return None
    return result


def mark_upload_merge_failed(
    db: Session,
    upload_id: str,
    *,
    job_id: str,
    lease_token: str,
) -> bool:
    """Persist a merge failure for polling without retaining the worker exception."""
    session = db.get(UploadSession, upload_id)
    if session is None:
        return False
    try:
        _assert_upload_merge_owner(db, session, job_id=job_id, lease_token=lease_token)
    except LeaseOwnershipLost:
        return False
    session.status = "merge_pending"
    task = db.get(Task, session.task_id) if session.task_id else None
    if task is not None:
        task.metadata_json = _with_raw_ingest_state(
            task.metadata_json,
            RAW_STATE_RAW_COPY_FAILED,
            source_kind="browser_upload",
        )
    db.commit()
    return True


def begin_upload_merge_attempt(
    db: Session,
    upload_id: str,
    job_id: str,
    lease_token: str,
) -> Path:
    _require_attempt_token(lease_token)
    job = _assert_job_attempt(db, job_id, lease_token, upload_id=upload_id, lock=True)
    now = database_now(db)
    session = db.scalar(
        select(UploadSession).where(UploadSession.id == upload_id).with_for_update()
    )
    if session is None:
        db.rollback()
        raise ValueError("upload_id 不存在")
    if session.status not in {"merge_pending", "merging"}:
        db.rollback()
        raise LeaseOwnershipLost("upload merge is no longer claimable")
    if session.merge_attempt_token and session.merge_attempt_token != lease_token:
        previous = db.scalar(
            select(UploadMergeAttempt)
            .where(UploadMergeAttempt.attempt_token == session.merge_attempt_token)
            .with_for_update()
        )
        if previous is not None and previous.status == "active":
            previous.status = "stale"
            previous.updated_at = now
    attempt = db.scalar(
        select(UploadMergeAttempt)
        .where(UploadMergeAttempt.attempt_token == lease_token)
        .with_for_update()
    )
    if attempt is None:
        attempt = UploadMergeAttempt(
            upload_id=upload_id,
            job_id=job.id,
            attempt_token=lease_token,
            status="active",
            created_at=now,
            updated_at=now,
        )
        db.add(attempt)
    elif attempt.upload_id != upload_id or attempt.job_id != job_id:
        db.rollback()
        raise LeaseOwnershipLost("merge attempt token scope mismatch")
    session.status = "merging"
    session.merge_job_id = job_id
    session.merge_attempt_token = lease_token
    db.commit()
    return _upload_attempt_dir_for(session.id, job_id, lease_token)


def cleanup_upload_merge_attempt(
    db: Session,
    upload_id: str,
    *,
    job_id: str,
    lease_token: str,
    reclaim_cleaning: bool = False,
    expected_cleanup_claim_token: str = "",
) -> bool:
    _require_attempt_token(lease_token)
    attempt = db.scalar(
        select(UploadMergeAttempt).where(
            UploadMergeAttempt.upload_id == upload_id,
            UploadMergeAttempt.job_id == job_id,
            UploadMergeAttempt.attempt_token == lease_token,
        )
    )
    if attempt is None:
        db.rollback()
        return False
    session = db.get(UploadSession, upload_id)
    if session is None:
        db.rollback()
        return False
    _cleanup_upload_merge_staging(session, job_id=job_id, lease_token=lease_token)
    if attempt.status == "winner":
        db.commit()
        return True
    cleanup_claim_token = uuid.uuid4().hex
    claimed_at = database_now(db)
    claimable = UploadMergeAttempt.status.in_(
        ("active", "stale", "publishing", "published", "cleanup_pending")
    )
    if reclaim_cleaning:
        previous_claim = (
            UploadMergeAttempt.cleanup_claim_token == expected_cleanup_claim_token
            if expected_cleanup_claim_token
            else or_(
                UploadMergeAttempt.cleanup_claim_token == "",
                UploadMergeAttempt.cleanup_claim_token.is_(None),
            )
        )
        claimable = or_(
            claimable,
            and_(UploadMergeAttempt.status == "cleaning", previous_claim),
        )
    claimed = db.execute(
        update(UploadMergeAttempt)
        .where(
            UploadMergeAttempt.id == attempt.id,
            claimable,
        )
        .values(
            status="cleaning",
            cleanup_claim_token=cleanup_claim_token,
            updated_at=claimed_at,
        ),
        execution_options={"synchronize_session": False},
    )
    if claimed.rowcount != 1:
        db.rollback()
        return False
    db.commit()
    if attempt.provider_prefix_uri:
        try:
            task = db.get(Task, session.task_id) if session.task_id else None
            workspace_id, project_id = _resolve_upload_namespace(db, session, task)
            cleanup_raw_upload_prefix(
                attempt.provider_prefix_uri,
                upload_id=upload_id,
                workspace_id=workspace_id,
                project_id=project_id,
                task_id=session.task_id,
                attempt_id=_attempt_id(job_id, lease_token),
            )
        except Exception as exc:
            failed_at = database_now(db)
            db.execute(
                update(UploadMergeAttempt)
                .where(
                    UploadMergeAttempt.id == attempt.id,
                    UploadMergeAttempt.status == "cleaning",
                    UploadMergeAttempt.cleanup_claim_token == cleanup_claim_token,
                )
                .values(
                    status="cleanup_pending",
                    cleanup_claim_token="",
                    cleanup_error=type(exc).__name__[:128],
                    updated_at=failed_at,
                )
            )
            db.commit()
            return False
    cleaned_at = database_now(db)
    db.execute(
        update(UploadMergeAttempt)
        .where(
            UploadMergeAttempt.id == attempt.id,
            UploadMergeAttempt.status == "cleaning",
            UploadMergeAttempt.cleanup_claim_token == cleanup_claim_token,
        )
        .values(
            status="cleaned",
            cleanup_claim_token="",
            raw_uri="",
            cleanup_error="",
            updated_at=cleaned_at,
        )
    )
    db.commit()
    return True


def cleanup_orphaned_upload_merge_attempts(
    db: Session,
    *,
    grace_seconds: int = 300,
) -> dict[str, list[int]]:
    """Bound orphan lifetime and retain cleanup_pending records on failures."""
    from datetime import timedelta

    now = database_now(db)
    cutoff = now - timedelta(seconds=max(0, grace_seconds))
    attempts = list(
        db.scalars(
            select(UploadMergeAttempt).where(
                UploadMergeAttempt.status.in_(
                    ("active", "stale", "publishing", "published", "cleaning", "cleanup_pending")
                ),
                UploadMergeAttempt.updated_at <= cutoff,
            )
        )
    )
    cleaned: list[int] = []
    pending: list[int] = []
    for attempt in attempts:
        job = db.get(JobRun, attempt.job_id)
        session = db.get(UploadSession, attempt.upload_id)
        task = db.get(Task, session.task_id) if session is not None and session.task_id else None
        referenced_winner = (
            session is not None
            and session.status == "merged"
            and (
                session.merge_attempt_token == attempt.attempt_token
                or bool(attempt.raw_uri and session.raw_uri == attempt.raw_uri)
            )
        ) or (
            task is not None
            and bool(attempt.raw_uri)
            and (
                task.storage_path == attempt.raw_uri
                or _metadata_references_value(task.metadata_json, attempt.raw_uri)
            )
        )
        if referenced_winner:
            continue
        still_owned = bool(
            job is not None
            and db.scalar(
                select(JobRun.id).where(
                    JobRun.id == job.id,
                    JobRun.status == "running",
                    JobRun.lease_token == attempt.attempt_token,
                    JobRun.lease_expires_at > func.current_timestamp(),
                )
            )
        )
        if still_owned:
            continue
        attempt_id = attempt.id
        if cleanup_upload_merge_attempt(
            db,
            attempt.upload_id,
            job_id=attempt.job_id,
            lease_token=attempt.attempt_token,
            reclaim_cleaning=attempt.status == "cleaning",
            expected_cleanup_claim_token=attempt.cleanup_claim_token or "",
        ):
            cleaned.append(attempt_id)
        else:
            pending.append(attempt_id)
    return {"cleaned": cleaned, "cleanup_pending": pending}


def _metadata_references_value(value: object, expected: str) -> bool:
    if isinstance(value, dict):
        return any(_metadata_references_value(item, expected) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_metadata_references_value(item, expected) for item in value)
    return isinstance(value, str) and value == expected


def _assert_job_attempt(
    db: Session,
    job_id: str,
    lease_token: str,
    *,
    upload_id: str,
    lock: bool = False,
) -> JobRun:
    _require_attempt_token(lease_token)
    query = select(JobRun).where(
        JobRun.id == job_id,
        JobRun.kind == UPLOAD_MERGE_JOB_KIND,
        JobRun.resource_type == "upload_session",
        JobRun.resource_id == upload_id,
        JobRun.status == "running",
        JobRun.lease_token == lease_token,
        JobRun.lease_expires_at > func.current_timestamp(),
    )
    if lock:
        query = query.with_for_update()
    job = db.scalar(query)
    if job is None:
        db.rollback()
        raise LeaseOwnershipLost("upload merge lease no longer owns this attempt")
    return job


def _assert_upload_merge_owner(
    db: Session,
    session: UploadSession,
    *,
    job_id: str,
    lease_token: str,
) -> UploadSession:
    _assert_job_attempt(db, job_id, lease_token, upload_id=session.id, lock=True)
    current = db.scalar(
        select(UploadSession).where(UploadSession.id == session.id).with_for_update()
    )
    if (
        current is None
        or current.status != "merging"
        or current.merge_job_id != job_id
        or current.merge_attempt_token != lease_token
    ):
        db.rollback()
        raise LeaseOwnershipLost("upload merge token is no longer current")
    db.commit()
    return current


def _record_attempt_publication(
    db: Session,
    upload_id: str,
    *,
    job_id: str,
    lease_token: str,
    raw_uri: str,
) -> None:
    attempt = db.scalar(
        select(UploadMergeAttempt)
        .where(
            UploadMergeAttempt.upload_id == upload_id,
            UploadMergeAttempt.job_id == job_id,
            UploadMergeAttempt.attempt_token == lease_token,
        )
        .with_for_update()
    )
    if attempt is None or attempt.status != "publishing" or attempt.provider_object_uri != raw_uri:
        db.rollback()
        raise LeaseOwnershipLost("upload merge attempt record is unavailable")
    db.execute(
        update(UploadMergeAttempt)
        .where(
            UploadMergeAttempt.id == attempt.id,
            UploadMergeAttempt.status == "publishing",
            UploadMergeAttempt.provider_object_uri == raw_uri,
        )
        .values(status="published", raw_uri=raw_uri, updated_at=database_now(db)),
        execution_options={"synchronize_session": False},
    )
    db.commit()


def _prepare_attempt_publication(
    db: Session,
    upload_id: str,
    *,
    job_id: str,
    lease_token: str,
    target: RawUploadTarget,
) -> None:
    _assert_job_attempt(db, job_id, lease_token, upload_id=upload_id, lock=True)
    attempt = db.scalar(
        select(UploadMergeAttempt)
        .where(
            UploadMergeAttempt.upload_id == upload_id,
            UploadMergeAttempt.job_id == job_id,
            UploadMergeAttempt.attempt_token == lease_token,
        )
        .with_for_update()
    )
    if attempt is None or attempt.status not in {"active", "publishing"}:
        db.rollback()
        raise LeaseOwnershipLost("upload merge attempt cannot enter publication")
    if attempt.provider_prefix_uri and (
        attempt.provider_prefix_uri != target.prefix_uri
        or attempt.provider_object_uri != target.object_uri
    ):
        db.rollback()
        raise LeaseOwnershipLost("upload merge provider target is immutable")
    db.execute(
        update(UploadMergeAttempt)
        .where(
            UploadMergeAttempt.id == attempt.id,
            UploadMergeAttempt.status.in_(("active", "publishing")),
        )
        .values(
            provider_prefix_uri=target.prefix_uri,
            provider_object_uri=target.object_uri,
            status="publishing",
            updated_at=database_now(db),
        ),
        execution_options={"synchronize_session": False},
    )
    db.commit()


def _commit_upload_merge_winner(
    db: Session,
    upload_id: str,
    *,
    job_id: str,
    lease_token: str,
    operator: str,
    result: dict[str, object],
    sha256: str,
) -> None:
    _assert_job_attempt(db, job_id, lease_token, upload_id=upload_id, lock=True)
    session = db.scalar(
        select(UploadSession).where(UploadSession.id == upload_id).with_for_update()
    )
    if (
        session is None
        or session.status != "merging"
        or session.merge_job_id != job_id
        or session.merge_attempt_token != lease_token
    ):
        db.rollback()
        raise LeaseOwnershipLost("another upload merge attempt won")
    attempt = db.scalar(
        select(UploadMergeAttempt)
        .where(
            UploadMergeAttempt.upload_id == upload_id,
            UploadMergeAttempt.job_id == job_id,
            UploadMergeAttempt.attempt_token == lease_token,
        )
        .with_for_update()
    )
    if attempt is None or attempt.status != "published" or attempt.raw_uri != result["raw_uri"]:
        db.rollback()
        raise LeaseOwnershipLost("upload merge publication is not current")

    task = (
        db.scalar(select(Task).where(Task.id == session.task_id).with_for_update())
        if session.task_id
        else None
    )
    if task is not None and task.task_type == "ego":
        raise ValueError("legacy EGO archive uploads are no longer supported")
    elif task is not None:
        collect_meta = dict((task.metadata_json or {}).get("collect") or {})
        collect_meta.update(
            {
                "file_name": result["file_name"],
                "merged_at": datetime.utcnow().isoformat() + "Z",
                "format": result["format"],
                "raw_uri": result["raw_uri"],
                "conversion": "mcap_to_qrdf_pending" if result["format"] == "mcap" else "none",
            }
        )
        metadata = dict(task.metadata_json or {})
        metadata["collect"] = collect_meta
        task.metadata_json = _with_raw_ingest_state(
            metadata,
            RAW_STATE_PROCESSING_READY,
            source_kind="browser_upload",
            sha256=sha256,
        )
        task.storage_path = str(result["raw_uri"])
        if task.status == TaskStatus.PENDING_COLLECT.value:
            transit_task(
                db,
                task,
                "collect_done",
                operator,
                "上传完成，待接入审核",
                commit=False,
            )

    session.raw_uri = str(result["raw_uri"])
    session.merge_result_json = dict(result)
    session.status = "merged"
    session.resume_key = None
    db.execute(
        update(UploadMergeAttempt)
        .where(
            UploadMergeAttempt.id == attempt.id,
            UploadMergeAttempt.status == "published",
        )
        .values(status="winner", updated_at=database_now(db)),
        execution_options={"synchronize_session": False},
    )
    db.commit()


def _resolve_upload_namespace(
    db: Session,
    session: UploadSession,
    task: Task | None,
) -> tuple[int, int | None]:
    if session.workspace_id is None:
        raise ValueError("upload workspace is required before raw publication")
    if task is None:
        if session.task_id is not None:
            raise ValueError("upload task does not exist")
        return session.workspace_id, None
    task_workspace_id = resolve_task_workspace_id(db, task)
    if task_workspace_id is None or task_workspace_id != session.workspace_id:
        raise ValueError("upload task workspace mismatch")
    project = db.get(Project, task.project_id)
    if project is None or project.workspace_id != task_workspace_id:
        raise ValueError("upload task project workspace mismatch")
    return task_workspace_id, project.id


def _cleanup_upload_chunks_if_winner(
    db: Session,
    upload_id: str,
    *,
    lease_token: str,
) -> bool:
    session = db.get(UploadSession, upload_id)
    if session is None or session.status != "merged" or session.merge_attempt_token != lease_token:
        return False
    chunk_folder = upload_service._chunk_path(session, 0).parent
    if chunk_folder.exists():
        for path in chunk_folder.iterdir():
            if path.is_symlink() or not path.is_file():
                raise ValueError("upload chunk cleanup encountered an invalid entry")
            path.unlink()
        chunk_folder.rmdir()
    return True


def _cleanup_upload_merge_staging(
    session: UploadSession,
    *,
    job_id: str,
    lease_token: str,
) -> bool:
    stage = _upload_attempt_dir_for(session.id, job_id, lease_token)
    if stage.is_symlink():
        raise ValueError("upload merge staging cleanup encountered a symlink")
    if stage.exists():
        shutil.rmtree(stage)
    return True


def _upload_attempt_dir_for(upload_id: str, job_id: str, lease_token: str) -> Path:
    attempt_root = (upload_service.hot_dir / "upload_merges").resolve()
    candidate = (attempt_root / upload_id / _attempt_id(job_id, lease_token)).resolve()
    try:
        candidate.relative_to(attempt_root)
    except ValueError as exc:
        raise ValueError("upload merge attempt path is invalid") from exc
    return candidate


def _attempt_id(job_id: str, lease_token: str) -> str:
    _require_attempt_token(lease_token)
    return hashlib.sha256(f"{job_id}:{lease_token}".encode()).hexdigest()


def _require_attempt_token(lease_token: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{32,64}", lease_token or ""):
        raise LeaseOwnershipLost("upload merge attempt token is invalid")


def _upload_resume_key(
    *,
    file_md5: str,
    total_chunks: int,
    file_name: str,
    task_id: int | None,
    owner_user_id: int,
    workspace_id: int | None,
) -> str:
    identity = "\x1f".join(
        (
            file_md5,
            str(total_chunks),
            file_name,
            str(task_id or 0),
            str(owner_user_id),
            str(workspace_id or 0),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _with_raw_ingest_state(metadata: object, state: str, **extra: object) -> dict[str, object]:
    payload = dict(metadata) if isinstance(metadata, dict) else {}
    raw_ingest = dict(payload.get("raw_ingest") or {})
    raw_ingest.update({"state": state, **extra})
    payload["raw_ingest"] = raw_ingest
    return payload


def _safe_upload_file_name(file_name: str) -> str:
    normalized = str(file_name or "").strip().replace("\\", "/")
    name = Path(normalized).name
    if not name or name in {".", ".."} or name != normalized or len(name) > 256:
        raise ValueError("上传文件名无效")
    return name


def _temporary_path(destination: Path) -> Path:
    return destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(UPLOAD_STREAM_BLOCK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _is_md5_digest(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{32}", value or ""))


upload_service = UploadService()
