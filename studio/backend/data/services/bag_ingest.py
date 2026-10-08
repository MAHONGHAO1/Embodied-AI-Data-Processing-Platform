"""Local rosbag2 MCAP directory ingestion service (multi-process parallelism + multi-threaded image acceleration)."""

from __future__ import annotations

import logging
import os
import shutil
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from data.config import settings
from data.database import Project, Task, TaskStatus
from data.integrations.qrdf.rosbag_importer import RosBagMcapImporter, scan_bag_folder
from data.integrations.qrdf.service import get_dataset_summary, validate_dataset
from data.services.bag_import_workers import import_mcap_worker
from data.services.cloud_storage import publish_raw_upload
from data.services.state_machine import transit_task
from data.utils.formatting import format_api_datetime

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)


def resolve_bag_root() -> Path:
    return Path(settings.bag_data_root).expanduser().resolve()


def resolve_bag_folder(folder_name: str) -> Path:
    """Resolve bag subdirectory; reject absolute paths and .. traversal."""
    name = (folder_name or "").strip().replace("\\", "/")
    if not name or name.startswith("/") or name.startswith("~") or ".." in Path(name).parts:
        raise ValueError("invalid bag directory name")
    root = resolve_bag_root()
    unresolved = root / name
    current = root
    for part in Path(name).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("invalid bag directory name")
    folder = unresolved.resolve()
    if root not in folder.parents and folder != root:
        raise ValueError("bag directory is outside the allowed root")
    if not folder.is_dir():
        raise FileNotFoundError("bag directory does not exist")
    return folder


def list_bag_folders() -> list[dict[str, Any]]:
    root = resolve_bag_root()
    if not root.is_dir():
        return []
    folders: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        if child.is_symlink() or not child.is_dir():
            continue
        mcap_count = len(_safe_mcap_files(child))
        if mcap_count == 0:
            continue
        folders.append(
            {
                "folder_name": child.name,
                # Do not expose host absolute paths to clients
                "mcap_count": mcap_count,
            }
        )
    return folders


def _dataset_output_dir(folder_name: str) -> Path:
    del folder_name
    return Path(settings.storage_root) / "hot" / "bag_imports" / uuid.uuid4().hex


def _safe_mcap_files(folder: Path) -> list[Path]:
    root = folder.resolve()
    safe: list[Path] = []
    for candidate in sorted(folder.glob("*.mcap")):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        safe.append(resolved)
    return safe


def _default_parallel_workers(file_count: int) -> int:
    cpu = os.cpu_count() or 4
    # ~2-4GB per process, control concurrency to avoid OOM
    by_mem = max(1, min(file_count, 6))
    by_cpu = max(1, min(file_count, max(2, cpu // 4)))
    return min(by_mem, by_cpu, file_count)


def _generate_previews_parallel(episode_paths: list[Path], preview_workers: int) -> None:
    from data.integrations.qrdf.preview_export import export_episode_preview

    def _one(path: Path) -> None:
        try:
            export_episode_preview(path)
        except Exception as exc:
            logger.warning("bag preview generation failed error_type=%s", type(exc).__name__)

    if not episode_paths:
        return
    workers = max(1, min(preview_workers, len(episode_paths)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_one, episode_paths))


class BagIngestService:
    def __init__(self) -> None:
        self.importer = RosBagMcapImporter()

    def scan(self, folder_name: str) -> dict[str, Any]:
        folder = resolve_bag_folder(folder_name)
        safe_names = {path.name for path in _safe_mcap_files(folder)}
        scanned = [
            item
            for item in scan_bag_folder(folder)
            if str(item.get("file_name") or "") in safe_names
        ]
        files = [
            {
                "file_name": item.get("file_name") or "",
                "size_bytes": item.get("size_bytes") or 0,
                "duration_ns": item.get("duration_ns"),
                "message_count": item.get("message_count"),
                "has_yaml": bool(item.get("yaml_path")),
            }
            for item in scanned
        ]
        total_size = sum(f.get("size_bytes") or 0 for f in files)
        return {
            "folder_name": folder_name,
            "mcap_count": len(files),
            "total_size_bytes": total_size,
            "files": files,
        }

    def import_folder(self, db: Session, **kwargs) -> dict[str, Any]:
        output_dir = _dataset_output_dir(str(kwargs.get("folder_name") or ""))
        try:
            return self._import_folder_once(db, output_dir=output_dir, **kwargs)
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)

    def _import_folder_once(
        self,
        db: Session,
        *,
        output_dir: Path,
        folder_name: str,
        project_id: int,
        task_id: int | None = None,
        task_name: str | None = None,
        episode_limit: int | None = None,
        operator: str = "system",
        actor_id: int | None = None,
        auto_preprocess: bool = True,
        parallel_workers: int | None = None,
        image_workers: int | None = None,
        skip_preview: bool = False,
        generate_preview: bool = True,
    ) -> dict[str, Any]:
        folder = resolve_bag_folder(folder_name)
        mcap_files = _safe_mcap_files(folder)
        if not mcap_files:
            raise ValueError("bag directory contains no MCAP files")

        if episode_limit is not None and episode_limit > 0:
            mcap_files = mcap_files[:episode_limit]

        output_dir.mkdir(parents=True, exist_ok=True)

        dataset_name = task_name or f"bag_{folder_name}"
        workers = parallel_workers or _default_parallel_workers(len(mcap_files))
        img_workers = image_workers or max(4, min(16, (os.cpu_count() or 4)))
        timing_stats: list[dict[str, Any]] = []
        episodes: list[dict[str, Any]] = []
        errors: list[str] = []
        batch_started = time.perf_counter()

        payloads = [
            {
                "index": idx,
                "mcap_path": str(mcap_path),
                "bag_root": str(folder),
                "output_dir": str(output_dir),
                "folder_name": folder_name,
                "task_name": task_name or folder_name,
                "dataset_name": dataset_name,
                "image_workers": img_workers,
                "skip_preview": True,
            }
            for idx, mcap_path in enumerate(mcap_files, start=1)
        ]

        if workers <= 1:
            for payload in payloads:
                result = _run_import_worker_safely(payload)
                timing_stats.append(_public_timing_item(result))
                if result.get("ok"):
                    episodes.append(
                        {
                            "episode_id": result["episode_id"],
                            "source_mcap": payload["mcap_path"],
                            "episode_path": result["episode_path"],
                            "duration_sec": result["duration_sec"],
                        }
                    )
                else:
                    errors.append(f"{result['file_name']}: import failed")
        else:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(import_mcap_worker, p): p for p in payloads}
                for fut in as_completed(futures):
                    result = _consume_import_future_safely(fut, futures[fut])
                    timing_stats.append(_public_timing_item(result))
                    if result.get("ok"):
                        episodes.append(
                            {
                                "episode_id": result["episode_id"],
                                "source_mcap": futures[fut]["mcap_path"],
                                "episode_path": result["episode_path"],
                                "duration_sec": result["duration_sec"],
                            }
                        )
                    else:
                        errors.append(f"{result['file_name']}: import failed")

        timing_stats.sort(key=lambda x: x.get("index", 0))

        if not episodes:
            shutil.rmtree(output_dir, ignore_errors=True)
            raise ValueError("bag import failed for all files")

        preview_started = time.perf_counter()
        if generate_preview:
            episode_paths = [Path(e["episode_path"]) for e in episodes if e.get("episode_path")]
            _generate_previews_parallel(episode_paths, preview_workers=min(4, workers))
        preview_elapsed = time.perf_counter() - preview_started

        total_elapsed = time.perf_counter() - batch_started
        success_times = [s["duration_sec"] for s in timing_stats if "error" not in s]
        timing_summary = {
            "total_files": len(mcap_files),
            "success_count": len(success_times),
            "failed_count": len(mcap_files) - len(success_times),
            "total_duration_sec": round(total_elapsed, 3),
            "average_duration_sec": round(sum(success_times) / len(success_times), 3)
            if success_times
            else 0,
            "parallel_workers": workers,
            "image_workers": img_workers,
            "preview_generation_sec": round(preview_elapsed, 3) if generate_preview else 0,
            "per_file": timing_stats,
        }

        redacted_roots = (folder, output_dir)
        quality = _public_quality(
            validate_dataset(str(output_dir)),
            redacted_roots=redacted_roots,
        )
        summary = _public_summary(
            get_dataset_summary(str(output_dir)),
            redacted_roots=redacted_roots,
        )
        public_episodes = [
            {
                "episode_id": episode["episode_id"],
                "source_file": Path(episode["source_mcap"]).name,
                "duration_sec": episode["duration_sec"],
            }
            for episode in episodes
        ]

        if task_id:
            task = db.get(Task, task_id)
            if not task:
                raise ValueError(f"task_id 不存在: {task_id}")
            if task.ingested_by_user_id is None:
                task.ingested_by_user_id = actor_id
        else:
            project = db.get(Project, project_id)
            if not project:
                raise ValueError(f"project_id 不存在: {project_id}")
            task = Task(
                project_id=project_id,
                workspace_id=project.workspace_id,
                name=task_name or f"Bag接入 {folder_name}",
                task_type="collect",
                data_source="ros2_mcap_bag",
                status=TaskStatus.PENDING_COLLECT.value,
                ingested_by_user_id=actor_id,
                created_by_user_id=actor_id,
                metadata_json={"bag": {"folder_name": folder_name}},
            )
            db.add(task)
            db.commit()
            db.refresh(task)

        storage_uri = publish_raw_upload(
            output_dir,
            upload_id=f"bag-{uuid.uuid4().hex}",
            task_id=task.id,
            workspace_id=task.workspace_id,
            project_id=task.project_id,
        )
        task.storage_path = storage_uri
        task.data_source = "ros2_mcap_bag"
        task.metadata_json = {
            **(task.metadata_json or {}),
            "bag": {
                "folder_name": folder_name,
                "imported_at": format_api_datetime(datetime.utcnow()),
                "episodes": public_episodes,
                "errors": errors,
                "timing": timing_summary,
                "quality": quality,
                "summary": summary,
            },
        }
        db.commit()

        task = transit_task(db, task, "collect_done", operator, "Bag导入完成，待接入审核")

        return {
            "task_id": task.id,
            "status": task.status,
            "storage_uri": storage_uri,
            "dataset_summary": summary,
            "quality": quality,
            "episodes": public_episodes,
            "errors": errors,
            "timing": timing_summary,
        }


bag_ingest_service = BagIngestService()


def _failed_worker_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": payload.get("index"),
        "file_name": Path(str(payload.get("mcap_path") or "")).name,
        "size_bytes": 0,
        "duration_sec": 0,
        "error": "import failed",
        "ok": False,
    }


def _run_import_worker_safely(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return import_mcap_worker(payload)
    except Exception as exc:
        logger.warning("bag import worker failed error_type=%s", type(exc).__name__)
        return _failed_worker_result(payload)


def _consume_import_future_safely(future, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return future.result()
    except Exception as exc:
        logger.warning("bag import worker failed error_type=%s", type(exc).__name__)
        return _failed_worker_result(payload)


def _public_timing_item(result: dict[str, Any]) -> dict[str, Any]:
    item = {
        "index": result.get("index"),
        "file_name": result.get("file_name") or "",
        "size_bytes": result.get("size_bytes") or 0,
        "duration_sec": result.get("duration_sec") or 0,
    }
    if result.get("error"):
        item["error"] = "import failed"
    return item


_SENSITIVE_DIAGNOSTIC_KEY_PARTS = (
    "credential",
    "path",
    "presign",
    "secret",
    "signature",
    "token",
    "url",
)
_SENSITIVE_DIAGNOSTIC_VALUE_MARKERS = (
    "access_key",
    "authorization:",
    "credential",
    "secret=",
    "signature=",
    "token=",
)


def _sanitize_diagnostic_value(
    value: Any,
    *,
    redacted_roots: tuple[Path, ...],
) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize_diagnostic_value(item, redacted_roots=redacted_roots)
            for key, item in value.items()
            if not any(part in str(key).lower() for part in _SENSITIVE_DIAGNOSTIC_KEY_PARTS)
        }
    if isinstance(value, list):
        return [_sanitize_diagnostic_value(item, redacted_roots=redacted_roots) for item in value]
    if isinstance(value, tuple):
        return tuple(
            _sanitize_diagnostic_value(item, redacted_roots=redacted_roots) for item in value
        )
    if isinstance(value, str):
        if any(marker in value.lower() for marker in _SENSITIVE_DIAGNOSTIC_VALUE_MARKERS):
            return "[redacted]"
        public = value
        for root in redacted_roots:
            public = public.replace(str(root), "[redacted]")
        if "://" in public and "?" in public:
            public = public.split("?", 1)[0]
        return public
    return value


def _public_summary(
    summary: dict[str, Any],
    *,
    redacted_roots: tuple[Path, ...] = (),
) -> dict[str, Any]:
    return _sanitize_diagnostic_value(summary, redacted_roots=redacted_roots)


def _public_quality(
    quality: dict[str, Any],
    *,
    redacted_roots: tuple[Path, ...] = (),
) -> dict[str, Any]:
    return _sanitize_diagnostic_value(quality, redacted_roots=redacted_roots)
