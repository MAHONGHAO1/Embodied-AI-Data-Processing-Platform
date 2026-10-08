"""QRDF metadata detail aggregation (P1)."""

from __future__ import annotations

from typing import Any

from data.integrations.qrdf.frame_preview import get_episode_detail
from data.integrations.qrdf.service import (
    get_dataset_summary,
    list_episodes,
    list_topic_stats,
    validate_dataset,
)


def build_qrdf_metadata_detail(
    *,
    storage_path: str | None,
    metadata_json: dict | None,
    qrdf_id: int | None = None,
    task_id: int | None = None,
) -> dict[str, Any]:
    meta = metadata_json or {}

    quality = meta.get("quality_check") or {}
    if not quality and storage_path:
        quality = validate_dataset(storage_path)

    # Prefer persisted task facts before falling back to QRDF SDK reads.
    cached_episodes = meta.get("episodes") or []
    episodes = cached_episodes if cached_episodes else list_episodes(storage_path) or []

    cached_topics = meta.get("topics") or []
    topics = cached_topics or list_topic_stats(storage_path) or []

    sdk_summary = get_dataset_summary(storage_path)

    # Avoid per-episode reads once the task already has a persisted episode cache.
    episode_details = []
    if storage_path and not cached_episodes:
        for episode in episodes[:20]:
            episode_id = episode.get("episode_id")
            if episode_id:
                detail = get_episode_detail(storage_path, episode_id)
                if detail.get("ok"):
                    episode_details.append(detail)

    tags = meta.get("tags") or []

    return {
        "qrdf_id": qrdf_id,
        "task_id": task_id,
        "sdk_summary": sdk_summary,
        "quality_check": quality,
        "episodes": episodes,
        "episode_details": episode_details,
        "topics": topics,
        "tags": tags,
        "metadata": dict(meta),
    }
