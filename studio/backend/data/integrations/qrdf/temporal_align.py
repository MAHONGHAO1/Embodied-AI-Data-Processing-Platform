"""QRDF temporal standardization (preserves native sampling rate: repairs disorder only, no downsampling or resampling)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from qrdf.reader.episode import Episode
from qrdf.registry.topics import EPISODE_EVENT

from data.integrations.qrdf.episode_metrics import (
    is_episode_aligned,
    is_episode_timing_standardized,
    measure_episode_message_counts,
    measure_episode_topic_frequencies,
    refresh_native_metrics,
)
from data.integrations.qrdf.episode_timing import repair_episode_timing
from data.integrations.qrdf.mcap_ops import clone_mcap_message, rewrite_episode_mcap


def _sort_topic_messages(messages: list) -> tuple[list, int]:
    """Sort by log_time to repair temporal disorder, returning (sorted_messages, disordered_count)."""
    if not messages:
        return [], 0
    out_of_order = sum(
        1 for i, msg in enumerate(messages) if i > 0 and msg.log_time < messages[i - 1].log_time
    )
    sorted_msgs = sorted(messages, key=lambda m: m.log_time)
    return [clone_mcap_message(m) for m in sorted_msgs], out_of_order


def standardize_episode_timing(
    episode_path: Path,
    *,
    align_fps: float | None = None,
    max_time_delta_ms: float = 100.0,
    force_realign: bool = False,
) -> dict[str, Any]:
    """Repair temporal disorder across topics for a single episode, preserving original message counts and sampling rates (no resampling)."""
    del (
        align_fps,
        max_time_delta_ms,
    )  # Preserve parameter signature compatibility; fps grid alignment is no longer performed

    episode_path = Path(episode_path)
    episode = Episode(episode_path)
    reader = episode.mcap_reader
    topics = [t for t in reader.list_topics() if t != EPISODE_EVENT]
    if not topics:
        return {"episode_id": episode.episode_id, "ok": False, "message": "无可用 topic"}

    if (
        is_episode_timing_standardized(episode_path) or is_episode_aligned(episode_path)
    ) and not force_realign:
        metrics = measure_episode_message_counts(episode)
        return {
            "episode_id": episode.episode_id,
            "ok": True,
            "skipped": True,
            "message_count": metrics,
            "message": "已时序标准化，跳过重复处理",
        }

    native_frequency_hz = measure_episode_topic_frequencies(episode)
    native_message_count = measure_episode_message_counts(episode)

    disorder_removed = 0
    monotonic_by_topic: dict[str, list] = {}
    for topic in topics:
        msgs, removed = _sort_topic_messages(reader.get_all_messages(topic))
        disorder_removed += removed
        monotonic_by_topic[topic] = msgs

    if disorder_removed > 0:
        rewrite_episode_mcap(episode_path, episode.metadata.data_file, monotonic_by_topic)
        episode = Episode(episode_path)

    timing = repair_episode_timing(episode_path)
    metrics = refresh_native_metrics(episode_path, timing_standardized=True)

    message_counts = metrics.get("message_count") or measure_episode_message_counts(episode)
    preserved = all(
        message_counts.get(topic, 0) == native_message_count.get(topic, 0)
        for topic in native_message_count
    )

    return {
        "episode_id": episode.episode_id,
        "ok": True,
        "skipped": False,
        "resampled": False,
        "disorder_removed": disorder_removed,
        "native_preserved": preserved,
        "message_count": message_counts,
        "native_message_count": native_message_count,
        "native_frequency_hz": native_frequency_hz,
        "frequency_hz": metrics.get("frequency_hz", {}),
        "timing": timing,
        "message": (
            f"时序修复完成 disorder_removed={disorder_removed}，保留原生采样率"
            if disorder_removed > 0
            else "保留原生采样率，无需时序修复"
        ),
    }


def standardize_dataset_timing(
    dataset_path: Path,
    *,
    align_fps: float | None = None,
    max_time_delta_ms: float = 100.0,
    force_realign: bool = False,
) -> dict[str, Any]:
    del align_fps, max_time_delta_ms  # Preserve parameter signature compatibility

    summary: dict[str, Any] = {
        "episodes_processed": 0,
        "valid_episodes": 0,
        "disorder_removed": 0,
        "resampled": False,
        "details": [],
    }

    episodes_dir = dataset_path / "episodes"
    if not episodes_dir.is_dir():
        summary["message"] = "数据集无 episodes 目录"
        return summary

    for ep_path in sorted(episodes_dir.iterdir()):
        if not ep_path.is_dir():
            continue
        try:
            result = standardize_episode_timing(
                ep_path,
                force_realign=force_realign,
            )
            summary["episodes_processed"] += 1
            if result.get("ok"):
                summary["valid_episodes"] += 1
            summary["disorder_removed"] += int(result.get("disorder_removed") or 0)
            summary["details"].append(result.get("message", ep_path.name))
        except Exception as exc:
            summary["details"].append(f"{ep_path.name}: 时序标准化跳过 ({exc})")

    summary["message"] = (
        f"时序标准化完成：{summary['valid_episodes']}/{summary['episodes_processed']} 集有效，"
        f"保留原生采样率（disorder_removed={summary['disorder_removed']}）"
    )
    return summary
