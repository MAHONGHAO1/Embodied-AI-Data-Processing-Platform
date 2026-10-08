"""MCAP <-> QRDF <-> LeRobot conversion service (wraps validate_mcap_pipeline core logic)."""

from __future__ import annotations

import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from qrdf.converters.validate_lerobot import validate_lerobot_dataset

from data.config import settings
from data.integrations.qrdf.lerobot_export import convert_dataset_extended
from data.integrations.qrdf.preview_export import infer_dataset_fps
from data.integrations.qrdf.rosbag_importer import RosBagMcapImporter, _episode_id_from_mcap
from data.integrations.qrdf.service import get_dataset_summary, validate_dataset
from data.services.bag_ingest import resolve_bag_folder
from data.utils.formatting import format_api_datetime, normalize_api_fps


def _storage_relative(path: Path) -> str:
    root = Path(settings.storage_root).resolve()
    resolved = path.resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return resolved.as_posix()


def _default_image_workers() -> int:
    return max(8, min((os.cpu_count() or 4), 16))


def _hot_output_dir(prefix: str, name: str) -> Path:
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^\w.\-]+", "_", name)[:80]
    out = Path(settings.storage_root) / "hot" / f"{prefix}_{safe}_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _resolve_convert_output_dir(
    output_dir: str | Path | None,
    *,
    prefix: str,
    name: str,
) -> Path:
    """Resolve conversion output directory: uses hot/ if unspecified; must reside within storage_root sandbox if specified."""
    if not output_dir:
        return _hot_output_dir(prefix, name)

    from data.utils.storage_paths import is_under_storage_root, storage_root_path

    root = storage_root_path()
    raw = Path(str(output_dir).strip())
    candidate = raw if raw.is_absolute() else (root / raw)
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise ValueError(f"无效 output_dir: {output_dir}") from exc
    if not is_under_storage_root(resolved, root=root):
        raise ValueError(f"output_dir 必须位于 storage_root 内: {output_dir}")
    return resolved


def resolve_mcap_path(
    *,
    mcap_path: str | None = None,
    folder_name: str | None = None,
    file_name: str | None = None,
) -> Path:
    """Resolve an MCAP only inside the configured storage sandbox."""
    from data.utils.storage_paths import is_under_storage_root, storage_root_path

    root = storage_root_path()
    if mcap_path:
        path = Path(mcap_path).expanduser().resolve()
        if not is_under_storage_root(path, root=root):
            raise ValueError("MCAP source must be inside storage_root")
        if not path.is_file():
            raise FileNotFoundError(f"MCAP 文件不存在: {path}")
        return path

    if not folder_name:
        raise ValueError("请提供 mcap_path，或 folder_name + file_name")

    folder = resolve_bag_folder(folder_name)
    if file_name:
        path = folder / file_name
    else:
        mcaps = sorted(folder.glob("*.mcap"))
        if not mcaps:
            raise FileNotFoundError(f"目录中未找到 MCAP: {folder}")
        path = mcaps[0]

    if not path.is_file():
        raise FileNotFoundError(f"MCAP 文件不存在: {path}")
    resolved = path.resolve()
    if not is_under_storage_root(resolved, root=root):
        raise ValueError("MCAP source must be inside storage_root")
    return resolved


def convert_mcap_to_qrdf(
    *,
    mcap_path: str | Path | None = None,
    folder_name: str | None = None,
    file_name: str | None = None,
    output_dir: str | Path | None = None,
    episode_id: str | None = None,
    task_name: str | None = None,
    dataset_name: str | None = None,
    image_workers: int | None = None,
    generate_preview: bool = False,
    reader_backend: str = "auto",
) -> dict[str, Any]:
    """MCAP -> QRDF single file conversion (Pipeline 2: Data standardization preprocessing)."""
    mcap = resolve_mcap_path(
        mcap_path=str(mcap_path) if mcap_path else None,
        folder_name=folder_name,
        file_name=file_name,
    )
    qrdf_dir = _resolve_convert_output_dir(output_dir, prefix="convert_qrdf", name=mcap.stem)
    qrdf_dir.mkdir(parents=True, exist_ok=True)

    workers = image_workers or _default_image_workers()
    ep_id = episode_id or _episode_id_from_mcap(mcap, 1)
    bag_folder = folder_name or mcap.parent.name

    importer = RosBagMcapImporter(
        skip_preview=not generate_preview,
        image_workers=workers,
        reader_backend=reader_backend,  # type: ignore[arg-type]
    )

    started = time.perf_counter()
    try:
        recorder = importer.import_episode(
            mcap,
            qrdf_dir,
            episode_id=ep_id,
            yaml_path=mcap.with_suffix(".yaml"),
            task_name=task_name or mcap.stem,
            dataset_name=dataset_name or f"qrdf_{mcap.stem}",
            bag_folder=bag_folder,
        )
    except Exception as exc:
        if reader_backend == "auto" and "ros2" in str(exc).lower():
            importer = RosBagMcapImporter(
                skip_preview=not generate_preview,
                image_workers=workers,
                reader_backend="mcap_ros2",
            )
            recorder = importer.import_episode(
                mcap,
                qrdf_dir,
                episode_id=ep_id,
                yaml_path=mcap.with_suffix(".yaml"),
                task_name=task_name or mcap.stem,
                dataset_name=dataset_name or f"qrdf_{mcap.stem}",
                bag_folder=bag_folder,
            )
        else:
            raise
    elapsed = time.perf_counter() - started
    import_timing = importer.last_import_timing or {}

    quality = validate_dataset(str(qrdf_dir))
    summary = get_dataset_summary(str(qrdf_dir))
    size_bytes = mcap.stat().st_size

    return {
        "conversion_type": "mcap_to_qrdf",
        "mcap_path": str(mcap),
        "size_bytes": size_bytes,
        "size_gb": round(size_bytes / (1024**3), 3),
        "qrdf_path": str(qrdf_dir),
        "storage_path": _storage_relative(qrdf_dir),
        "episode_id": recorder.episode_id,
        "episode_path": str(recorder.episode_path) if recorder.episode_path else None,
        "image_workers": workers,
        "timing": {
            "duration_sec": round(elapsed, 3),
            "state_pass_sec": import_timing.get("state_pass_sec"),
            "camera_pass_sec": import_timing.get("camera_pass_sec"),
            "throughput_gbps": round(size_bytes / (1024**3) / elapsed, 4) if elapsed > 0 else 0,
        },
        "quality": quality,
        "summary": summary,
        "finished_at": format_api_datetime(datetime.utcnow()),
    }


def convert_qrdf_to_lerobot(
    *,
    storage_path: str | Path,
    output_dir: str | Path | None = None,
    fps: float | None = None,
    lerobot_version: str = "v3.0",
    image_size: tuple[int, int] | None = None,
    export_template: str = "generic",
) -> dict[str, Any]:
    """QRDF -> LeRobot conversion (Pipeline 5: Training data export)."""
    from data.integrations.qrdf.service import resolve_dataset_path

    qrdf_dir = resolve_dataset_path(str(storage_path))
    if not qrdf_dir or not qrdf_dir.is_dir():
        raise FileNotFoundError(f"QRDF 数据集不存在: {storage_path}")

    lerobot_dir = _resolve_convert_output_dir(
        output_dir, prefix="convert_lerobot", name=qrdf_dir.name
    )
    if lerobot_dir.exists():
        import shutil

        shutil.rmtree(lerobot_dir)
    lerobot_dir.mkdir(parents=True, exist_ok=True)

    dataset_fps = fps if fps and fps > 0 else infer_dataset_fps(qrdf_dir)

    started = time.perf_counter()
    convert_dataset_extended(
        qrdf_dir,
        lerobot_dir,
        fps=dataset_fps,
        image_size=image_size,
        lerobot_version=lerobot_version,
        export_template=export_template,
    )
    elapsed = time.perf_counter() - started

    lerobot_report = validate_lerobot_dataset(lerobot_dir)
    info_path = lerobot_dir / "meta" / "info.json"
    features: list[str] = []
    total_frames = None
    if info_path.is_file():
        import json

        info = json.loads(info_path.read_text(encoding="utf-8"))
        features = list((info.get("features") or {}).keys())
        total_frames = info.get("total_frames")

    video_keys = [k for k in features if k.startswith("observation.images.")]

    return {
        "conversion_type": "qrdf_to_lerobot",
        "qrdf_path": str(qrdf_dir),
        "lerobot_path": str(lerobot_dir),
        "storage_path": _storage_relative(lerobot_dir),
        "lerobot_fps": normalize_api_fps(dataset_fps),
        "lerobot_version": lerobot_version,
        "export_template": export_template,
        "video_keys": video_keys,
        "total_frames": total_frames,
        "timing": {
            "duration_sec": round(elapsed, 3),
        },
        "quality": {
            "ok": lerobot_report.ok,
            "error_count": lerobot_report.error_count,
            "warning_count": lerobot_report.warning_count,
        },
        "finished_at": format_api_datetime(datetime.utcnow()),
    }


def new_conversion_job_id() -> str:
    return uuid.uuid4().hex


def resolve_qrdf_storage_path(
    *,
    storage_path: str | None = None,
    qrdf_id: int | None = None,
    task_id: int | None = None,
    db: Any | None = None,
) -> str:
    """Resolve QRDF dataset storage_path (supports direct upload / qrdf_id / task_id)."""
    from data.integrations.qrdf.service import resolve_dataset_path

    if storage_path:
        resolved = resolve_dataset_path(storage_path)
        if resolved and resolved.is_dir():
            return str(resolved)
        raise FileNotFoundError(f"QRDF 数据集不存在: {storage_path}")

    if db is None:
        raise ValueError("解析 qrdf_id / task_id 需要数据库会话")

    if qrdf_id is not None:
        from data.database import QrdfData

        item = db.get(QrdfData, qrdf_id)
        if not item or not item.storage_path:
            raise FileNotFoundError(f"QRDF 记录不存在: {qrdf_id}")
        resolved = resolve_dataset_path(item.storage_path)
        if not resolved:
            raise FileNotFoundError(f"QRDF 存储路径无效: {item.storage_path}")
        return str(resolved)

    if task_id is not None:
        from data.database import Task

        task = db.get(Task, task_id)
        if not task or not task.storage_path:
            raise FileNotFoundError(f"任务无 QRDF 存储路径: {task_id}")
        resolved = resolve_dataset_path(task.storage_path)
        if not resolved:
            raise FileNotFoundError(f"任务 QRDF 路径无效: {task.storage_path}")
        return str(resolved)

    raise ValueError("请提供 storage_path、qrdf_id 或 task_id 之一")
