"""QRDF EEF / gripper timeseries extraction (P1 multimodal action curves, EEF native timeline)."""

from __future__ import annotations

from typing import Any

from data.integrations.qrdf.episode_metrics import (
    pick_camera_topic_from_names,
    topic_names_from_episode_dir,
)
from data.integrations.qrdf.multimodal_preview import build_multimodal_session
from data.integrations.qrdf.service import get_reader, resolve_dataset_path


def detect_view_toggles(storage_path: str | None) -> dict[str, bool]:
    """Lightweight detection of view toggles (reads metadata.json topic list only, without opening MCAP)."""
    default = {"rgb": True, "action_chart": False, "depth": False}
    dataset_path = resolve_dataset_path(storage_path)
    reader = get_reader(storage_path)
    if not reader or not dataset_path:
        return default
    episode_ids = reader.list_episodes()
    if not episode_ids:
        return default

    topics = topic_names_from_episode_dir(dataset_path / "episodes" / episode_ids[0])
    if not topics:
        episode = reader.load_episode(episode_ids[0])
        topics = episode.list_topics()

    has_eef = any(any(hint in t for hint in ("eef", "hand_state", "joint_state")) for t in topics)
    has_gripper = any("gripper" in t or "hand_state" in t for t in topics)
    has_depth = any("depth" in t for t in topics)
    has_rgb = bool(pick_camera_topic_from_names(topics)) or any("/camera/" in t for t in topics)
    return {
        "rgb": has_rgb,
        "action_chart": has_eef or has_gripper,
        "depth": has_depth,
    }


def extract_episode_timeseries(
    storage_path: str | None,
    *,
    episode_id: str | None = None,
    fps: float | None = None,
    total_frames: int | None = None,
) -> dict[str, Any]:
    """Extract curves on EEF/gripper reference topic native timeline (without resampling to camera frame count)."""
    del (
        fps,
        total_frames,
    )  # Preserve parameter signature compatibility; timeline is based on MCAP reference topic
    session = build_multimodal_session(storage_path, episode_id=episode_id)
    if not session.get("ok"):
        return {
            "ok": False,
            "message": session.get("message", "时序抽取失败"),
            "episode_id": episode_id,
            "series": [],
            "times": [],
        }

    if not session.get("series"):
        return {
            "ok": False,
            "message": "数据集无 EEF / 夹爪 topic",
            "episode_id": session.get("episode_id"),
            "series": [],
            "times": [],
        }

    sample_count = int(session.get("sample_count") or 0)
    return {
        "ok": True,
        "episode_id": session.get("episode_id"),
        "fps": session.get("native_fps"),
        "native_fps": session.get("native_fps"),
        "total_frames": sample_count,
        "sample_count": sample_count,
        "max_frame_index": session.get("max_index", 0),
        "max_index": session.get("max_index", 0),
        "duration_sec": session.get("duration_sec", 0),
        "reference_topic": session.get("reference_topic"),
        "start_timestamp_ns": session.get("start_timestamp_ns"),
        "end_timestamp_ns": session.get("end_timestamp_ns"),
        "times": session.get("times") or [],
        "timestamps_ns": session.get("timestamps_ns") or [],
        "series": session.get("series") or [],
        "dual_arm": session.get("dual_arm", False),
        "streams": session.get("streams") or [],
        "time_axis": session.get("time_axis", "eef_reference"),
        "reference_kind": session.get("reference_kind"),
        "reference_label": session.get("reference_label"),
    }
