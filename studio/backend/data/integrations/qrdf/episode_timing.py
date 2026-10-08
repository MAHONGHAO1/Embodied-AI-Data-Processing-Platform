"""Fix QRDF episode timing metadata (based on MCAP message timestamps, without modifying vendor/qrdf)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from qrdf.registry.topics import EPISODE_EVENT

from data.integrations.qrdf.episode_metrics import patch_metrics_after_timing_repair


def repair_episode_timing(episode_path: str | Path) -> dict[str, Any]:
    """Overwrite recorder wall-clock timing with message timestamps in MCAP, and safely update metrics frame rate fields."""
    from qrdf.reader.episode import Episode

    episode_path = Path(episode_path)
    episode = Episode(episode_path)

    start_ns: int | None = None
    end_ns: int | None = None
    for topic in episode.list_topics():
        if topic == EPISODE_EVENT:
            continue
        for msg in episode.iter_topic(topic):
            ts = int(msg.log_time)
            start_ns = ts if start_ns is None else min(start_ns, ts)
            end_ns = ts if end_ns is None else max(end_ns, ts)

    if start_ns is None or end_ns is None or end_ns <= start_ns:
        return {}

    duration_s = (end_ns - start_ns) / 1e9
    timing = episode.metadata.timing
    timing.start_timestamp_ns = start_ns
    timing.end_timestamp_ns = end_ns
    timing.duration_s = duration_s
    episode.metadata.save(episode_path / "metadata.json")

    patch_metrics_after_timing_repair(episode_path, duration_s)

    return {
        "start_timestamp_ns": start_ns,
        "end_timestamp_ns": end_ns,
        "duration_s": round(duration_s, 6),
    }
