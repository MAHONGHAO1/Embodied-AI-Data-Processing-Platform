"""Multi-process bag MCAP import worker (for ProcessPoolExecutor invocations)."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_VENDOR_QRDF = _BACKEND_ROOT / "vendor" / "qrdf"
for p in (str(_VENDOR_QRDF), str(_BACKEND_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


def import_mcap_worker(payload: dict[str, Any]) -> dict[str, Any]:
    """Import single MCAP (independent process)."""
    from data.integrations.qrdf.rosbag_importer import RosBagMcapImporter, _episode_id_from_mcap

    mcap_path = Path(payload["mcap_path"])
    bag_root = Path(payload["bag_root"]).resolve()
    if mcap_path.is_symlink():
        raise ValueError("invalid bag source file")
    mcap_path = mcap_path.resolve()
    try:
        mcap_path.relative_to(bag_root)
    except ValueError as exc:
        raise ValueError("invalid bag source file") from exc
    if not mcap_path.is_file():
        raise ValueError("invalid bag source file")
    output_dir = Path(payload["output_dir"])
    idx = int(payload["index"])
    folder_name = payload["folder_name"]
    task_name = payload.get("task_name") or folder_name
    dataset_name = payload.get("dataset_name") or f"bag_{folder_name}"
    image_workers = int(payload.get("image_workers") or 8)
    skip_preview = bool(payload.get("skip_preview", True))

    yaml_path = mcap_path.with_suffix(".yaml")
    if yaml_path.is_symlink():
        raise ValueError("invalid bag source metadata")
    if yaml_path.exists():
        yaml_path = yaml_path.resolve()
        try:
            yaml_path.relative_to(bag_root)
        except ValueError as exc:
            raise ValueError("invalid bag source metadata") from exc
    episode_id = _episode_id_from_mcap(mcap_path, idx)
    started = time.perf_counter()

    try:
        importer = RosBagMcapImporter(
            image_workers=image_workers,
            skip_preview=skip_preview,
        )
        recorder = importer.import_episode(
            mcap_path,
            output_dir,
            episode_id=episode_id,
            yaml_path=yaml_path if yaml_path.is_file() else None,
            task_name=task_name,
            dataset_name=dataset_name,
            bag_folder=folder_name,
        )
        elapsed = time.perf_counter() - started
        return {
            "index": idx,
            "file_name": mcap_path.name,
            "size_bytes": mcap_path.stat().st_size,
            "episode_id": recorder.episode_id,
            "episode_path": str(recorder.episode_path),
            "duration_sec": round(elapsed, 3),
            "ok": True,
        }
    except Exception as exc:
        elapsed = time.perf_counter() - started
        return {
            "index": idx,
            "file_name": mcap_path.name,
            "size_bytes": mcap_path.stat().st_size if mcap_path.is_file() else 0,
            "duration_sec": round(elapsed, 3),
            "error": str(exc),
            "ok": False,
        }
