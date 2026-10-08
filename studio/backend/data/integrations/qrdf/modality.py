"""QRDF sensor modality.json generation (sensor only, excludes annotation semantics)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from data.config import settings


def _storage_root() -> Path:
    return Path(settings.storage_root).resolve()


def resolve_qrdf_root(storage_path: str | None) -> Path | None:
    if not storage_path:
        return None
    root = _storage_root() / storage_path
    return root if root.is_dir() else None


def _find_episode_dir(qrdf_root: Path) -> Path | None:
    episodes_dir = qrdf_root / "episodes"
    if not episodes_dir.is_dir():
        return None
    children = sorted(p for p in episodes_dir.iterdir() if p.is_dir())
    return children[0] if children else None


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def build_modality_json(episode_dir: Path, fps: float = 30.0) -> dict[str, Any]:
    metadata = _load_json(episode_dir / "metadata.json")
    sensors = metadata.get("sensors") or {}
    cameras = sensors.get("cameras") or []
    video: dict[str, Any] = {}
    preview_streams: list[dict[str, str]] = []
    role_map = {"front": "main", "left_wrist": "aux", "right_wrist": "aux"}
    label_map = {"front": "头部", "left_wrist": "左腕", "right_wrist": "右腕"}
    for cam in cameras:
        name = cam.get("name") or ""
        if not name:
            continue
        video[name] = {
            "topic": cam.get("topic", ""),
            "schema": "qrdf.v0.CameraFrame",
            "lerobot_key": f"observation.images.{name}",
            "shape": [cam.get("height", 480), cam.get("width", 640)],
        }
        preview_streams.append(
            {
                "id": name,
                "video_key": name,
                "label": label_map.get(name, name),
                "role": role_map.get(name, "aux"),
            }
        )
    robot = metadata.get("robot") or {}
    timing = metadata.get("timing") or {}
    metrics = _load_json(episode_dir / "metrics.json")
    resolved_fps = float(
        metrics.get("align_fps") or timing.get("nominal_fps") or timing.get("fps") or fps or 30.0
    )
    return {
        "qrdf_version": "0.1.0",
        "modality_schema": "quicdata.sensor_modality.v1",
        "robot_type": robot.get("name") or robot.get("type") or "unknown",
        "num_arms": robot.get("num_arms", 1),
        "fps": resolved_fps,
        "video": video,
        "state": {},
        "action": {},
        "preview_streams": preview_streams
        or [
            {"id": "front", "video_key": "front", "label": "头部", "role": "main"},
            {"id": "left_wrist", "video_key": "left_wrist", "label": "左腕", "role": "aux"},
            {"id": "right_wrist", "video_key": "right_wrist", "label": "右腕", "role": "aux"},
        ],
    }


def load_modality_json(storage_path: str | None) -> dict[str, Any]:
    root = resolve_qrdf_root(storage_path)
    if not root:
        return {}
    path = root / "modality.json"
    if not path.is_file():
        return {}
    return _load_json(path)


def ensure_modality_json(storage_path: str | None, *, fps: float = 30.0) -> dict[str, Any]:
    """Preprocessing or review stage: write modality.json if missing (does not overwrite existing)."""
    root = resolve_qrdf_root(storage_path)
    if not root:
        return {"written": False, "reason": "storage_path_not_found"}
    modality_path = root / "modality.json"
    if modality_path.is_file():
        return {"written": False, "reason": "already_exists", "path": str(modality_path)}
    episode_dir = _find_episode_dir(root)
    if not episode_dir:
        return {"written": False, "reason": "episode_dir_not_found"}
    payload = build_modality_json(episode_dir, fps=fps)
    modality_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return {"written": True, "path": str(modality_path)}
