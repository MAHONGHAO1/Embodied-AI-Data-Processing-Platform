"""Episode frame rate statistics and metrics.json generation (without modifying vendor/qrdf)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from qrdf.metrics.compute import compute_episode_metrics
from qrdf.reader.episode import Episode
from qrdf.registry.topics import CAMERA_FRONT_RGB, EPISODE_EVENT

_STANDARD_FPS = (10.0, 12.0, 15.0, 20.0, 24.0, 25.0, 30.0, 50.0, 60.0, 100.0)
_FPS_SNAP_TOLERANCE = 0.08


def _pick_camera_topic(episode: Episode) -> str | None:
    topics = episode.list_topics()
    if CAMERA_FRONT_RGB in topics:
        return CAMERA_FRONT_RGB
    cameras = episode.metadata.sensors.cameras if episode.metadata else []
    if cameras:
        return cameras[0].topic
    for topic in topics:
        if topic.startswith("/camera/") and topic.endswith("/rgb"):
            return topic
    return None


def round_nominal_fps(fps: float) -> float:
    """Snap measured frame rate to common nominal values (when deviation is within tolerance)."""
    if fps <= 0:
        return 30.0
    best = min(_STANDARD_FPS, key=lambda s: abs(s - fps))
    if abs(best - fps) / best <= _FPS_SNAP_TOLERANCE:
        return best
    return round(fps, 3)


def measure_topic_span_fps(times: list[int]) -> float | None:
    """Calculate topic frame rate based on interval between first and last timestamps: (n-1)/span."""
    if len(times) < 2:
        return None
    span_ns = times[-1] - times[0]
    if span_ns <= 0:
        return None
    fps = (len(times) - 1) * 1e9 / span_ns
    if fps < 0.5 or fps > 200.0:
        return None
    return fps


def measure_episode_topic_frequencies(episode: Episode) -> dict[str, float]:
    """Estimate raw sampling rate per topic using message timestamp intervals."""
    out: dict[str, float] = {}
    for topic in episode.list_topics():
        if topic == EPISODE_EVENT:
            continue
        times = [int(m.log_time) for m in episode.iter_topic(topic)]
        fps = measure_topic_span_fps(times)
        if fps is not None:
            out[topic] = round(fps, 6)
    return out


def measure_episode_message_counts(episode: Episode) -> dict[str, int]:
    counts: dict[str, int] = {}
    for topic in episode.list_topics():
        if topic == EPISODE_EVENT:
            continue
        counts[topic] = episode.mcap_reader.message_count(topic)
    return counts


def load_metrics_json(episode_path: Path) -> dict[str, Any]:
    path = episode_path / "metrics.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_metrics_json(episode_path: Path, metrics: dict[str, Any]) -> None:
    path = episode_path / "metrics.json"
    path.write_text(json.dumps(metrics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def is_episode_aligned(episode_path: Path) -> bool:
    metrics = load_metrics_json(episode_path)
    return bool(metrics.get("aligned"))


def is_episode_timing_standardized(episode_path: Path) -> bool:
    metrics = load_metrics_json(episode_path)
    return bool(metrics.get("timing_standardized"))


def _declared_camera_fps(episode: Episode, camera_topic: str | None) -> float | None:
    if not camera_topic or not episode.metadata or not episode.metadata.sensors:
        return None
    for cam in episode.metadata.sensors.cameras:
        if cam.topic == camera_topic and cam.fps and cam.fps > 0:
            return float(cam.fps)
    return None


def _stored_align_fps(episode_path: Path, episode: Episode) -> float | None:
    metrics = load_metrics_json(episode_path)
    if metrics.get("align_fps"):
        return float(metrics["align_fps"])
    timing = episode.metadata.timing if episode.metadata else None
    if timing and timing.nominal_fps and timing.nominal_fps > 0:
        return float(timing.nominal_fps)
    return None


def resolve_episode_align_fps(
    episode_path: Path | str,
    *,
    camera_topic: str | None = None,
) -> float:
    """Resolve target frame rate for temporal alignment (prioritize aligned record, then measured camera fps)."""
    episode_path = Path(episode_path)
    episode = Episode(episode_path)
    topic = camera_topic or _pick_camera_topic(episode)

    stored = _stored_align_fps(episode_path, episode)
    if stored and is_episode_aligned(episode_path):
        return stored

    metrics = load_metrics_json(episode_path)
    native = metrics.get("native_frequency_hz") or {}
    if topic and topic in native and float(native[topic]) > 0:
        return round_nominal_fps(float(native[topic]))

    if topic:
        times = [int(m.log_time) for m in episode.iter_topic(topic)]
        measured = measure_topic_span_fps(times)
        if measured is not None:
            return round_nominal_fps(measured)

    declared = _declared_camera_fps(episode, topic)
    if declared:
        return round_nominal_fps(declared)

    return 30.0


def resolve_dataset_align_fps(storage_path: str | Path) -> float:
    from qrdf.reader.reader import QRDFReader

    reader = QRDFReader(storage_path)
    episode_ids = reader.list_episodes()
    if not episode_ids:
        return 30.0
    episode = reader.load_episode(episode_ids[0])
    return resolve_episode_align_fps(episode.path)


def _sync_metadata_sensor_fps(episode: Episode, native_hz: dict[str, float]) -> None:
    """Write measured frame rate back to metadata.sensors / topics (replacing recorder defaults)."""
    meta = episode.metadata
    if not meta:
        return
    if meta.sensors:
        for cam in meta.sensors.cameras:
            hz = native_hz.get(cam.topic)
            if hz:
                cam.fps = round_nominal_fps(hz)
        for lowdim in meta.sensors.lowdim:
            hz = native_hz.get(lowdim.topic)
            if hz:
                lowdim.frequency_hz = round_nominal_fps(hz)
    for entry in meta.topics:
        hz = native_hz.get(entry.name)
        if hz:
            entry.frequency_hz = round_nominal_fps(hz)


def _effective_frequency_hz(
    message_counts: dict[str, int],
    episode: Episode,
    *,
    align_fps: float | None = None,
) -> dict[str, float]:
    """Effective frame rate on training timeline after alignment (prioritize interval method, otherwise align_fps)."""
    out: dict[str, float] = {}
    for topic, count in message_counts.items():
        if topic == EPISODE_EVENT or count <= 0:
            continue
        times = [int(m.log_time) for m in episode.iter_topic(topic)]
        fps = measure_topic_span_fps(times)
        if fps is not None:
            out[topic] = round(fps, 6)
        elif align_fps and align_fps > 0:
            out[topic] = round(align_fps, 6)
    return out


def _alignment_dropped_frames(
    *,
    align_fps: float,
    duration_s: float,
    actual_count: int,
    total_frames: int,
    valid_frames: int,
) -> int:
    expected = int(round(align_fps * duration_s)) if align_fps > 0 and duration_s > 0 else 0
    grid_drop = max(0, total_frames - valid_frames)
    count_drop = max(0, expected - actual_count)
    return max(grid_drop, count_drop)


def refresh_native_metrics(
    episode_path: str | Path,
    *,
    timing_standardized: bool = False,
) -> dict[str, Any]:
    """After MCAP ingestion: compute raw topic frame rates and update metadata and metrics.json."""
    episode_path = Path(episode_path)
    episode = Episode(episode_path)

    native_hz = measure_episode_topic_frequencies(episode)
    counts = measure_episode_message_counts(episode)
    duration_s = float(episode.metadata.timing.duration_s if episode.metadata.timing else 0.0)

    camera_topic = _pick_camera_topic(episode)
    if camera_topic and camera_topic in native_hz:
        align_fps = round_nominal_fps(native_hz[camera_topic])
    else:
        align_fps = resolve_episode_align_fps(episode_path, camera_topic=camera_topic)

    _sync_metadata_sensor_fps(episode, native_hz)
    timing = episode.metadata.timing
    if timing:
        timing.nominal_fps = align_fps
    episode.metadata.save(episode_path / "metadata.json")

    metrics: dict[str, Any] = {
        "duration_s": duration_s,
        "message_count": counts,
        "frequency_hz": dict(native_hz),
        "native_frequency_hz": dict(native_hz),
        "effective_frequency_hz": dict(native_hz),
        "align_fps": align_fps,
        "aligned": False,
        "timing_standardized": timing_standardized,
        "resampled": False,
        "dropped_frames": {},
        "max_alignment_delta_ms": None,
        "valid_for_training": len(counts) > 0,
    }
    save_metrics_json(episode_path, metrics)
    return metrics


def write_aligned_episode_metrics(
    episode_path: Path,
    *,
    message_counts: dict[str, int],
    duration_s: float,
    align_fps: float,
    native_frequency_hz: dict[str, float],
    native_message_count: dict[str, int],
    valid_frames: int,
    total_frames: int,
    max_time_delta_ms: float = 100.0,
) -> dict[str, Any]:
    """Write full metrics after temporal alignment (preserving native snapshot prior to alignment)."""
    episode = Episode(episode_path)
    vendor = compute_episode_metrics(
        episode_path,
        episode.metadata,
        message_counts=message_counts,
        duration_s=duration_s,
        max_time_delta_ms=max_time_delta_ms,
        align_fps=align_fps,
    )

    effective_hz = _effective_frequency_hz(message_counts, episode, align_fps=align_fps)
    dropped: dict[str, int] = {}
    if episode.metadata and episode.metadata.sensors:
        for cam in episode.metadata.sensors.cameras:
            actual = message_counts.get(cam.topic, 0)
            dropped[cam.topic] = _alignment_dropped_frames(
                align_fps=align_fps,
                duration_s=duration_s,
                actual_count=actual,
                total_frames=total_frames,
                valid_frames=valid_frames,
            )

    timing = episode.metadata.timing
    if timing:
        timing.nominal_fps = align_fps
        episode.metadata.save(episode_path / "metadata.json")

    metrics: dict[str, Any] = {
        "duration_s": duration_s,
        "message_count": message_counts,
        "frequency_hz": dict(native_frequency_hz),
        "native_frequency_hz": dict(native_frequency_hz),
        "native_message_count": dict(native_message_count),
        "effective_frequency_hz": effective_hz,
        "align_fps": align_fps,
        "aligned": True,
        "alignment_valid_frames": valid_frames,
        "alignment_total_frames": total_frames,
        "dropped_frames": dropped,
        "max_alignment_delta_ms": vendor.max_alignment_delta_ms,
        "valid_for_training": vendor.valid_for_training,
    }
    save_metrics_json(episode_path, metrics)
    return metrics


def patch_metrics_after_timing_repair(episode_path: Path, duration_s: float) -> None:
    """Patch metrics after repair_episode_timing to prevent count/duration formulas from breaking native semantics."""
    metrics = load_metrics_json(episode_path)
    if not metrics:
        return

    metrics["duration_s"] = duration_s
    episode = Episode(episode_path)
    counts = metrics.get("message_count") or measure_episode_message_counts(episode)

    if metrics.get("aligned"):
        align_fps = float(metrics.get("align_fps") or resolve_episode_align_fps(episode_path))
        metrics["effective_frequency_hz"] = _effective_frequency_hz(
            counts, episode, align_fps=align_fps
        )
        native = metrics.get("native_frequency_hz") or {}
        metrics["frequency_hz"] = dict(native)
        valid_frames = int(metrics.get("alignment_valid_frames") or 0)
        total_frames = int(metrics.get("alignment_total_frames") or 0)
        dropped: dict[str, int] = {}
        if episode.metadata and episode.metadata.sensors:
            for cam in episode.metadata.sensors.cameras:
                actual = counts.get(cam.topic, 0)
                dropped[cam.topic] = _alignment_dropped_frames(
                    align_fps=align_fps,
                    duration_s=duration_s,
                    actual_count=actual,
                    total_frames=total_frames,
                    valid_frames=valid_frames,
                )
        metrics["dropped_frames"] = dropped
    else:
        native_hz = measure_episode_topic_frequencies(episode)
        metrics["frequency_hz"] = native_hz
        metrics["native_frequency_hz"] = native_hz
        metrics["effective_frequency_hz"] = dict(native_hz)
        metrics["message_count"] = counts

    save_metrics_json(episode_path, metrics)


def infer_camera_fps_from_episode(episode, camera_topic: str) -> float:
    """For preview/export: use effective fps after alignment, otherwise use native / interval measured fps."""
    metrics = load_metrics_json(episode.path)
    if metrics.get("aligned"):
        eff = (metrics.get("effective_frequency_hz") or {}).get(camera_topic)
        if eff and float(eff) > 0:
            return float(eff)
        align_fps = metrics.get("align_fps")
        if align_fps and float(align_fps) > 0:
            return float(align_fps)

    native = (metrics.get("native_frequency_hz") or metrics.get("frequency_hz") or {}).get(
        camera_topic
    )
    if native and float(native) > 0:
        return float(native)

    first_timestamp_ns: int | None = None
    last_timestamp_ns: int | None = None
    message_count = 0
    for message in episode.mcap_reader.iter_messages_streaming(camera_topic):
        timestamp_ns = int(message.log_time)
        if first_timestamp_ns is None:
            first_timestamp_ns = timestamp_ns
        last_timestamp_ns = timestamp_ns
        message_count += 1
    if (
        message_count >= 2
        and first_timestamp_ns is not None
        and last_timestamp_ns is not None
        and last_timestamp_ns > first_timestamp_ns
    ):
        measured = (message_count - 1) * 1e9 / (last_timestamp_ns - first_timestamp_ns)
        if 0.5 <= measured <= 200.0:
            return measured

    declared = _declared_camera_fps(episode, camera_topic)
    if declared:
        return declared

    return 30.0


def topic_names_from_episode_dir(episode_path: Path) -> list[str]:
    """Read topic list from metadata.json to avoid opening MCAP index."""
    meta_path = Path(episode_path) / "metadata.json"
    if not meta_path.is_file():
        return []
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    topics = data.get("topics") or []
    names: list[str] = []
    for entry in topics:
        if isinstance(entry, dict) and entry.get("name"):
            names.append(str(entry["name"]))
        elif isinstance(entry, str):
            names.append(entry)
    return names


def episode_topic_names(episode) -> list[str]:
    """Prioritize metadata object / metadata.json, then fallback to MCAP list_topics."""
    meta = episode.metadata
    if meta and meta.topics:
        return [t.name for t in meta.topics if t.name]
    from_path = topic_names_from_episode_dir(episode.path)
    if from_path:
        return from_path
    return episode.list_topics()


def pick_camera_topic_from_names(topics: list[str]) -> str | None:
    if CAMERA_FRONT_RGB in topics:
        return CAMERA_FRONT_RGB
    for topic in topics:
        if topic.startswith("/camera/") and topic.endswith("/rgb"):
            return topic
    for topic in topics:
        tl = topic.lower()
        if "/camera/" in tl and ("rgb" in tl or "color" in tl):
            return topic
    return None


def infer_fps_from_metrics(episode_path: Path, camera_topic: str | None = None) -> float | None:
    """Read camera frame rate from metrics.json without scanning MCAP."""
    metrics = load_metrics_json(episode_path)
    topic = camera_topic or pick_camera_topic_from_names(topic_names_from_episode_dir(episode_path))
    if not topic:
        align_fps = metrics.get("align_fps")
        return float(align_fps) if align_fps and float(align_fps) > 0 else None
    if metrics.get("aligned"):
        eff = (metrics.get("effective_frequency_hz") or {}).get(topic)
        if eff and float(eff) > 0:
            return float(eff)
        align_fps = metrics.get("align_fps")
        if align_fps and float(align_fps) > 0:
            return float(align_fps)
    native = (metrics.get("native_frequency_hz") or metrics.get("frequency_hz") or {}).get(topic)
    if native and float(native) > 0:
        return float(native)
    return None
