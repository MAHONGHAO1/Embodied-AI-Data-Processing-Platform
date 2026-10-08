"""Annotation workbench 3-way RGB topic preview (without modifying vendor/qrdf).

Prioritize directly reusing existing preview MP4 files in the QRDF episode package:
  preview.mp4 / preview_left.mp4 / preview_right.mp4
When left/right wrist videos are missing, do not parse MCAP or write cache; frontend skips the corresponding panels accordingly.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any

from qrdf.models.episode import EpisodeMetadata
from qrdf.registry.topics import CAMERA_FRONT_RGB, CAMERA_WRIST_RGB

from data.config import settings
from data.integrations.qrdf.episode_metrics import (
    infer_fps_from_metrics,
    pick_camera_topic_from_names,
    topic_names_from_episode_dir,
)
from data.integrations.qrdf.modality import load_modality_json
from data.integrations.qrdf.paths import resolve_legacy_qrdf_preview_file
from data.integrations.qrdf.preview_export import infer_camera_fps
from data.integrations.qrdf.rgb_preview import (
    ensure_qrdf_rgb_previews,
    load_preview_timeline,
    load_qrdf_preview_descriptor,
    resolve_preview_stream_artifact,
)
from data.integrations.qrdf.service import (
    classify_storage_layout,
    get_reader,
    resolve_dataset_path,
    resolve_preview_file,
)

# Frontend stream id / query topic -> QRDF topic candidates
TOPIC_ALIASES: dict[str, list[str]] = {
    "head_color": [CAMERA_FRONT_RGB, "/camera/head/rgb", "/camera/front/rgb"],
    "head": [CAMERA_FRONT_RGB, "/camera/head/rgb", "/camera/front/rgb"],
    "front": [CAMERA_FRONT_RGB, "/camera/head/rgb", "/camera/front/rgb"],
    "left_wrist_color": ["/camera/left/wrist/rgb", "/camera/left_wrist/rgb"],
    "left_wrist": ["/camera/left/wrist/rgb", "/camera/left_wrist/rgb"],
    "right_wrist_color": ["/camera/right/wrist/rgb", "/camera/right_wrist/rgb"],
    "right_wrist": ["/camera/right/wrist/rgb", "/camera/right_wrist/rgb"],
    "wrist": [CAMERA_WRIST_RGB, "/camera/wrist/rgb"],
}

# Legacy package preview filename convention (same level as data.mcap; left/right wrist not included in metadata.preview_file)
PACKAGE_PREVIEW_FILES: dict[str, tuple[str, ...]] = {
    "front": ("preview.mp4",),
    "head": ("preview.mp4",),
    "head_color": ("preview.mp4",),
    "left_wrist": ("preview_left.mp4", "preview_left_wrist.mp4"),
    "left_wrist_color": ("preview_left.mp4", "preview_left_wrist.mp4"),
    "right_wrist": ("preview_right.mp4", "preview_right_wrist.mp4"),
    "right_wrist_color": ("preview_right.mp4", "preview_right_wrist.mp4"),
}

PREVIEW_STREAMS: list[dict[str, str]] = [
    {"id": "front", "role": "main", "label": "头部", "topic": "head_color"},
    {"id": "left_wrist", "role": "aux", "label": "左臂", "topic": "left_wrist_color"},
    {"id": "right_wrist", "role": "aux", "label": "右臂", "topic": "right_wrist_color"},
]

_FRONT_CAMERA_TOPICS = frozenset({CAMERA_FRONT_RGB, "/camera/front/rgb", "/camera/head/rgb"})
logger = logging.getLogger(__name__)


def resolve_qrdf_topic_from_names(topics: set[str], alias: str) -> str | None:
    """Resolve aliases such as head_color to actual topic in episode (topic name only)."""
    for candidate in TOPIC_ALIASES.get(alias, [alias]):
        if candidate in topics:
            return candidate
    if alias in topics:
        return alias
    if alias.endswith("_color"):
        base = alias[: -len("_color")]
        for candidate in TOPIC_ALIASES.get(base, []):
            if candidate in topics:
                return candidate
    return None


def resolve_qrdf_topic(episode, alias: str) -> str | None:
    """Resolve aliases such as head_color to actual topic in episode."""
    from data.integrations.qrdf.episode_metrics import episode_topic_names

    topics = set(episode_topic_names(episode))
    return resolve_qrdf_topic_from_names(topics, alias)


def _cache_path(
    storage_path: str | None,
    episode_id: str,
    alias: str,
    *,
    task_id: int | None = None,
) -> Path:
    if task_id is not None:
        safe_alias = re.sub(r"[^\w.\-]+", "_", alias)[:64]
        safe_ep = re.sub(r"[^\w.\-]+", "_", episode_id)[:64]
        return (
            Path(settings.storage_root)
            / "previews"
            / "topics"
            / f"task{task_id}"
            / f"{safe_alias}_{safe_ep}.mp4"
        )
    digest = hashlib.md5(
        f"{storage_path}:{episode_id}:{alias}".encode(), usedforsecurity=False
    ).hexdigest()[:16]
    return Path(settings.storage_root) / "previews" / "topics" / f"{digest}.mp4"


def _episode_path_for(storage_path: str | None, episode_id: str | None = None) -> Path | None:
    """Resolve exactly one dataset episode without falling back across IDs."""
    layout = classify_storage_layout(storage_path)
    episode_path = layout.get("episode_dir") if layout.get("kind") == "single_episode" else None
    if episode_path is not None:
        if episode_id and episode_path.name != episode_id:
            return None
        return Path(episode_path) if Path(episode_path).is_dir() else None

    reader = get_reader(storage_path)
    if not reader:
        return None
    episode_ids = reader.list_episodes()
    if not episode_ids:
        return None
    selected_episode_id = episode_id or episode_ids[0]
    if selected_episode_id not in episode_ids:
        return None
    dataset_path = resolve_dataset_path(storage_path)
    episode_path = (dataset_path / "episodes" / selected_episode_id) if dataset_path else None
    return episode_path if episode_path is not None and episode_path.is_dir() else None


def list_episode_rgb_topics(storage_path: str | None, episode_id: str) -> list[str]:
    """Return only RGB streams declared or present for one Episode.

    QRDF detail pages use this list as an allowlist.  In particular, depth and
    camera-info topics never become video preview choices merely because they
    live beside an RGB stream.
    """
    episode_path = _episode_path_for(storage_path, episode_id)
    if episode_path is None:
        return []
    available_topics = set(topic_names_from_episode_dir(episode_path))
    declared_rgb_topics: set[str] = set()
    try:
        metadata = EpisodeMetadata.load(episode_path / "metadata.json")
        declared_rgb_topics = {
            stream.rgb_topic
            for device in metadata.devices
            for stream in device.streams
            if stream.rgb_topic
        }
    except (OSError, ValueError):
        pass
    inferred_rgb_topics = {
        topic
        for topic in available_topics
        if topic.lower().endswith("/rgb")
        or "/rgb/" in topic.lower()
        or topic.lower().endswith("_rgb")
    }
    return sorted((declared_rgb_topics | inferred_rgb_topics) & available_topics)


def resolve_episode_preview_root(
    storage_path: str | None,
    *,
    episode_id: str | None = None,
) -> tuple[str, Path] | None:
    """Resolve one episode's manifest-backed preview root for internal services."""
    episode_path = _episode_path_for(storage_path, episode_id)
    if episode_path is None:
        return None
    return episode_path.name, episode_path / "media" / "preview"


def resolve_topic_preview_file(
    storage_path: str | None,
    *,
    topic_alias: str,
    generate: bool = True,
    task_id: int | None = None,
    episode_id: str | None = None,
) -> tuple[Path | None, str | None]:
    """Read QRDF VFR preview by topic alias; legacy fallback is read-only."""
    status = get_topic_preview_status(
        storage_path,
        topic_alias=topic_alias,
        generate=generate,
        task_id=task_id,
        episode_id=episode_id,
    )
    raw_path = status.get("path")
    return (Path(raw_path) if raw_path else None), status.get("qrdf_topic")


def _episode_preview_context(
    storage_path: str | None,
    topic_alias: str,
    *,
    episode_id: str | None = None,
) -> tuple[Path, str, list[str]] | None:
    ep_path = _episode_path_for(storage_path, episode_id)
    if ep_path is None:
        return None
    topic_names = topic_names_from_episode_dir(ep_path) if ep_path else []
    topic_set = set(topic_names)
    qrdf_topic = resolve_qrdf_topic_from_names(topic_set, topic_alias)
    if not qrdf_topic:
        return None

    requested_topics = sorted(
        {
            resolved
            for stream in PREVIEW_STREAMS
            if (resolved := resolve_qrdf_topic_from_names(topic_set, stream["topic"]))
        }
    )
    if qrdf_topic not in requested_topics:
        requested_topics.append(qrdf_topic)
        requested_topics.sort()
    return ep_path, qrdf_topic, requested_topics


def _legacy_topic_preview(
    storage_path: str | None,
    *,
    episode_path: Path | None,
    qrdf_topic: str | None,
    topic_alias: str,
    task_id: int | None,
    episode_id: str | None,
) -> Path | None:
    cache = None
    if episode_path is not None:
        cache = _cache_path(storage_path, episode_path.name, topic_alias, task_id=task_id)
        if cache.is_file():
            return cache
        default_preview = resolve_legacy_qrdf_preview_file(episode_path)
        if qrdf_topic in _FRONT_CAMERA_TOPICS and default_preview.is_file():
            return default_preview
    if episode_id is None and topic_alias in ("head_color", "head", "front"):
        return resolve_preview_file(storage_path, generate=False, task_id=task_id)
    return None


def get_topic_preview_status(
    storage_path: str | None,
    *,
    topic_alias: str = "head_color",
    generate: bool = False,
    task_id: int | None = None,
    episode_id: str | None = None,
    media_api: str = "",
    timeline_api: str = "",
) -> dict[str, Any]:
    context = _episode_preview_context(storage_path, topic_alias, episode_id=episode_id)
    if context is not None:
        episode_path, qrdf_topic, requested_topics = context
        preview_root = episode_path / "media" / "preview"
        try:
            if generate:
                preview_root = ensure_qrdf_rgb_previews(
                    episode_path,
                    topics=requested_topics,
                )
            descriptor = load_qrdf_preview_descriptor(
                preview_root,
                topic=qrdf_topic,
                media_api=media_api,
                timeline_api=timeline_api,
            )
            if descriptor is not None:
                path = (
                    resolve_preview_stream_artifact(
                        preview_root,
                        topic=qrdf_topic,
                        artifact="media",
                    )
                    if descriptor.get("media_api")
                    or descriptor.get("status") in {"complete", "partial"}
                    else None
                )
                return {
                    **descriptor,
                    "available": bool(path),
                    "path": str(path) if path else "",
                    "media_type": "video/mp4",
                    "qrdf_topic": qrdf_topic,
                    "topic_alias": topic_alias,
                    "episode_id": episode_path.name,
                }
        except (ImportError, OSError, RuntimeError, ValueError):
            logger.warning(
                "QRDF preview unavailable; checking legacy fallback topic=%s",
                qrdf_topic,
                exc_info=True,
            )
        legacy = _legacy_topic_preview(
            storage_path,
            episode_path=episode_path,
            qrdf_topic=qrdf_topic,
            topic_alias=topic_alias,
            task_id=task_id,
            episode_id=episode_id,
        )
    else:
        qrdf_topic = None
        legacy = _legacy_topic_preview(
            storage_path,
            episode_path=None,
            qrdf_topic=None,
            topic_alias=topic_alias,
            task_id=task_id,
            episode_id=episode_id,
        )

    if legacy and legacy.is_file():
        return {
            "available": True,
            "path": str(legacy),
            "media_type": "video/mp4",
            "qrdf_topic": qrdf_topic,
            "topic_alias": topic_alias,
            "episode_id": episode_path.name if context is not None else episode_id,
            "status": "legacy",
            "media_api": media_api,
            "timeline_api": "",
            "legacy": True,
        }
    return {
        "available": False,
        "error": f"topic {topic_alias} 预览不可用",
        "qrdf_topic": qrdf_topic,
        "topic_alias": topic_alias,
        "episode_id": episode_id,
        "status": "failed",
        "media_api": "",
        "timeline_api": "",
        "legacy": False,
    }


def load_topic_preview_timeline(
    storage_path: str | None,
    *,
    topic_alias: str,
    episode_id: str | None = None,
) -> dict[str, Any]:
    context = _episode_preview_context(storage_path, topic_alias, episode_id=episode_id)
    if context is None:
        raise ValueError("QRDF preview timeline is unavailable")
    episode_path, qrdf_topic, _requested_topics = context
    return load_preview_timeline(
        episode_path / "media" / "preview",
        topic=qrdf_topic,
    )


def infer_task_fps(storage_path: str | None) -> float:
    """Infer task frame rate: prioritize metrics.json to avoid full scan of MCAP."""
    dataset_path = resolve_dataset_path(storage_path)
    reader = get_reader(storage_path)
    if not reader or not dataset_path:
        return 30.0
    episode_ids = reader.list_episodes()
    if not episode_ids:
        return 30.0
    eid = episode_ids[0]
    ep_path = dataset_path / "episodes" / eid
    topics = topic_names_from_episode_dir(ep_path)
    camera = (
        resolve_qrdf_topic_from_names(set(topics), "head_color")
        or resolve_qrdf_topic_from_names(set(topics), "front")
        or pick_camera_topic_from_names(topics)
    )
    if camera:
        from_metrics = infer_fps_from_metrics(ep_path, camera)
        if from_metrics and from_metrics > 0:
            return from_metrics

    episode = reader.load_episode(eid)
    topic = resolve_qrdf_topic(episode, "head_color") or resolve_qrdf_topic(episode, "front")
    if topic:
        return infer_camera_fps(episode, topic)
    return 30.0


def list_available_streams(
    storage_path: str | None,
    *,
    task_id: int | None = None,
    api_base: str = "/api/v1/annotate",
    generate: bool = False,
) -> list[dict[str, Any]]:
    modality = load_modality_json(storage_path)
    preview_streams = modality.get("preview_streams") or []
    if preview_streams:
        streams = [
            {
                "id": s.get("id", ""),
                "role": s.get("role", "aux"),
                "label": s.get("label", s.get("id", "")),
            }
            for s in preview_streams
            if s.get("id")
        ]
    else:
        layout = classify_storage_layout(storage_path)
        if layout.get("kind") == "single_episode" and layout.get("episode_dir"):
            topics = set(topic_names_from_episode_dir(layout["episode_dir"]))
            streams = []
            for stream in PREVIEW_STREAMS:
                if resolve_qrdf_topic_from_names(topics, stream["topic"]):
                    streams.append({k: v for k, v in stream.items() if k != "topic"})
        else:
            dataset_path = resolve_dataset_path(storage_path)
            reader = get_reader(storage_path)
            if not reader or not dataset_path or not reader.list_episodes():
                streams = []
            else:
                episode_id = reader.list_episodes()[0]
                topics = set(topic_names_from_episode_dir(dataset_path / "episodes" / episode_id))
                streams = []
                for stream in PREVIEW_STREAMS:
                    alias = stream["topic"]
                    if resolve_qrdf_topic_from_names(topics, alias):
                        streams.append({k: v for k, v in stream.items() if k != "topic"})
        if not streams:
            streams = [{k: v for k, v in s.items() if k != "topic"} for s in PREVIEW_STREAMS]

    if task_id is None:
        return streams

    enriched: list[dict[str, Any]] = []
    for stream in streams:
        alias = str(stream["id"])
        query = f"?topic={alias}"
        descriptor_api = f"{api_base}/{task_id}/preview{query}"
        media_api = f"{api_base}/{task_id}/preview/media{query}"
        timeline_api = f"{api_base}/{task_id}/preview/timeline{query}"
        status = get_topic_preview_status(
            storage_path,
            topic_alias=alias,
            generate=generate,
            task_id=task_id,
            media_api=media_api,
            timeline_api=timeline_api,
        )
        public_status = {key: value for key, value in status.items() if key != "path"}
        public_status["descriptor_api"] = descriptor_api
        enriched.append({**stream, "preview": public_status})
    return enriched
