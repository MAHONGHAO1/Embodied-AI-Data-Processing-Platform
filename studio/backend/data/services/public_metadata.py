"""Explicit DTO serialization for user-visible task metadata."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from data.security.patterns import contains_cloud_access_key_value

_DROP = object()
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_HEX_64 = re.compile(r"^[0-9a-fA-F]{64}$")
_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def _safe_text(value: object) -> str | object:
    if not isinstance(value, str) or len(value) > 1000:
        return _DROP
    normalized = value.strip()
    lowered = normalized.lower()
    parts = PurePosixPath(normalized.replace("\\", "/")).parts
    if (
        not normalized
        or normalized.startswith(("/", "~", "\\"))
        or _WINDOWS_PATH.match(normalized)
        or ".." in parts
        or "://" in normalized
        or lowered.startswith(("jdbc:", "mongodb:", "postgres:", "postgresql:"))
        or contains_cloud_access_key_value(normalized)
        or any(marker in lowered for marker in ("access_key", "password=", "secret=", "token="))
    ):
        return _DROP
    return normalized


def _identifier(value: object) -> str | object:
    safe = _safe_text(value)
    return safe if safe is not _DROP and _IDENTIFIER.fullmatch(safe) else _DROP


def _number(value: object) -> int | float | object:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _DROP
    return value


def _bounded_progress(value: object) -> int | float | object:
    number = _number(value)
    return number if number is not _DROP and 0 <= number <= 100 else _DROP


def _number_or_identifier(value: object) -> int | float | str | object:
    number = _number(value)
    return _identifier(value) if number is _DROP else number


def _nonnegative_int(value: object) -> int | object:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else _DROP


def _boolean(value: object) -> bool | object:
    return value if isinstance(value, bool) else _DROP


def _timestamp(value: object) -> str | object:
    safe = _safe_text(value)
    if safe is _DROP:
        return _DROP
    try:
        datetime.fromisoformat(safe.removesuffix("Z") + ("+00:00" if safe.endswith("Z") else ""))
    except ValueError:
        return _DROP
    return safe


def _safe_storage_uri(value: object) -> str | object:
    if not isinstance(value, str):
        return _DROP
    parsed = urlsplit(value)
    path_parts = PurePosixPath(unquote(parsed.path)).parts
    lowered = unquote(value).lower()
    if (
        parsed.scheme not in {"nas", "oss"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(marker in lowered for marker in ("access_key=", "password=", "secret=", "token="))
        or ".." in path_parts
    ):
        return _DROP
    return value


def _allow_fields(
    value: object,
    validators: dict[str, Callable[[object], Any]],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, validator in validators.items():
        if key not in value:
            continue
        item = validator(value[key])
        if item is not _DROP and item not in ({}, []):
            result[key] = item
    return result


def _int_list(value: object) -> list[int] | object:
    if not isinstance(value, list) or len(value) > 1000:
        return _DROP
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        return _DROP
    return list(value)


def _hex_digest(value: object) -> str | object:
    return value if isinstance(value, str) and _HEX_64.fullmatch(value) else _DROP


def serialize_public_metadata(metadata: object) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    public: dict[str, Any] = {}
    scalar_fields = {
        "pipeline_key": _identifier,
        "scene": _safe_text,
        "remark": _safe_text,
    }
    for key, validator in scalar_fields.items():
        if key in metadata:
            value = validator(metadata[key])
            if value is not _DROP:
                public[key] = value

    preprocess = _allow_fields(
        metadata.get("preprocess"),
        {
            "status": _identifier,
            "progress": _bounded_progress,
            "message": _safe_text,
            "quality": _number_or_identifier,
            "score": _number,
            "started_at": _timestamp,
            "updated_at": _timestamp,
            "finished_at": _timestamp,
        },
    )
    quality_check = _allow_fields(
        metadata.get("quality_check"),
        {"level": _identifier, "score": _number},
    )
    ego = _allow_fields(
        metadata.get("ego"),
        {
            "raw_archive_uri": _safe_storage_uri,
            "source_backend": _identifier,
            "episode_count": _nonnegative_int,
            "candidate_ids": _int_list,
            "archive_sha256": _hex_digest,
            "operator": _safe_text,
        },
    )
    for key, value in (("preprocess", preprocess), ("quality_check", quality_check), ("ego", ego)):
        if value:
            public[key] = value
    return public


def _topic_name(value: object) -> str | object:
    if not isinstance(value, str) or not value or len(value) > 512:
        return _DROP
    normalized = value.strip()
    parts = PurePosixPath(normalized.replace("\\", "/")).parts
    lowered = normalized.lower()
    if (
        not normalized
        or ".." in parts
        or "://" in normalized
        or contains_cloud_access_key_value(normalized)
        or any(marker in lowered for marker in ("access_key", "password=", "secret=", "token="))
    ):
        return _DROP
    return normalized


def _string_list(
    value: object, validator: Callable[[object], Any], *, limit: int = 1000
) -> list[str] | object:
    if not isinstance(value, list) or len(value) > limit:
        return _DROP
    result: list[str] = []
    for item in value:
        safe = validator(item)
        if safe is not _DROP:
            result.append(safe)
    return result


def serialize_public_qrdf_episodes(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    episodes: list[dict[str, Any]] = []
    for item in value[:1000]:
        if not isinstance(item, dict):
            continue
        episode = _allow_fields(
            item,
            {
                "episode_id": _identifier,
                "duration_sec": _number,
                "frame_count": _nonnegative_int,
                "topics": lambda topics: _string_list(topics, _topic_name),
                "rgb_topics": lambda topics: _string_list(topics, _topic_name),
            },
        )
        topic_stats = item.get("topic_stats")
        if isinstance(topic_stats, dict):
            safe_stats = {}
            for name, count in list(topic_stats.items())[:1000]:
                safe_name = _topic_name(name)
                safe_count = _nonnegative_int(count)
                if safe_name is not _DROP and safe_count is not _DROP:
                    safe_stats[safe_name] = safe_count
            if safe_stats:
                episode["topic_stats"] = safe_stats
        if episode:
            episodes.append(episode)
    return episodes


def serialize_public_qrdf_topics(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    topics: list[dict[str, Any]] = []
    for item in value[:2000]:
        if not isinstance(item, dict):
            continue
        topic = _allow_fields(
            item,
            {
                "name": _topic_name,
                "schema": _identifier,
                "frame_count": _nonnegative_int,
                "message_count": _nonnegative_int,
            },
        )
        if topic:
            topics.append(topic)
    return topics


def serialize_public_qrdf_summary(value: object) -> dict[str, Any]:
    return _allow_fields(
        value,
        {
            "dataset_name": _safe_text,
            "episode_count": _nonnegative_int,
            "qrdf_version": _identifier,
            "total_duration_sec": _number,
            "layout": _identifier,
            "domains": lambda items: _string_list(items, _safe_text, limit=100),
            "tasks": lambda items: _string_list(items, _safe_text, limit=100),
            "robots": lambda items: _string_list(items, _safe_text, limit=100),
        },
    )


def serialize_public_qrdf_quality(value: object) -> dict[str, Any]:
    return _allow_fields(
        value,
        {
            "level": _identifier,
            "ok": _boolean,
            "score": _number,
            "episode_count": _nonnegative_int,
        },
    )


def _topic_value_map(value: object, value_validator: Callable[[object], Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for name, item in list(value.items())[:2000]:
        safe_name = _topic_name(name)
        safe_item = value_validator(item)
        if safe_name is not _DROP and safe_item is not _DROP:
            result[safe_name] = safe_item
    return result


def _public_episode_timing(value: object) -> dict[str, Any]:
    return _allow_fields(
        value,
        {
            "duration_s": _number,
            "start_timestamp_ns": _number,
            "end_timestamp_ns": _number,
        },
    )


def _public_episode_metadata(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    public = _allow_fields(
        value,
        {
            "qrdf_version": _identifier,
            "episode_id": _identifier,
            "collection_task_id": _safe_text,
            "data_file": _identifier,
            "recording_status": _identifier,
            "integrity_status": _identifier,
        },
    )
    integrity_issues = _string_list(value.get("integrity_issues"), _safe_text, limit=100)
    task = _allow_fields(value.get("task"), {"name": _safe_text, "language": _safe_text})
    operator = _allow_fields(value.get("operator"), {"type": _identifier, "id": _safe_text})
    timing = _public_episode_timing(value.get("timing"))
    topics = serialize_public_qrdf_topics(value.get("topics"))
    annotation = _allow_fields(value.get("annotation"), {"language": _safe_text})
    capture = _allow_fields(
        value.get("capture"),
        {
            "mode": _identifier,
            "episode_type": _identifier,
            "app": _safe_text,
            "app_version": _identifier,
        },
    )
    for key, item in (
        ("integrity_issues", integrity_issues),
        ("task", task),
        ("operator", operator),
        ("timing", timing),
        ("topics", topics),
        ("annotation", annotation),
        ("capture", capture),
    ):
        if item is not _DROP and item:
            public[key] = item
    return public


def _public_episode_metrics(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    public = _allow_fields(
        value,
        {
            "duration_s": _number,
            "max_alignment_delta_ms": _number,
            "valid_for_training": _boolean,
            "episode_type": _identifier,
            "pose_tracking_warnings": _nonnegative_int,
            "valid_for_ego_export": _boolean,
        },
    )
    for key, validator in (
        ("message_count", _nonnegative_int),
        ("frequency_hz", _number),
        ("dropped_frames", _nonnegative_int),
    ):
        values = _topic_value_map(value.get(key), validator)
        if values:
            public[key] = values
    return public


def serialize_public_qrdf_episode_detail(value: object) -> dict[str, Any]:
    """Return a bounded Episode DTO without arbitrary metadata or metrics fields."""
    if not isinstance(value, dict):
        return {}
    public = _allow_fields(
        value,
        {
            "ok": _boolean,
            "episode_id": _identifier,
            "duration_sec": _number,
            "frame_count": _nonnegative_int,
            "topics": lambda topics: _string_list(topics, _topic_name),
            "preview_topic": _topic_name,
        },
    )
    topic_stats = _topic_value_map(value.get("topic_stats"), _nonnegative_int)
    timing = _public_episode_timing(value.get("timing"))
    metadata = _public_episode_metadata(value.get("metadata"))
    metrics = _public_episode_metrics(value.get("metrics"))
    for key, item in (
        ("topic_stats", topic_stats),
        ("timing", timing),
        ("metadata", metadata),
        ("metrics", metrics),
    ):
        if item:
            public[key] = item
    return public


def serialize_public_review_summary(value: object) -> dict[str, Any]:
    return _allow_fields(
        value, {"operator": _safe_text, "reason": _safe_text, "reviewed_at": _timestamp}
    )


def serialize_public_packet_metadata(metadata: object) -> dict[str, Any]:
    """Bounded Episode packet metadata for console display under artifacts.

    Only fields already persisted by import/quality projections are accepted.
    Paths, storage URIs, credentials, and arbitrary nested JSON are dropped.
    """
    if not isinstance(metadata, dict):
        return {}
    source = metadata.get("source") if isinstance(metadata.get("source"), dict) else {}
    public = _allow_fields(
        source,
        {
            "qrdf_version": _identifier,
            "episode_id": _identifier,
            "data_file": _identifier,
        },
    )
    task = _allow_fields(source.get("task"), {"name": _safe_text, "language": _safe_text})
    robot = _allow_fields(
        source.get("robot"),
        {
            "name": _safe_text,
            "type": _identifier,
            "num_arms": _nonnegative_int,
            "base_frame": _identifier,
        },
    )
    capture = _allow_fields(
        source.get("capture"),
        {
            "mode": _identifier,
            "episode_type": _identifier,
            "app": _safe_text,
            "app_version": _identifier,
        },
    )
    timing = _public_episode_timing(metadata.get("timing"))
    # Quality timing stores nanosecond bounds as decimal strings; accept those.
    if isinstance(metadata.get("timing"), dict):
        for key in ("start_timestamp_ns", "end_timestamp_ns"):
            raw = metadata["timing"].get(key)
            if key not in timing and isinstance(raw, str) and raw.isdigit() and len(raw) <= 20:
                timing[key] = raw
    metrics = _allow_fields(
        metadata.get("metrics") if isinstance(metadata.get("metrics"), dict) else {},
        {
            "reference_frame_count": _nonnegative_int,
            "duration_s": _number,
            "average_rgb_rate_hz": _number,
        },
    )
    reference_topic = _topic_name(metadata.get("reference_topic"))
    multimodal = metadata.get("multimodal") if isinstance(metadata.get("multimodal"), dict) else {}
    cameras: list[dict[str, Any]] = []
    sensors: list[dict[str, Any]] = []
    streams = multimodal.get("streams") if isinstance(multimodal.get("streams"), list) else []
    for item in streams[:32]:
        if not isinstance(item, dict):
            continue
        topic = _topic_name(item.get("id") or item.get("topic"))
        if topic is _DROP:
            continue
        camera = {"topic": topic}
        for key, validator in (
            ("width", _nonnegative_int),
            ("height", _nonnegative_int),
            ("fps", _number),
        ):
            value = validator(item.get(key))
            if value is not _DROP:
                camera[key] = value
        cameras.append(camera)
    timeseries = (
        multimodal.get("timeseries") if isinstance(multimodal.get("timeseries"), list) else []
    )
    for item in timeseries[:32]:
        if not isinstance(item, dict):
            continue
        topic = _topic_name(item.get("id") or item.get("topic"))
        if topic is _DROP:
            continue
        sensor = {"topic": topic}
        frequency = _number(item.get("frequency_hz"))
        if frequency is not _DROP:
            sensor["frequency_hz"] = frequency
        sensors.append(sensor)
    for key, item in (
        ("task", task),
        ("robot", robot),
        ("capture", capture),
        ("timing", timing),
        ("metrics", metrics),
        ("cameras", cameras),
        ("sensors", sensors),
    ):
        if item:
            public[key] = item
    if reference_topic is not _DROP:
        public["reference_topic"] = reference_topic
    return public


def serialize_public_qrdf_metadata(metadata: object) -> dict[str, Any]:
    """Explicit public DTO for QRDF metadata_json, rejecting storage paths and credential fields."""
    if not isinstance(metadata, dict):
        return {}
    public = serialize_public_metadata(metadata)
    episodes = serialize_public_qrdf_episodes(
        metadata.get("episodes") or (metadata.get("preprocess") or {}).get("episodes")
    )
    topics = serialize_public_qrdf_topics(metadata.get("topics"))
    tags = _string_list(metadata.get("tags"), _safe_text, limit=200)
    if episodes:
        public["episodes"] = episodes
    if topics:
        public["topics"] = topics
    if tags is not _DROP and tags:
        public["tags"] = tags
    return public


def serialize_public_qrdf_detail(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    public: dict[str, Any] = {}
    for key in ("qrdf_id", "task_id"):
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool):
            public[key] = item
    summary = serialize_public_qrdf_summary(value.get("sdk_summary"))
    quality = serialize_public_qrdf_quality(value.get("quality_check"))
    episodes = serialize_public_qrdf_episodes(value.get("episodes"))
    topics = serialize_public_qrdf_topics(value.get("topics"))
    metadata = serialize_public_qrdf_metadata(value.get("metadata"))
    tags = _string_list(value.get("tags"), _safe_text, limit=200)
    for key, item in (
        ("sdk_summary", summary),
        ("quality_check", quality),
        ("episodes", episodes),
        ("topics", topics),
        ("metadata", metadata),
    ):
        if item:
            public[key] = item
    if tags is not _DROP and tags:
        public["tags"] = tags
    return public
