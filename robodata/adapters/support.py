"""Shared, adapter-agnostic helpers.

These helpers were previously private to ``robodata.importing``. They are
stable enough to be reused by every adapter; nothing here knows about a
particular input format.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

from ..config import HDF5_EPISODES, PROJECT_ROOT
from ..source import file_sha256

PROFILE = {
    "profile_version": "0.6.0",
    "hdf5_episodes": list(HDF5_EPISODES),
    "state_fields": ["obs/robot0_eef_pos", "obs/robot0_eef_quat", "obs/robot0_gripper_qpos"],
    "action": "actions",
    "image": "obs/agentview_image",
    "fps": 20,
    "timestamp": "derived_frame_index/fps",
}

HDF5_RULES = ("required_fields", "row_count", "vector_shape", "numeric_finite", "frame_index",
              "timestamp_monotonic", "timestamp_interval", "image_shape", "image_frame_count", "image_read")


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    from ..runtime import _atomic_json
    _atomic_json(Path(path), value)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def file_records(root: Path, names) -> list[dict]:
    """Describe the named files, including missing ones, without trusting them."""
    records = []
    for name in sorted(set(names)):
        path = Path(root) / name
        records.append({"path": name, "exists": path.is_file(),
                        **({"size": path.stat().st_size, "sha256": file_sha256(path)}
                           if path.is_file() else {})})
    return records


def run_conversion_worker(script: Path, raw: Path, output: Path, *, label: str | None = None,
                          failure_message: str) -> None:
    """Run an isolated preview worker and surface its diagnostic verbatim."""
    from ..conversion import CONVERSION_PYTHON
    command = [str(CONVERSION_PYTHON), str(script), "--input", str(raw), "--output", str(output)]
    if label:
        command.extend(["--test-mode", label])
    environment = os.environ.copy()
    environment.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                       HF_HUB_OFFLINE="1", HF_DATASETS_OFFLINE="1")
    process = subprocess.run(command, env=environment, capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
    output.parent.mkdir(parents=True, exist_ok=True)
    (output.parent / (output.name + ".stdout.log")).write_text(process.stdout, encoding="utf-8")
    (output.parent / (output.name + ".stderr.log")).write_text(process.stderr, encoding="utf-8")
    if process.returncode:
        diagnostic = output / "diagnostic.json"
        details = read_json(diagnostic) if diagnostic.is_file() else {}
        error = ValueError(details.get("message_zh") or details.get("message") or failure_message)
        error.details = details
        raise error


def preview_worker(raw: Path, output: Path, label: str | None = None) -> None:
    run_conversion_worker(PROJECT_ROOT / "tools/conversion/preview.py", raw, output, label=label,
                          failure_message="原始 HDF5 预览解析失败，请查看独立解析日志")


def matching_preview_worker(raw: Path, output: Path) -> None:
    run_conversion_worker(PROJECT_ROOT / "tools/conversion/matching_preview.py", raw, output,
                          failure_message="Matching Cable 帧数据预览解析失败")
