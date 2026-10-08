"""QRDF episode preview video export (H.264 faststart, full frames, native frame rate, no frame dropping)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from math import isfinite
from pathlib import Path

from qrdf.reader.reader import QRDFReader
from qrdf.registry.topics import CAMERA_FRONT_RGB
from qrdf.schema.qrdf.v0 import camera_pb2
from qrdf.utils.image import camera_frame_to_rgb_array

from data.integrations.qrdf.episode_metrics import (
    infer_camera_fps_from_episode,
    resolve_dataset_align_fps,
)
from data.integrations.qrdf.paths import resolve_legacy_qrdf_preview_file


@dataclass(frozen=True)
class EpisodePreviewFacts:
    """One encoded preview and the source frame timestamps it represents."""

    path: Path
    camera_topic: str
    encoded_fps: float
    frame_timestamps_ns: tuple[int, ...]


@dataclass(frozen=True)
class EpisodePreviewTimelineFacts:
    """Exact source timeline for an already encoded full-frame preview."""

    camera_topic: str
    encoded_fps: float
    frame_timestamps_ns: tuple[int, ...]


@dataclass(frozen=True)
class PreviewSegmentSpec:
    """One half-open source range to encode from an already-produced MP4."""

    episode_id: int
    start_ns: int
    end_ns: int
    output_path: Path


@dataclass(frozen=True)
class PreviewSegmentFacts:
    """The exact parent-preview timestamps written to one child preview."""

    episode_id: int
    path: Path
    frame_timestamps_ns: tuple[int, ...]


def _pick_camera_topic(episode) -> str | None:
    from data.integrations.qrdf.episode_metrics import pick_camera_topic_from_names

    metadata_topics = episode.metadata.topics if episode.metadata else []
    topics = [entry.name for entry in metadata_topics if entry.name]
    picked = pick_camera_topic_from_names(topics)
    if picked:
        return picked
    cameras = episode.metadata.sensors.cameras if episode.metadata else []
    if cameras:
        return cameras[0].topic

    discovered_topics: set[str] = set()
    for mcap_msg in episode.mcap_reader.iter_all_messages_streaming():
        if isinstance(mcap_msg.message, camera_pb2.CameraFrame) and mcap_msg.message.data:
            discovered_topics.add(mcap_msg.topic)
            if mcap_msg.topic == CAMERA_FRONT_RGB:
                return CAMERA_FRONT_RGB
    return pick_camera_topic_from_names(sorted(discovered_topics))


def infer_camera_fps(episode, camera_topic: str) -> float:
    """Infer frame rate for preview/alignment from episode (prioritizing native / effective snapshot)."""
    return infer_camera_fps_from_episode(episode, camera_topic)


def infer_dataset_fps(storage_path: str | Path) -> float:
    """Infer frame rate for QRDF dataset used for export/alignment."""
    return resolve_dataset_align_fps(storage_path)


def _iter_camera_frames(
    episode,
    camera_topic: str,
    *,
    start_ns: int | None,
    end_ns: int | None,
) -> Iterator[tuple[object, int]]:
    """Decode one camera frame at a time in an optional half-open source range."""
    for mcap_msg in episode.mcap_reader.iter_messages_streaming(camera_topic):
        timestamp_ns = int(mcap_msg.log_time)
        if start_ns is not None and timestamp_ns < start_ns:
            continue
        if end_ns is not None and timestamp_ns >= end_ns:
            continue
        msg = mcap_msg.message
        if not isinstance(msg, camera_pb2.CameraFrame) or not msg.data:
            continue
        yield camera_frame_to_rgb_array(msg), timestamp_ns


def _optional_interval(start_ns: int | None, end_ns: int | None) -> tuple[int | None, int | None]:
    if start_ns is None and end_ns is None:
        return None, None
    if type(start_ns) is not int or type(end_ns) is not int or start_ns < 0 or start_ns >= end_ns:
        raise ValueError("preview source interval is invalid")
    return start_ns, end_ns


def write_browser_mp4(
    frame_factory: Callable[[], Iterator[tuple[object, int]]],
    output_path: Path,
    *,
    fps: float,
) -> tuple[Path, tuple[int, ...]]:
    """Write H.264 MP4 with faststart frame-by-frame; prioritize NVENC hardware encoding."""
    import imageio.v2 as iio

    from data.integrations.qrdf.gpu_utils import imageio_ffmpeg_nvenc_available

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if fps <= 0:
        raise ValueError(f"无效帧率: {fps}")

    tmp_path = output_path.with_suffix(".tmp.mp4")
    if tmp_path.exists():
        tmp_path.unlink()

    codec = "libx264"
    ffmpeg_params = ["-movflags", "+faststart", "-pix_fmt", "yuv420p"]

    def _encode(selected_codec: str, params: list[str]) -> tuple[int, ...]:
        timestamps: list[int] = []
        with iio.get_writer(
            str(tmp_path),
            fps=fps,
            codec=selected_codec,
            ffmpeg_params=params,
        ) as writer:
            for frame, timestamp_ns in frame_factory():
                writer.append_data(frame)
                timestamps.append(timestamp_ns)
        if not timestamps:
            raise ValueError("无可用相机帧，无法生成预览")
        return tuple(timestamps)

    try:
        if imageio_ffmpeg_nvenc_available():
            try:
                timestamps = _encode(
                    "h264_nvenc",
                    ["-movflags", "+faststart", "-pix_fmt", "yuv420p", "-preset", "p4"],
                )
            except (OSError, RuntimeError):
                if tmp_path.exists():
                    tmp_path.unlink()
                timestamps = _encode(codec, ffmpeg_params)
        else:
            timestamps = _encode(codec, ffmpeg_params)
        tmp_path.replace(output_path)
    except BaseException:
        if tmp_path.exists():
            tmp_path.unlink()
        raise

    return output_path, timestamps


def export_preview_segments_from_mp4(
    source_path: Path,
    *,
    source_timestamps_ns: tuple[int, ...],
    segments: tuple[PreviewSegmentSpec, ...],
    fps: float,
) -> tuple[PreviewSegmentFacts, ...]:
    """Decode one parent MP4 pass and atomically produce all requested segments."""
    import imageio.v2 as iio

    normalized_segments = _validated_preview_segments(segments)
    normalized_timestamps = _validated_preview_timestamps(source_timestamps_ns)
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not isfinite(fps) or fps <= 0:
        raise ValueError("preview segment fps is invalid")

    source_path = Path(source_path)
    temporary_paths = tuple(
        segment.output_path.with_suffix(".tmp.mp4") for segment in normalized_segments
    )
    for segment, temporary_path in zip(normalized_segments, temporary_paths, strict=True):
        segment.output_path.parent.mkdir(parents=True, exist_ok=True)
        if temporary_path.exists():
            temporary_path.unlink()

    reader = iio.get_reader(str(source_path))
    active_writer: object | None = None
    active_timestamps: list[int] = []
    active_segment_index: int | None = None
    facts: list[PreviewSegmentFacts] = []
    frame_count = 0

    def close_active_writer(*, error: BaseException | None = None) -> None:
        nonlocal active_writer
        writer = active_writer
        active_writer = None
        if writer is not None:
            _close_media_resource(writer, error=error)

    def finish_active_segment() -> None:
        nonlocal active_segment_index, active_timestamps
        if active_segment_index is None or not active_timestamps:
            raise ValueError("preview segment contains no source frame")
        segment = normalized_segments[active_segment_index]
        temporary_path = temporary_paths[active_segment_index]
        close_active_writer()
        temporary_path.replace(segment.output_path)
        facts.append(
            PreviewSegmentFacts(
                episode_id=segment.episode_id,
                path=segment.output_path,
                frame_timestamps_ns=tuple(active_timestamps),
            )
        )
        active_segment_index = None
        active_timestamps = []

    try:
        segment_index = 0
        for frame in reader:
            if frame_count >= len(normalized_timestamps):
                raise ValueError("preview source frame count does not match timestamp axis")
            timestamp_ns = normalized_timestamps[frame_count]
            frame_count += 1

            while (
                segment_index < len(normalized_segments)
                and timestamp_ns >= normalized_segments[segment_index].end_ns
            ):
                if active_segment_index != segment_index:
                    raise ValueError("preview segment contains no source frame")
                finish_active_segment()
                segment_index += 1

            if segment_index >= len(normalized_segments):
                continue
            segment = normalized_segments[segment_index]
            if timestamp_ns < segment.start_ns:
                continue

            if active_segment_index is None:
                temporary_path = temporary_paths[segment_index]
                active_writer = iio.get_writer(
                    str(temporary_path),
                    fps=float(fps),
                    codec="libx264",
                    ffmpeg_params=["-movflags", "+faststart", "-pix_fmt", "yuv420p"],
                )
                active_segment_index = segment_index
            active_writer.append_data(frame)
            active_timestamps.append(timestamp_ns)

        if frame_count != len(normalized_timestamps):
            raise ValueError("preview source frame count does not match timestamp axis")
        while segment_index < len(normalized_segments):
            if active_segment_index != segment_index:
                raise ValueError("preview segment contains no source frame")
            finish_active_segment()
            segment_index += 1
        return tuple(facts)
    except BaseException as exc:
        close_active_writer(error=exc)
        raise
    finally:
        cleanup_error: OSError | None = None
        for temporary_path in temporary_paths:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        try:
            _close_media_resource(reader, allow_missing=True)
        finally:
            if cleanup_error is not None:
                raise cleanup_error


def _validated_preview_segments(
    segments: tuple[PreviewSegmentSpec, ...],
) -> tuple[PreviewSegmentSpec, ...]:
    if not isinstance(segments, tuple) or not 1 <= len(segments) <= 1000:
        raise ValueError("preview segments must contain between 1 and 1000 items")
    normalized: list[PreviewSegmentSpec] = []
    episode_ids: set[int] = set()
    output_paths: set[Path] = set()
    previous_end_ns: int | None = None
    for segment in segments:
        if not isinstance(segment, PreviewSegmentSpec):
            raise ValueError("preview segment is invalid")
        if (
            isinstance(segment.episode_id, bool)
            or not isinstance(segment.episode_id, int)
            or segment.episode_id <= 0
            or isinstance(segment.start_ns, bool)
            or not isinstance(segment.start_ns, int)
            or isinstance(segment.end_ns, bool)
            or not isinstance(segment.end_ns, int)
            or segment.start_ns < 0
            or segment.start_ns >= segment.end_ns
            or not isinstance(segment.output_path, Path)
        ):
            raise ValueError("preview segment is invalid")
        output_path = segment.output_path.resolve(strict=False)
        if segment.episode_id in episode_ids or output_path in output_paths:
            raise ValueError("preview segments must have unique episode and output paths")
        if previous_end_ns is not None and segment.start_ns < previous_end_ns:
            raise ValueError("preview segments must be ordered and non-overlapping")
        normalized.append(segment)
        episode_ids.add(segment.episode_id)
        output_paths.add(output_path)
        previous_end_ns = segment.end_ns
    return tuple(normalized)


def _validated_preview_timestamps(source_timestamps_ns: tuple[int, ...]) -> tuple[int, ...]:
    if not isinstance(source_timestamps_ns, tuple) or not source_timestamps_ns:
        raise ValueError("preview source timestamp axis is invalid")
    previous_timestamp_ns: int | None = None
    for timestamp_ns in source_timestamps_ns:
        if (
            isinstance(timestamp_ns, bool)
            or not isinstance(timestamp_ns, int)
            or timestamp_ns < 0
            or (previous_timestamp_ns is not None and timestamp_ns <= previous_timestamp_ns)
        ):
            raise ValueError("preview source timestamp axis is invalid")
        previous_timestamp_ns = timestamp_ns
    return source_timestamps_ns


def _close_media_resource(
    resource: object,
    *,
    error: BaseException | None = None,
    allow_missing: bool = False,
) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        close()
        return
    exit_context = getattr(resource, "__exit__", None)
    if callable(exit_context):
        if error is None:
            exit_context(None, None, None)
        else:
            exit_context(type(error), error, error.__traceback__)
        return
    if not allow_missing:
        raise TypeError("preview media resource cannot be closed")


def export_episode_preview(
    episode_path: str | Path,
    output_path: str | Path | None = None,
    *,
    camera_topic: str | None = None,
    fps: float | None = None,
    start_ns: int | None = None,
    end_ns: int | None = None,
) -> Path:
    """Export a full-frame H.264 preview, optionally limited to one source range."""
    return export_episode_preview_facts(
        episode_path,
        output_path,
        camera_topic=camera_topic,
        fps=fps,
        start_ns=start_ns,
        end_ns=end_ns,
    ).path


def export_episode_preview_facts(
    episode_path: str | Path,
    output_path: str | Path | None = None,
    *,
    camera_topic: str | None = None,
    fps: float | None = None,
    start_ns: int | None = None,
    end_ns: int | None = None,
) -> EpisodePreviewFacts:
    """Export a preview together with its exact included camera timestamps."""
    from qrdf.reader.episode import Episode

    start_ns, end_ns = _optional_interval(start_ns, end_ns)
    episode_path = Path(episode_path)
    episode = Episode(episode_path)
    topic = camera_topic or _pick_camera_topic(episode)
    if not topic:
        raise ValueError(f"episode 无相机 topic: {episode_path}")

    if output_path is None:
        output_path = resolve_legacy_qrdf_preview_file(episode_path)
    else:
        output_path = Path(output_path)

    effective_fps = fps if fps is not None and fps > 0 else infer_camera_fps(episode, topic)

    def frame_factory() -> Iterator[tuple[object, int]]:
        yield from _iter_camera_frames(
            episode,
            topic,
            start_ns=start_ns,
            end_ns=end_ns,
        )

    path, timestamps = write_browser_mp4(frame_factory, output_path, fps=effective_fps)
    return EpisodePreviewFacts(
        path=path,
        camera_topic=topic,
        encoded_fps=float(effective_fps),
        frame_timestamps_ns=timestamps,
    )


def inspect_episode_preview_timeline(
    episode_path: str | Path,
    *,
    camera_topic: str | None = None,
    fps: float | None = None,
    start_ns: int | None = None,
    end_ns: int | None = None,
) -> EpisodePreviewTimelineFacts:
    """Inspect timestamps without decoding frames or rewriting an existing MP4."""
    from qrdf.reader.episode import Episode

    start_ns, end_ns = _optional_interval(start_ns, end_ns)
    episode = Episode(Path(episode_path))
    topic = camera_topic or _pick_camera_topic(episode)
    if not topic:
        raise ValueError(f"episode 无相机 topic: {episode_path}")
    effective_fps = fps if fps is not None and fps > 0 else infer_camera_fps(episode, topic)
    timestamps: list[int] = []
    for mcap_msg in episode.mcap_reader.iter_messages_streaming(topic):
        timestamp_ns = int(mcap_msg.log_time)
        if start_ns is not None and timestamp_ns < start_ns:
            continue
        if end_ns is not None and timestamp_ns >= end_ns:
            continue
        msg = mcap_msg.message
        if isinstance(msg, camera_pb2.CameraFrame) and msg.data:
            timestamps.append(timestamp_ns)
    return EpisodePreviewTimelineFacts(
        camera_topic=topic,
        encoded_fps=float(effective_fps),
        frame_timestamps_ns=tuple(timestamps),
    )


def export_dataset_preview(storage_path: str | Path) -> Path | None:
    """Generate / overwrite preview.mp4 for first episode of QRDF dataset (full frames, native frame rate)."""
    reader = QRDFReader(storage_path)
    episode_ids = reader.list_episodes()
    if not episode_ids:
        return None
    episode = reader.load_episode(episode_ids[0])
    return export_episode_preview(episode.path)


def mp4_is_browser_ready(path: Path) -> bool:
    """Check whether MP4 places moov before mdat (faststart)."""
    data = path.read_bytes()
    atom_order: list[str] = []
    offset = 0
    while offset + 8 <= len(data) and len(atom_order) < 8:
        size = int.from_bytes(data[offset : offset + 4], "big")
        if size < 8:
            break
        atom_order.append(data[offset + 4 : offset + 8].decode("latin1", errors="replace"))
        offset += size
    if "moov" not in atom_order or "mdat" not in atom_order:
        return False
    return atom_order.index("moov") < atom_order.index("mdat")
