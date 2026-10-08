"""Allowlisted durable and cached facts for legacy QRDF preprocessing."""

from __future__ import annotations

import math
import re
from typing import Any

from data.security.patterns import contains_cloud_access_key_value

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_TOPIC_NAME = re.compile(r"^/[A-Za-z0-9_~./-]{1,511}$")
_URI_REFERENCE = re.compile(
    r"(?i)(?:[a-z][a-z0-9+.-]*://|(?<![A-Za-z0-9])(?:urn|file|jdbc|mongodb|postgresql?|redis):)"
)
_POSIX_PATH = re.compile(r"(?:^|[^A-Za-z0-9_])/(?!/)(?:[^\s,\])}]+)")
_WINDOWS_PATH = re.compile(r"(?i)(?:^|[^A-Za-z0-9_])[a-z]:[\\/]")
_SECRET_MARKER = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:access[_-]?key(?:[_-]?id)?|api[_-]?key|authorization|"
    r"bearer|password|secret|signature|token)(?=$|[^A-Za-z0-9])"
)
_MAX_ITEMS = 200
_CLEANING_NUMBERS = (
    "episodes_processed",
    "removed_empty",
    "removed_invalid",
    "removed_duplicate",
    "removed_noise",
    "removed_empty_files",
    "messages_before",
    "messages_after",
)
_ALIGNING_NUMBERS = ("episodes_processed", "valid_episodes", "disorder_removed")


def _safe_text(value: object, *, limit: int = 1000) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if (
        not normalized
        or len(normalized) > limit
        or ".." in normalized.replace("\\", "/").split("/")
        or _URI_REFERENCE.search(normalized)
        or _POSIX_PATH.search(normalized)
        or _WINDOWS_PATH.search(normalized)
        or _SECRET_MARKER.search(normalized)
        or contains_cloud_access_key_value(normalized)
    ):
        return None
    return normalized


def sanitize_fact_text(value: object, *, limit: int = 1000) -> str | None:
    """Return text safe for durable preprocess and quality facts."""
    return _safe_text(value, limit=limit)


def _identifier(value: object) -> str | None:
    safe = _safe_text(value, limit=128)
    return safe if safe and _IDENTIFIER.fullmatch(safe) else None


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _topic_name(value: object) -> str | None:
    if not isinstance(value, str) or value != value.strip() or not _TOPIC_NAME.fullmatch(value):
        return None
    if ".." in value.split("/"):
        return None
    return value


def _topic_names(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:1000]:
        topic = _topic_name(item)
        if topic and topic not in result:
            result.append(topic)
    return result


def sanitize_episode_facts(value: object) -> list[dict[str, Any]]:
    """Return storage-free QRDF episode summaries suitable for durable metadata."""
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in value[:1000]:
        if not isinstance(raw, dict):
            continue
        episode: dict[str, Any] = {}
        episode_id = _identifier(raw.get("episode_id"))
        if episode_id:
            episode["episode_id"] = episode_id
        duration = _number(raw.get("duration_sec"))
        if duration is not None and duration >= 0:
            episode["duration_sec"] = duration
        frame_count = _nonnegative_int(raw.get("frame_count"))
        if frame_count is not None:
            episode["frame_count"] = frame_count
        for key in ("topics", "rgb_topics"):
            topics = _topic_names(raw.get(key))
            if topics:
                episode[key] = topics
        raw_stats = raw.get("topic_stats")
        if isinstance(raw_stats, dict):
            topic_stats: dict[str, int] = {}
            for name, count in list(raw_stats.items())[:1000]:
                topic = _topic_name(name)
                safe_count = _nonnegative_int(count)
                if topic and safe_count is not None:
                    topic_stats[topic] = safe_count
            if topic_stats:
                episode["topic_stats"] = topic_stats
        if episode:
            result.append(episode)
    return result


def sanitize_topic_facts(value: object) -> list[dict[str, Any]]:
    """Return allowlisted QRDF topic counts without storage or preview pointers."""
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in value[:2000]:
        if not isinstance(raw, dict):
            continue
        topic: dict[str, Any] = {}
        name = _topic_name(raw.get("name"))
        if name:
            topic["name"] = name
        for key in ("frame_count", "message_count"):
            count = _nonnegative_int(raw.get(key))
            if count is not None:
                topic[key] = count
        if topic:
            result.append(topic)
    return result


def _sanitize_step(value: object) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result: dict[str, Any] = {}
    for key in ("name", "status", "code"):
        item = _identifier(source.get(key))
        if item:
            result[key] = item
    progress = _number(source.get("progress"))
    if progress is not None:
        result["progress"] = max(0, min(100, progress))
    if isinstance(source.get("ok"), bool):
        result["ok"] = source["ok"]
    message = _safe_text(source.get("message"))
    if message:
        result["message"] = message
    return result


def _safe_messages(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:_MAX_ITEMS]:
        safe = _safe_text(item)
        if safe and safe not in result:
            result.append(safe)
    return result


def _sanitize_dataset_ref(value: object) -> dict[str, str]:
    source = value if isinstance(value, dict) else {}
    result: dict[str, str] = {}
    for key in ("resource_type", "resource_id", "stage", "run_id"):
        item = _identifier(source.get(key))
        if item:
            result[key] = item
    return result


def _sanitize_stats(
    value: object,
    *,
    number_fields: tuple[str, ...],
    bool_fields: tuple[str, ...] = (),
) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result: dict[str, Any] = {}
    for key in number_fields:
        item = _number(source.get(key))
        if item is not None and item >= 0:
            result[key] = item
    for key in bool_fields:
        if isinstance(source.get(key), bool):
            result[key] = source[key]
    message = _safe_text(source.get("message"))
    if message:
        result["message"] = message
    details = _safe_messages(source.get("details"))
    if details:
        result["details"] = details
    return result


def sanitize_preprocess_fact(value: object) -> dict[str, Any]:
    """Return the complete allowlisted preprocess fact used by SQL and Redis."""
    source = value if isinstance(value, dict) else {}
    result: dict[str, Any] = {}
    for key in ("status", "stage", "quality", "run_id", "code"):
        item = _identifier(source.get(key))
        if item:
            result[key] = item
    for key in ("progress", "quality_score"):
        item = _number(source.get(key))
        if item is not None:
            result[key] = max(0, min(100, item)) if key == "progress" else item
    fps = _number(source.get("fps"))
    if fps is not None and fps > 0:
        result["fps"] = fps
    for key in ("started_at", "finished_at"):
        item = _safe_text(source.get(key), limit=128)
        if item:
            result[key] = item
    message = _safe_text(source.get("message"))
    if message:
        result["message"] = message

    raw_steps = source.get("steps")
    steps = (
        [_sanitize_step(item) for item in raw_steps[:_MAX_ITEMS]]
        if isinstance(raw_steps, list)
        else []
    )
    steps = [item for item in steps if item]
    if steps or isinstance(raw_steps, list):
        result["steps"] = steps
    anomalies = _safe_messages(source.get("anomalies"))
    if anomalies:
        result["anomalies"] = anomalies
    dataset_ref = _sanitize_dataset_ref(source.get("dataset_ref"))
    if dataset_ref:
        result["dataset_ref"] = dataset_ref
    cleaning = _sanitize_stats(source.get("cleaning"), number_fields=_CLEANING_NUMBERS)
    if cleaning:
        result["cleaning"] = cleaning
    aligning = _sanitize_stats(
        source.get("aligning_stats"),
        number_fields=_ALIGNING_NUMBERS,
        bool_fields=("resampled",),
    )
    if aligning:
        result["aligning_stats"] = aligning
    return result
