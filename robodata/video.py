"""CPU video decoding with explicit mapping to actual presentation timestamps."""

from __future__ import annotations

import bisect
import math
from functools import lru_cache
from pathlib import Path

import av

from .source import file_sha256


@lru_cache(maxsize=8)
def _inspect(path: str, content_sha256: str) -> dict:
    del content_sha256  # Included in the cache key, never inferred from mtime alone.
    pts_times = []
    with av.open(path) as container:
        if not container.streams.video:
            raise ValueError("文件中没有视频轨道")
        stream = container.streams.video[0]
        for frame in container.decode(stream):
            if frame.pts is None or frame.time_base is None:
                raise ValueError("视频帧缺少 PTS，无法可靠定位")
            timestamp = float(frame.pts * frame.time_base)
            if not math.isfinite(timestamp) or (pts_times and timestamp <= pts_times[-1]):
                raise ValueError("视频 PTS 不是严格递增的有限数值")
            pts_times.append(timestamp)
        if not pts_times:
            raise ValueError("视频没有可解码帧")
        return {"frame_count": len(pts_times), "width": stream.codec_context.width,
                "height": stream.codec_context.height,
                "codec": stream.codec_context.codec.canonical_name,
                "decoder": stream.codec_context.name,
                "fps": float(stream.average_rate) if stream.average_rate else None,
                "pts_times": tuple(pts_times)}


def inspect_video(path: Path) -> dict:
    path = Path(path).resolve()
    result = _inspect(str(path), file_sha256(path))
    return {**result, "pts_times": list(result["pts_times"])}


def episode_video_segment(decoded: dict, episode) -> dict:
    """Count only [from_timestamp, to_timestamp), returning episode-relative PTS."""
    if episode.video_end_time is None:
        return decoded
    start, end = episode.video_start_time, episode.video_end_time
    times = [t for t in decoded['pts_times'] if t >= start - 1e-6 and t < end - 1e-6]
    return {**decoded, 'file_frame_count': decoded['frame_count'], 'frame_count': len(times),
            'pts_times': [t - start for t in times], 'segment_start': start, 'segment_end': end}


def inspect_episode_video(episode) -> dict:
    return episode_video_segment(inspect_video(episode.video_path), episode)


def _decode_target(path: Path, target: float, seek: bool):
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        if seek:
            container.seek(int(target / float(stream.time_base)), stream=stream,
                           backward=True, any_frame=False)
        for frame in container.decode(stream):
            if frame.pts is None or frame.time_base is None:
                raise ValueError("视频帧缺少 PTS")
            current = float(frame.pts * frame.time_base)
            if abs(current - target) < 1e-8:
                return frame.to_ndarray(format="rgb24"), current
            if current > target + 1e-8:
                break
    return None


def read_video_frame(path: Path, timestamp: float) -> dict:
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
        raise ValueError("请求时间必须为有限数值")
    path = Path(path).resolve()
    metadata = _inspect(str(path), file_sha256(path))
    times = metadata["pts_times"]
    # float32 table timestamps can differ by a few microseconds from rational PTS.
    epsilon = max(1e-5, 0.001 / (metadata["fps"] or 30))
    if timestamp < times[0] - epsilon or timestamp > times[-1] + epsilon:
        raise ValueError(f"请求时间 {timestamp:.6f}s 超出视频 PTS 范围 [{times[0]:.6f}, {times[-1]:.6f}]s")
    insertion = bisect.bisect_left(times, timestamp)
    indices = {max(0, min(len(times) - 1, insertion)), max(0, insertion - 1)}
    closest = min(indices, key=lambda i: (abs(times[i] - timestamp), i))
    target = times[closest]
    try:
        decoded = _decode_target(path, target, seek=True)
    except (av.error.FFmpegError, ValueError):
        decoded = None
    if decoded is None:
        decoded = _decode_target(path, target, seek=False)
    if decoded is None:
        raise ValueError("已检查的视频帧无法再次定位；请检查文件是否发生变化")
    image, actual = decoded
    return {"image": image, "pts_time": actual, "timestamp_error": actual - timestamp,
            "frame_index": closest}
