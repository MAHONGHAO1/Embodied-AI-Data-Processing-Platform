"""QRDF Episode frame data sampling preview (without modifying vendor/qrdf)."""

from __future__ import annotations

import base64
import io
from typing import Any

from qrdf.schema.qrdf.v0 import camera_pb2
from qrdf.utils.image import camera_frame_to_rgb_array

from data.integrations.qrdf.preview_export import _pick_camera_topic, infer_camera_fps
from data.integrations.qrdf.service import (
    INCOMPATIBLE_QRDF_FORMAT,
    _load_episode_for_storage,
    classify_storage_layout,
)
from data.utils.formatting import normalize_api_fps


def _pick_topic(episode, topic: str | None) -> str | None:
    topics = episode.list_topics()
    if topic and topic in topics:
        return topic
    camera = _pick_camera_topic(episode)
    if camera:
        return camera
    for candidate in topics:
        if candidate.startswith("/observation/") or candidate.startswith("/camera/"):
            return candidate
    return topics[0] if topics else None


def _is_camera_topic(topic: str) -> bool:
    return topic.startswith("/camera/") or topic.endswith("/rgb") or topic.endswith("/depth")


def _thumbnail_base64(rgb_array, *, max_size: int = 360) -> str:
    from PIL import Image

    img = Image.fromarray(rgb_array)
    img.thumbnail((max_size, max_size))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _state_payload(message) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if hasattr(message, "position") and message.position:
        payload["position"] = list(message.position)
    if hasattr(message, "velocity") and message.velocity:
        payload["velocity"] = list(message.velocity)
    if hasattr(message, "orientation_xyzw") and message.orientation_xyzw:
        payload["orientation_xyzw"] = list(message.orientation_xyzw)
    if hasattr(message, "arm") and message.arm:
        payload["arm"] = message.arm
    if hasattr(message, "names") and message.names:
        payload["names"] = list(message.names)
    return payload


def sample_episode_frames(
    storage_path: str | None,
    episode_id: str,
    *,
    topic: str | None = None,
    start_index: int = 0,
    limit: int = 10,
) -> dict[str, Any]:
    """Sample episode frames by topic; camera returns JPEG base64, status topics return numeric values."""
    limit = max(1, min(limit, 50))
    start_index = max(0, start_index)

    layout = classify_storage_layout(storage_path)
    if layout["kind"] == "incompatible":
        return {"ok": False, "message": layout["error"], "frames": []}

    episode = _load_episode_for_storage(storage_path, episode_id)
    if not episode:
        if layout["kind"] == "missing":
            return {"ok": False, "message": "存储路径无效或数据文件不存在", "frames": []}
        return {"ok": False, "message": INCOMPATIBLE_QRDF_FORMAT, "frames": []}
    selected_topic = _pick_topic(episode, topic)
    if not selected_topic:
        return {"ok": False, "message": "Episode 无可用 topic", "frames": []}

    messages = list(episode.iter_topic(selected_topic))
    total = len(messages)
    if total == 0:
        return {
            "ok": False,
            "message": f"topic {selected_topic} 无消息",
            "episode_id": episode_id,
            "topic": selected_topic,
            "total_frames": 0,
            "frames": [],
        }

    indices = list(range(start_index, min(start_index + limit, total)))
    frames: list[dict[str, Any]] = []
    is_camera = _is_camera_topic(selected_topic)

    for idx in indices:
        mcap_msg = messages[idx]
        item: dict[str, Any] = {
            "frame_index": idx,
            "timestamp_ns": mcap_msg.log_time,
            "topic": selected_topic,
            "schema": mcap_msg.schema_name,
        }
        msg = mcap_msg.message
        if is_camera and isinstance(msg, camera_pb2.CameraFrame) and msg.data:
            try:
                rgb = camera_frame_to_rgb_array(msg)
                item["preview_type"] = "image/jpeg"
                item["thumbnail_base64"] = _thumbnail_base64(rgb)
                item["width"] = int(msg.width or rgb.shape[1])
                item["height"] = int(msg.height or rgb.shape[0])
            except Exception as exc:
                item["preview_type"] = "error"
                item["error"] = str(exc)
        else:
            item["preview_type"] = "state"
            item["data"] = _state_payload(msg)
        frames.append(item)

    fps = normalize_api_fps(infer_camera_fps(episode, selected_topic)) if is_camera else None
    return {
        "ok": True,
        "episode_id": episode_id,
        "topic": selected_topic,
        "total_frames": total,
        "start_index": start_index,
        "limit": limit,
        "fps": fps,
        "frames": frames,
    }


def get_episode_detail(storage_path: str | None, episode_id: str) -> dict[str, Any]:
    """Read full episode metadata and topic statistics."""
    layout = classify_storage_layout(storage_path)
    if layout["kind"] == "incompatible":
        return {"episode_id": episode_id, "ok": False, "message": layout["error"]}

    episode = _load_episode_for_storage(storage_path, episode_id)
    if not episode:
        return {"episode_id": episode_id, "ok": False, "message": INCOMPATIBLE_QRDF_FORMAT}
    meta = episode.metadata
    topics = episode.list_topics()
    topic_stats = {t: episode.mcap_reader.message_count(t) for t in topics}
    frame_count = 0
    camera_topic = _pick_camera_topic(episode)
    if camera_topic:
        frame_count = topic_stats.get(camera_topic, 0)
    elif topics:
        frame_count = max(topic_stats.values()) if topic_stats else 0

    timing = {}
    if meta and meta.timing:
        timing = {
            "duration_s": meta.timing.duration_s,
            "start_timestamp_ns": meta.timing.start_timestamp_ns,
            "end_timestamp_ns": meta.timing.end_timestamp_ns,
        }

    metadata_path = episode.path / "metadata.json"
    raw_metadata = {}
    if metadata_path.is_file():
        import json

        try:
            raw_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    metrics = {}
    metrics_path = episode.path / "metrics.json"
    if metrics_path.is_file():
        import json

        try:
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    return {
        "ok": True,
        "episode_id": episode_id,
        "duration_sec": timing.get("duration_s"),
        "frame_count": frame_count,
        "topics": topics,
        "topic_stats": topic_stats,
        "timing": timing,
        "metadata": raw_metadata,
        "metrics": metrics,
        "preview_topic": camera_topic,
    }
