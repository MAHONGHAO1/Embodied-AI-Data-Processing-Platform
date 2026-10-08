"""Split ROS2 MCAP into episode-level mcap+yaml according to atom_segments.json."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from mcap.reader import make_reader
from mcap.writer import CompressionType, Writer

PREVIEW_CAMERA_TOPICS: dict[str, str] = {
    "top": "/camera/rs2_head/color/image_raw",
    "front": "/camera/rs2_head/color/image_raw",
    "head": "/camera/rs2_head/color/image_raw",
}


@dataclass(frozen=True)
class AtomSegmentManifest:
    version: str
    source_root: str
    preview_camera: str
    fps: float
    frame_count: int
    episodes: list[dict[str, Any]]

    @classmethod
    def load(cls, path: Path) -> AtomSegmentManifest:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            version=data.get("version", ""),
            source_root=data.get("source_root", ""),
            preview_camera=data.get("preview_camera", "top"),
            fps=float(data.get("fps", 15)),
            frame_count=int(data.get("frame_count", 0)),
            episodes=list(data.get("episodes") or []),
        )


@dataclass(frozen=True)
class EpisodeTimeRange:
    episode_index: int
    start_frame: int
    end_frame: int
    num_frames: int
    start_time_ns: int
    end_time_ns: int
    source_mcaps: tuple[Path, ...]


def load_manifest(path: Path) -> AtomSegmentManifest:
    if not path.is_file():
        raise FileNotFoundError(f"atom_segments.json 不存在: {path}")
    return AtomSegmentManifest.load(path)


def preview_camera_topic(manifest: AtomSegmentManifest) -> str:
    topic = PREVIEW_CAMERA_TOPICS.get(manifest.preview_camera)
    if not topic:
        raise ValueError(f"不支持的 preview_camera: {manifest.preview_camera}")
    return topic


def list_source_mcaps(source_dir: Path) -> list[Path]:
    if not source_dir.is_dir():
        raise FileNotFoundError(f"源数据目录不存在: {source_dir}")
    mcaps = sorted(source_dir.glob("*.mcap"))
    if not mcaps:
        raise FileNotFoundError(f"目录中未找到 MCAP: {source_dir}")
    return mcaps


def collect_preview_frame_timestamps(source_dir: Path, camera_topic: str) -> list[int]:
    timestamps: list[int] = []
    for mcap_path in list_source_mcaps(source_dir):
        with mcap_path.open("rb") as handle:
            reader = make_reader(handle)
            for _schema, _channel, record in reader.iter_messages(topics=[camera_topic]):
                timestamps.append(int(record.log_time))
    return timestamps


def mcap_time_bounds(mcap_path: Path) -> tuple[int, int]:
    yaml_path = mcap_path.with_suffix(".yaml")
    if yaml_path.is_file():
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        info = data.get("rosbag2_bagfile_information") or {}
        start_ns = int((info.get("starting_time") or {}).get("nanoseconds_since_epoch") or 0)
        duration_ns = int((info.get("duration") or {}).get("nanoseconds") or 0)
        if start_ns and duration_ns:
            return start_ns, start_ns + duration_ns

    first_ts: int | None = None
    last_ts: int | None = None
    with mcap_path.open("rb") as handle:
        reader = make_reader(handle)
        for _schema, _channel, record in reader.iter_messages():
            ts = int(record.log_time)
            if first_ts is None:
                first_ts = ts
            last_ts = ts
    if first_ts is None or last_ts is None:
        raise RuntimeError(f"MCAP 无消息: {mcap_path}")
    return first_ts, last_ts


def resolve_episode_time_ranges(
    source_dir: Path,
    manifest: AtomSegmentManifest,
) -> list[EpisodeTimeRange]:
    camera_topic = preview_camera_topic(manifest)
    frame_timestamps = collect_preview_frame_timestamps(source_dir, camera_topic)
    if not frame_timestamps:
        raise RuntimeError(f"未在 {source_dir} 中找到预览相机帧: {camera_topic}")

    usable_frames = min(len(frame_timestamps), manifest.frame_count or len(frame_timestamps))
    if usable_frames < len(frame_timestamps):
        frame_timestamps = frame_timestamps[:usable_frames]

    source_mcaps = list_source_mcaps(source_dir)
    mcap_bounds = {path: mcap_time_bounds(path) for path in source_mcaps}

    ranges: list[EpisodeTimeRange] = []
    for episode in manifest.episodes:
        episode_index = int(episode["episode_index"])
        start_frame = int(episode["start_frame"])
        end_frame = int(episode["end_frame"])
        if start_frame < 0 or end_frame >= len(frame_timestamps):
            raise ValueError(
                f"episode {episode_index} 帧范围越界: "
                f"{start_frame}-{end_frame}, 可用帧数={len(frame_timestamps)}"
            )

        start_time_ns = frame_timestamps[start_frame]
        end_time_ns = frame_timestamps[end_frame]
        overlapping = tuple(
            path
            for path in source_mcaps
            if not (mcap_bounds[path][1] < start_time_ns or mcap_bounds[path][0] > end_time_ns)
        )
        if not overlapping:
            raise RuntimeError(
                f"episode {episode_index} 未匹配到源 MCAP: {start_time_ns}-{end_time_ns}"
            )

        ranges.append(
            EpisodeTimeRange(
                episode_index=episode_index,
                start_frame=start_frame,
                end_frame=end_frame,
                num_frames=int(episode.get("num_frames") or (end_frame - start_frame + 1)),
                start_time_ns=start_time_ns,
                end_time_ns=end_time_ns,
                source_mcaps=overlapping,
            )
        )
    return ranges


def episode_output_stem(folder_name: str, episode_index: int) -> str:
    return f"{folder_name}_ep{episode_index:03d}"


def _write_filtered_mcap(
    source_mcaps: list[Path],
    output_mcap: Path,
    *,
    start_time_ns: int,
    end_time_ns: int,
) -> dict[str, Any]:
    output_mcap.parent.mkdir(parents=True, exist_ok=True)
    if output_mcap.exists():
        output_mcap.unlink()

    schema_ids: dict[tuple[str, str, bytes], int] = {}
    channel_ids: dict[tuple[int, str], int] = {}
    topic_counts: dict[str, int] = {}
    message_count = 0
    first_ts: int | None = None
    last_ts: int | None = None

    with output_mcap.open("wb") as out_handle:
        writer = Writer(out_handle, compression=CompressionType.ZSTD)
        writer.start(profile="ros2")

        for source_mcap in source_mcaps:
            with source_mcap.open("rb") as in_handle:
                reader = make_reader(in_handle)
                for schema, channel, record in reader.iter_messages():
                    log_time = int(record.log_time)
                    if log_time < start_time_ns or log_time > end_time_ns:
                        continue

                    schema_key = (
                        schema.name if schema else "",
                        schema.encoding if schema else "",
                        schema.data if schema else b"",
                    )
                    if schema_key not in schema_ids:
                        schema_ids[schema_key] = writer.register_schema(
                            schema.name if schema else "",
                            schema.encoding if schema else "",
                            schema.data if schema else b"",
                        )

                    channel_key = (schema_ids[schema_key], channel.topic)
                    if channel_key not in channel_ids:
                        channel_ids[channel_key] = writer.register_channel(
                            channel.topic,
                            channel.message_encoding,
                            schema_ids[schema_key],
                            channel.metadata or {},
                        )

                    channel_id = channel_ids[channel_key]
                    publish_time = int(record.publish_time)
                    writer.add_message(
                        channel_id,
                        log_time,
                        record.data,
                        publish_time,
                    )
                    topic_counts[channel.topic] = topic_counts.get(channel.topic, 0) + 1
                    message_count += 1
                    if first_ts is None:
                        first_ts = log_time
                    last_ts = log_time

        writer.finish()

    if message_count == 0:
        raise RuntimeError(f"时间范围内无消息: {start_time_ns}-{end_time_ns}")

    return {
        "message_count": message_count,
        "topic_counts": topic_counts,
        "starting_time_ns": first_ts,
        "duration_ns": (last_ts or first_ts or 0) - (first_ts or 0),
        "output_mcap": str(output_mcap),
    }


def _build_topics_with_message_count(
    template_topics: list[dict[str, Any]] | None,
    topic_counts: dict[str, int],
) -> list[dict[str, Any]]:
    if not template_topics:
        return [
            {
                "topic_metadata": {
                    "name": topic,
                    "type": "unknown",
                    "serialization_format": "cdr",
                    "offered_qos_profiles": "",
                },
                "message_count": count,
            }
            for topic, count in sorted(topic_counts.items())
        ]

    rebuilt: list[dict[str, Any]] = []
    for item in template_topics:
        copied = copy.deepcopy(item)
        topic_name = ((copied.get("topic_metadata") or {}).get("name")) or ""
        copied["message_count"] = topic_counts.get(topic_name, 0)
        rebuilt.append(copied)
    return rebuilt


def write_episode_yaml(
    *,
    output_yaml: Path,
    output_mcap_name: str,
    split_stats: dict[str, Any],
    template_yaml: Path | None,
) -> None:
    template_data: dict[str, Any] = {}
    if template_yaml and template_yaml.is_file():
        template_data = yaml.safe_load(template_yaml.read_text(encoding="utf-8")) or {}

    info = copy.deepcopy(template_data.get("rosbag2_bagfile_information") or {})
    info["version"] = info.get("version", 5)
    info["storage_identifier"] = info.get("storage_identifier", "mcap")
    info["duration"] = {"nanoseconds": int(split_stats["duration_ns"])}
    info["starting_time"] = {
        "nanoseconds_since_epoch": int(split_stats["starting_time_ns"]),
    }
    info["message_count"] = int(split_stats["message_count"])
    info["topics_with_message_count"] = _build_topics_with_message_count(
        info.get("topics_with_message_count"),
        split_stats["topic_counts"],
    )
    info["relative_file_paths"] = [output_mcap_name]
    info["files"] = [
        {
            "path": output_mcap_name,
            "starting_time": {
                "nanoseconds_since_epoch": int(split_stats["starting_time_ns"]),
            },
            "duration": {"nanoseconds": int(split_stats["duration_ns"])},
            "message_count": int(split_stats["message_count"]),
        }
    ]

    output_yaml.parent.mkdir(parents=True, exist_ok=True)
    output_yaml.write_text(
        yaml.safe_dump(
            {"rosbag2_bagfile_information": info},
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )


def split_episode_mcap(
    *,
    source_dir: Path,
    output_dir: Path,
    folder_name: str,
    episode_range: EpisodeTimeRange,
) -> dict[str, Any]:
    stem = episode_output_stem(folder_name, episode_range.episode_index)
    output_mcap = output_dir / f"{stem}.mcap"
    output_yaml = output_dir / f"{stem}.yaml"
    template_yaml = episode_range.source_mcaps[0].with_suffix(".yaml")

    split_stats = _write_filtered_mcap(
        list(episode_range.source_mcaps),
        output_mcap,
        start_time_ns=episode_range.start_time_ns,
        end_time_ns=episode_range.end_time_ns,
    )
    write_episode_yaml(
        output_yaml=output_yaml,
        output_mcap_name=output_mcap.name,
        split_stats=split_stats,
        template_yaml=template_yaml if template_yaml.is_file() else None,
    )

    return {
        "episode_index": episode_range.episode_index,
        "start_frame": episode_range.start_frame,
        "end_frame": episode_range.end_frame,
        "num_frames": episode_range.num_frames,
        "start_time_ns": episode_range.start_time_ns,
        "end_time_ns": episode_range.end_time_ns,
        "source_mcaps": [str(path) for path in episode_range.source_mcaps],
        "mcap_path": str(output_mcap),
        "yaml_path": str(output_yaml),
        "message_count": split_stats["message_count"],
        "duration_ns": split_stats["duration_ns"],
    }
