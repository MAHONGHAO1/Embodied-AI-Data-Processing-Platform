"""Header-only timestamp indexes used by the EGO cut workflow.

This adapter deliberately uses the low-level MCAP reader instead of the QRDF
``McapEpisodeReader``.  The latter decodes protobuf messages and retains them
in memory, which is not acceptable on the API request path.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from mcap.reader import NonSeekingReader
from qrdf.models.episode import EpisodeMetadata
from qrdf.registry.topic_layout import resolve_reference_topic
from qrdf.registry.topics import ANNOTATION_EVENT, ANNOTATION_QUALITY, EPISODE_EVENT

from data.integrations.qrdf.paths import resolve_qrdf_episode_data_file


@dataclass(frozen=True)
class CutTopicRequirements:
    """Topics and their validation semantics for one source episode."""

    reference_topic: str
    dynamic_topics: frozenset[str]
    calibration_topics: frozenset[str]


@dataclass(frozen=True)
class CutTimestampScan:
    """Sorted, payload-free timestamp arrays collected from one MCAP."""

    requirements: CutTopicRequirements
    timestamps_by_topic: dict[str, tuple[int, ...]]


def required_cut_topics(
    metadata: EpisodeMetadata,
    available_topics: Iterable[str],
) -> CutTopicRequirements:
    """Return the topics required by the existing EGO cut semantics.

    ``available_topics`` is used only to resolve the same reference fallback
    as QRDF inspection. Missing declared topics remain in the requirement set
    so the caller can return the existing actionable missing-topic error.
    """

    available = frozenset(str(topic) for topic in available_topics)
    dynamic: set[str] = set()
    calibration: set[str] = set()
    declared_required = {entry.name for entry in metadata.topics if entry.required}

    for device in metadata.devices:
        for stream in device.streams:
            dynamic.add(stream.rgb_topic)
            calibration.add(stream.camera_info_topic)
            if stream.pose_topic:
                dynamic.add(stream.pose_topic)
            if stream.depth_topic and stream.depth_topic in declared_required:
                dynamic.add(stream.depth_topic)
                if stream.depth_camera_info_topic:
                    calibration.add(stream.depth_camera_info_topic)

    ignored_events = {EPISODE_EVENT, ANNOTATION_EVENT, ANNOTATION_QUALITY}
    calibration.update(topic for topic in declared_required if topic.endswith("camera_info"))
    dynamic.update(
        topic
        for topic in declared_required
        if topic not in calibration and topic not in ignored_events
    )

    reference_topic = resolve_reference_topic(metadata, sorted(available))
    if not reference_topic:
        raise ValueError("EGO RGB reference is unavailable for cutting")
    # A fallback reference can be present in MCAP without being declared in a
    # device stream. It still defines the legal cut bounds and must be present
    # in every dynamic interval.
    dynamic.add(reference_topic)
    return CutTopicRequirements(
        reference_topic=reference_topic,
        dynamic_topics=frozenset(dynamic),
        calibration_topics=frozenset(calibration),
    )


def scan_cut_topic_timestamps(
    episode_dir: Path | str,
    *,
    metadata: EpisodeMetadata,
) -> CutTimestampScan:
    """Collect required topic log times without decoding protobuf payloads."""

    episode_path = Path(episode_dir).resolve()
    data_file = resolve_qrdf_episode_data_file(metadata, episode_path).resolve()
    try:
        data_file.relative_to(episode_path)
    except ValueError as exc:
        raise ValueError("EGO MCAP is outside the episode directory") from exc
    if not data_file.is_file():
        raise ValueError("EGO raw MCAP is unavailable")

    available_topics = _scan_available_topics(data_file)
    requirements = required_cut_topics(metadata, available_topics)
    requested = (
        requirements.dynamic_topics
        | requirements.calibration_topics
        | {requirements.reference_topic}
    )
    timestamps: dict[str, list[int]] = {topic: [] for topic in requested}
    with data_file.open("rb") as source:
        reader = NonSeekingReader(source)
        for _schema, channel, record in reader.iter_messages(
            topics=sorted(requested),
            log_time_order=False,
        ):
            if channel.topic in timestamps:
                timestamps[channel.topic].append(int(record.log_time))

    return CutTimestampScan(
        requirements=requirements,
        timestamps_by_topic={
            topic: tuple(sorted(set(values))) for topic, values in timestamps.items()
        },
    )


def _scan_available_topics(data_file: Path) -> frozenset[str]:
    """Read channel names in one pass; MCAP records are never decoded."""

    topics: set[str] = set()
    with data_file.open("rb") as source:
        reader = NonSeekingReader(source)
        for _schema, channel, _record in reader.iter_messages(log_time_order=False):
            topics.add(channel.topic)
    return frozenset(topics)
