"""Multimodal preview: MCAP aligned frames, 3-way RGB/Depth, EEF/gripper timeseries (Rerun-style)."""

from __future__ import annotations

import base64
import io
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np
from qrdf.registry.topic_layout import resolve_eef_reference_topic
from qrdf.schema.qrdf.v0 import camera_pb2
from qrdf.utils.image import camera_frame_to_rgb_array

from data.integrations.qrdf.episode_metrics import episode_topic_names, infer_fps_from_metrics
from data.integrations.qrdf.service import get_reader
from data.integrations.qrdf.timeseries_helpers import (
    SERIES_META,
    STATE_TOPIC_HINTS,
    _downsample,
    _gripper_ratio_extended,
    _has_any_value,
    collect_arm_states_extended,
    eef_xyz_for_arm,
)
from data.utils.formatting import normalize_api_fps

VIZ_MAX_SERIES_POINTS = 800
FRAME_JPEG_MAX_SIZE = 360
_SAMPLE_WORKERS = 4
_ENCODE_WORKERS = 3

# Multimodal session cache: key -> (mcap_mtime_ns, payload)
_SESSION_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
# Camera timeline index: episode_path:ref_topic -> [log_time, ...]
_REF_TIMELINE_CACHE: dict[str, list[int]] = {}
# Episode instance cache: cache_key -> (mtime_ns, episode)
_EPISODE_CACHE: dict[str, Any] = {}


def _dataset_cache_key(storage_path: str | None, episode_id: str) -> str | None:
    from data.integrations.qrdf.service import resolve_dataset_path

    dataset_path = resolve_dataset_path(storage_path)
    if not dataset_path:
        return None
    ep_dir = dataset_path / "episodes" / episode_id
    mcap = ep_dir / "data.mcap"
    if not mcap.is_file():
        alt = ep_dir / "episode.mcap"
        mcap = alt if alt.is_file() else mcap
    try:
        mtime = mcap.stat().st_mtime_ns if mcap.is_file() else 0
    except OSError:
        mtime = 0
    return f"{dataset_path.resolve()}:{episode_id}:{mtime}"


def clear_multimodal_session_cache() -> None:
    _SESSION_CACHE.clear()
    _REF_TIMELINE_CACHE.clear()
    _EPISODE_CACHE.clear()


def _load_episode(storage_path: str | None, episode_id: str):
    reader = get_reader(storage_path)
    if not reader:
        return None
    cache_key = _dataset_cache_key(storage_path, episode_id)
    if cache_key:
        mtime = int(cache_key.rsplit(":", 1)[-1])
        cached = _EPISODE_CACHE.get(cache_key)
        if cached and cached[0] == mtime:
            return cached[1]
    episode = reader.load_episode(episode_id)
    if cache_key:
        mtime = int(cache_key.rsplit(":", 1)[-1])
        _EPISODE_CACHE[cache_key] = (mtime, episode)
    return episode


def _reference_timeline_ns(episode, ref_topic: str) -> list[int]:
    cache_key = f"{episode.path}:{ref_topic}"
    cached = _REF_TIMELINE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    times = [int(m.log_time) for m in episode.mcap_reader.get_all_messages(ref_topic)]
    if times:
        deduped = [times[0]]
        for t in times[1:]:
            if t != deduped[-1]:
                deduped.append(t)
        times = deduped
    _REF_TIMELINE_CACHE[cache_key] = times
    return times


# 3-way camera RGB / Depth topic candidates (by stream id)
CAMERA_STREAM_DEFS: list[dict[str, Any]] = [
    {
        "id": "front",
        "label": "头部",
        "rgb_topics": [
            "/camera/front/rgb",
            "/camera/head/rgb",
            "/camera/head_color/image_raw",
            "/camera/rs2_head/color/image_raw",
        ],
        "depth_topics": [
            "/camera/front/depth",
            "/camera/head/depth",
            "/depth/camera_rs2_head_aligned_depth_to_color/image_raw",
            "/camera/head/aligned_depth_to_color",
        ],
    },
    {
        "id": "left_wrist",
        "label": "左腕",
        "rgb_topics": [
            "/camera/left/wrist/rgb",
            "/camera/left_wrist/rgb",
            "/camera/left_hand/color/image_raw",
            "/cameras/camera_left_hand_color_image_raw",
        ],
        "depth_topics": [
            "/camera/left/wrist/depth",
            "/camera/left_wrist/depth",
            "/depth/camera_left_hand_aligned_depth_to_color/image_raw",
        ],
    },
    {
        "id": "right_wrist",
        "label": "右腕",
        "rgb_topics": [
            "/camera/right/wrist/rgb",
            "/camera/right_wrist/rgb",
            "/camera/right_hand/color/image_raw",
            "/cameras/camera_right_hand_color_image_raw",
        ],
        "depth_topics": [
            "/camera/right/wrist/depth",
            "/camera/right_wrist/depth",
            "/depth/camera_right_hand_aligned_depth_to_color/image_raw",
        ],
    },
]


def _pick_topic(topics: set[str], candidates: list[str]) -> str | None:
    for topic in candidates:
        if topic in topics:
            return topic
    for topic in candidates:
        short = topic.split("/")[-1]
        for existing in topics:
            if existing.endswith(short) or short in existing:
                return existing
    return None


def resolve_camera_streams(episode) -> list[dict[str, str | None]]:
    topics = set(episode_topic_names(episode))
    streams: list[dict[str, str | None]] = []
    for spec in CAMERA_STREAM_DEFS:
        rgb = _pick_topic(topics, spec["rgb_topics"])
        depth = _pick_topic(topics, spec["depth_topics"])
        if not rgb and not depth:
            # Fuzzy match: topic name contains stream id and rgb/depth
            sid = spec["id"]
            for t in topics:
                tl = t.lower()
                if sid.replace("_", "") in tl.replace("_", "") or (
                    sid == "front" and ("head" in tl or "front" in tl)
                ):
                    if "depth" in tl and not depth:
                        depth = t
                    elif ("rgb" in tl or "color" in tl) and not rgb:
                        rgb = t
        if rgb or depth:
            streams.append(
                {
                    "id": spec["id"],
                    "label": spec["label"],
                    "rgb_topic": rgb,
                    "depth_topic": depth,
                }
            )
    return streams


def _jpeg_base64(rgb_array: np.ndarray, *, max_size: int = FRAME_JPEG_MAX_SIZE) -> str:
    from PIL import Image

    img = Image.fromarray(rgb_array)
    img.thumbnail((max_size, max_size))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _depth_to_rgb(frame: camera_pb2.CameraFrame) -> np.ndarray | None:
    encoding = (frame.encoding or "").lower()
    w = int(frame.width or 0)
    h = int(frame.height or 0)
    if w <= 0 or h <= 0 or not frame.data:
        return None

    if encoding in ("jpeg", "jpg", "png", "rgb8", "bgr8"):
        return camera_frame_to_rgb_array(frame)

    raw = bytes(frame.data)
    arr: np.ndarray | None = None
    if encoding in ("16uc1", "mono16", "depth16", "z16") or "depth" in encoding:
        if len(raw) >= w * h * 2:
            arr = np.frombuffer(raw, dtype=np.uint16).reshape(h, w).astype(np.float32)
        elif len(raw) >= w * h * 4:
            arr = np.frombuffer(raw, dtype=np.float32).reshape(h, w)
    elif len(raw) == w * h:
        arr = np.frombuffer(raw, dtype=np.uint8).reshape(h, w).astype(np.float32)
    elif len(raw) == w * h * 2:
        arr = np.frombuffer(raw, dtype=np.uint16).reshape(h, w).astype(np.float32)

    if arr is None:
        try:
            return camera_frame_to_rgb_array(frame)
        except Exception:
            return None

    valid = arr[np.isfinite(arr) & (arr > 0)]
    if valid.size == 0:
        return np.zeros((h, w, 3), dtype=np.uint8)
    vmin, vmax = float(np.percentile(valid, 2)), float(np.percentile(valid, 98))
    if vmax <= vmin:
        vmax = vmin + 1.0
    norm = np.clip((arr - vmin) / (vmax - vmin), 0, 1)
    # Purple -> Blue colormap (approximate Rerun depth)
    r = (norm * 180).astype(np.uint8)
    g = (norm * 60).astype(np.uint8)
    b = (255 - norm * 100).astype(np.uint8)
    mask = (arr <= 0) | ~np.isfinite(arr)
    rgb = np.stack([r, g, b], axis=-1)
    rgb[mask] = 0
    return rgb


def _camera_message_to_b64(msg: Any, *, is_depth: bool = False) -> str | None:
    if msg is None or not isinstance(msg, camera_pb2.CameraFrame):
        return None
    if not msg.data:
        return None
    try:
        rgb = _depth_to_rgb(msg) if is_depth else camera_frame_to_rgb_array(msg)
        if rgb is None:
            return None
        return _jpeg_base64(rgb)
    except Exception:
        return None


def _resolve_reference_topic(episode) -> tuple[str | None, str]:
    """Resolve reference topic for alignment, prioritizing head RGB camera (consistent with annotation preview MP4)."""
    streams = resolve_camera_streams(episode)
    for stream in streams:
        if stream.get("id") == "front" and stream.get("rgb_topic"):
            return stream["rgb_topic"], "head_camera"

    from data.integrations.qrdf.preview_export import _pick_camera_topic

    camera = _pick_camera_topic(episode)
    if camera:
        return camera, "head_camera"

    topics = episode_topic_names(episode)
    ref = resolve_eef_reference_topic(topics)
    if ref:
        return ref, "eef"
    for candidate in topics:
        if "eef" in candidate:
            return candidate, "eef"
    for candidate in topics:
        if "gripper" in candidate:
            return candidate, "eef"
    if topics:
        return topics[0], "fallback"
    return None, "fallback"


def _state_topics_for_series(topic_set: set[str]) -> list[str]:
    return sorted(t for t in topic_set if any(hint in t.lower() for hint in STATE_TOPIC_HINTS))


def _sample_state_frame(reader, state_topics: tuple[str, ...], ts_ns: int) -> dict[str, Any]:
    frame_data: dict[str, Any] = {"timestamp_ns": ts_ns}
    for topic in state_topics:
        nearest = reader.get_nearest_messages(topic, ts_ns)
        if not nearest:
            continue
        if len(nearest) == 1:
            frame_data[topic] = nearest[0].message
        else:
            frame_data[topic] = [m.message for m in nearest]
    return frame_data


def _build_state_aligned_frames(
    episode,
    ref_times: list[int],
    state_topics: list[str],
) -> list[dict[str, Any]]:
    """Nearest-neighbor sample status topics on camera reference timeline (without loading camera JPEGs)."""
    if not ref_times:
        return []
    if not state_topics:
        return [{"timestamp_ns": ts} for ts in ref_times]

    reader = episode.mcap_reader
    topics_tuple = tuple(state_topics)
    n = len(ref_times)
    workers = min(_SAMPLE_WORKERS, max(1, n // 48))

    def process_chunk(chunk: list[int]) -> list[dict[str, Any]]:
        return [_sample_state_frame(reader, topics_tuple, ts) for ts in chunk]

    if workers <= 1 or n < 64:
        return process_chunk(ref_times)

    chunk_size = (n + workers - 1) // workers
    chunks = [ref_times[i : i + chunk_size] for i in range(0, n, chunk_size)]
    aligned: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for part in pool.map(process_chunk, chunks):
            aligned.extend(part)
    return aligned


def _extract_series_from_aligned(
    aligned: list[dict[str, Any]],
    start_ns: int,
    *,
    max_points: int | None = VIZ_MAX_SERIES_POINTS,
) -> tuple[list[float], list[int], list[dict[str, Any]], bool]:
    buffers: dict[str, list[float | None]] = {
        "left_eef_x": [],
        "left_eef_y": [],
        "left_eef_z": [],
        "right_eef_x": [],
        "right_eef_y": [],
        "right_eef_z": [],
        "left_gripper": [],
        "right_gripper": [],
        "eef_x": [],
        "eef_y": [],
        "eef_z": [],
        "gripper": [],
    }
    times: list[float] = []
    timestamps_ns: list[int] = []

    dual_arm = False
    for frame_data in aligned:
        _, _, frame_dual = collect_arm_states_extended(frame_data)
        if frame_dual:
            dual_arm = True
            break

    for frame_data in aligned:
        eef_arms, grip_arms, frame_dual = collect_arm_states_extended(frame_data)
        if frame_dual:
            dual_arm = True
        if dual_arm:
            for arm, px in (("left", "left"), ("right", "right")):
                xyz = eef_xyz_for_arm(eef_arms.get(arm), arm)
                if xyz:
                    buffers[f"{px}_eef_x"].append(xyz[0])
                    buffers[f"{px}_eef_y"].append(xyz[1])
                    buffers[f"{px}_eef_z"].append(xyz[2])
                else:
                    buffers[f"{px}_eef_x"].append(None)
                    buffers[f"{px}_eef_y"].append(None)
                    buffers[f"{px}_eef_z"].append(None)
                buffers[f"{px}_gripper"].append(_gripper_ratio_extended(grip_arms.get(arm)))
        else:
            single_eef = eef_arms.get("single") or eef_arms.get("left")
            single_grip = grip_arms.get("single") or grip_arms.get("left")
            xyz = eef_xyz_for_arm(single_eef, "left")
            if xyz:
                buffers["eef_x"].append(xyz[0])
                buffers["eef_y"].append(xyz[1])
                buffers["eef_z"].append(xyz[2])
            else:
                buffers["eef_x"].append(None)
                buffers["eef_y"].append(None)
                buffers["eef_z"].append(None)
            buffers["gripper"].append(_gripper_ratio_extended(single_grip))

        ts_ns = int(frame_data.get("timestamp_ns") or start_ns)
        timestamps_ns.append(ts_ns)
        times.append(round((ts_ns - start_ns) / 1e9, 6))

    active_keys = (
        [
            "left_eef_x",
            "left_eef_y",
            "left_eef_z",
            "right_eef_x",
            "right_eef_y",
            "right_eef_z",
            "left_gripper",
            "right_gripper",
        ]
        if dual_arm
        else ["eef_x", "eef_y", "eef_z", "gripper"]
    )

    times, buffers = _downsample(times, buffers, max_points) if max_points else (times, buffers)
    if timestamps_ns and len(timestamps_ns) != len(times):
        step = max(1, len(timestamps_ns) // max(len(times), 1))
        timestamps_ns = [
            timestamps_ns[min(i * step, len(timestamps_ns) - 1)] for i in range(len(times))
        ]

    series: list[dict[str, Any]] = []
    for key in active_keys:
        values = buffers.get(key, [])
        if not _has_any_value(values):
            continue
        meta = SERIES_META.get(key, {"label": key, "group": "other", "arm": "", "unit": ""})
        series.append(
            {
                "id": key,
                "label": meta["label"],
                "group": meta["group"],
                "arm": meta["arm"],
                "unit": meta["unit"],
                "values": [round(v, 6) if v is not None else None for v in values],
            }
        )

    return times, timestamps_ns, series, dual_arm


def build_multimodal_session(
    storage_path: str | None,
    *,
    episode_id: str | None = None,
) -> dict[str, Any]:
    """Build multimodal session: head camera reference timeline + camera stream metadata + EEF/gripper curves."""
    reader = get_reader(storage_path)
    if not reader:
        return {"ok": False, "message": "未找到 QRDF 数据集"}

    episode_ids = reader.list_episodes()
    if not episode_ids:
        return {"ok": False, "message": "无 Episode"}

    eid = episode_id or episode_ids[0]
    cache_key = _dataset_cache_key(storage_path, eid)
    if cache_key:
        cached = _SESSION_CACHE.get(cache_key)
        if cached is not None:
            return dict(cached[1])
    episode = _load_episode(storage_path, eid)
    ref_topic, ref_kind = _resolve_reference_topic(episode)
    if not ref_topic:
        return {"ok": False, "message": "无法确定参考 topic", "episode_id": eid}

    ref_times = _reference_timeline_ns(episode, ref_topic)
    if not ref_times:
        return {"ok": False, "message": "参考 topic 无消息", "episode_id": eid}

    streams = resolve_camera_streams(episode)
    topics = episode_topic_names(episode)
    topic_set = set(topics)
    start_ns = int(ref_times[0])
    end_ns = int(ref_times[-1])
    playback_times = [round((ts - start_ns) / 1e9, 6) for ts in ref_times]
    sample_count = len(ref_times)

    state_topics = _state_topics_for_series(topic_set)
    aligned = _build_state_aligned_frames(episode, ref_times, state_topics)
    times, timestamps_ns, series, dual_arm = _extract_series_from_aligned(
        aligned, start_ns, max_points=VIZ_MAX_SERIES_POINTS
    )

    duration_sec = round((end_ns - start_ns) / 1e9, 4) if end_ns > start_ns else 0.0
    native_fps = None
    if ref_kind == "head_camera":
        native_fps = infer_fps_from_metrics(episode.path, ref_topic)
        if not native_fps or native_fps <= 0:
            from data.integrations.qrdf.preview_export import infer_camera_fps

            native_fps = infer_camera_fps(episode, ref_topic)
        native_fps = normalize_api_fps(native_fps)
    elif sample_count > 1 and duration_sec > 0:
        native_fps = normalize_api_fps((sample_count - 1) / duration_sec)

    ref_count = episode.mcap_reader.message_count(ref_topic)
    ref_label = {
        "head_camera": "头部相机",
        "eef": "EEF 状态",
        "fallback": "默认 Topic",
    }.get(ref_kind, "参考 Topic")
    has_eef = any(
        any(hint in t for hint in ("eef", "hand_state", "joint_state")) for t in topic_set
    )
    has_gripper = any("gripper" in t or "hand_state" in t for t in topic_set)
    has_depth = any("depth" in t for t in topic_set) or any(s.get("depth_topic") for s in streams)

    from data.integrations.qrdf.topic_preview import list_available_streams

    preview_streams = list_available_streams(storage_path)

    payload = {
        "ok": True,
        "episode_id": eid,
        "reference_topic": ref_topic,
        "reference_kind": ref_kind,
        "reference_label": ref_label,
        "reference_message_count": ref_count,
        "sample_count": sample_count,
        "max_index": max(0, sample_count - 1),
        "duration_sec": duration_sec
        if duration_sec > 0
        else (playback_times[-1] if playback_times else 0.0),
        "start_timestamp_ns": start_ns,
        "end_timestamp_ns": end_ns,
        "native_fps": native_fps,
        "playback_times": playback_times,
        "times": times,
        "timestamps_ns": timestamps_ns,
        "series": series,
        "dual_arm": dual_arm,
        "streams": streams,
        "preview_streams": preview_streams,
        "preview_video_mode": bool(preview_streams),
        "view_toggles": {
            "rgb": bool(streams),
            "action_chart": bool(series),
            "depth": has_depth,
        },
        "has_eef": has_eef,
        "has_gripper": has_gripper,
        "time_axis": "head_camera" if ref_kind == "head_camera" else "eef_reference",
    }
    if cache_key:
        _SESSION_CACHE[cache_key] = (episode.mcap_path.stat().st_mtime_ns, dict(payload))
    return payload


def lookup_playback_time_sec(
    storage_path: str | None,
    episode_id: str,
    frame_index: int,
    reference_topic: str | None = None,
) -> float | None:
    """Read playback time from session cache (avoids frame API rebuilding session)."""
    del reference_topic
    cache_key = _dataset_cache_key(storage_path, episode_id)
    if not cache_key:
        return None
    cached = _SESSION_CACHE.get(cache_key)
    if not cached:
        return None
    playback_times = cached[1].get("playback_times") or []
    if 0 <= frame_index < len(playback_times):
        return float(playback_times[frame_index])
    return None


def get_multimodal_frame(
    storage_path: str | None,
    *,
    episode_id: str,
    frame_index: int,
    streams: list[dict[str, str | None]] | None = None,
    reference_topic: str | None = None,
    kinds: str | None = None,
) -> dict[str, Any]:
    """Get RGB/Depth frames by reference timeline index; kinds can be rgb,depth (comma-separated)."""
    reader = get_reader(storage_path)
    if not reader:
        return {"ok": False, "message": "未找到 QRDF 数据集", "images": []}

    episode = _load_episode(storage_path, episode_id)
    if not episode:
        return {"ok": False, "message": "未找到 QRDF 数据集", "images": []}

    ref_topic, _ref_kind = (
        (reference_topic, "head_camera") if reference_topic else _resolve_reference_topic(episode)
    )
    if not ref_topic:
        return {"ok": False, "message": "无法确定参考 topic", "images": []}

    stream_list = streams or resolve_camera_streams(episode)
    kind_set: set[str] | None = None
    if kinds:
        kind_set = {k.strip().lower() for k in kinds.split(",") if k.strip()}
    if kind_set == {"depth"} and not any(s.get("depth_topic") for s in stream_list):
        ref_times = _reference_timeline_ns(episode, ref_topic)
        frame_index = max(0, min(frame_index, len(ref_times) - 1)) if ref_times else 0
        start_ns = ref_times[0] if ref_times else 0
        ts_ns = ref_times[frame_index] if ref_times else 0
        return {
            "ok": True,
            "episode_id": episode_id,
            "frame_index": frame_index,
            "timestamp_ns": ts_ns,
            "time_sec": round((ts_ns - start_ns) / 1e9, 6) if ref_times else 0.0,
            "images": [],
        }

    ref_times = _reference_timeline_ns(episode, ref_topic)
    if not ref_times:
        return {
            "ok": False,
            "message": "参考 topic 无消息",
            "frame_index": frame_index,
            "images": [],
        }

    frame_index = max(0, min(frame_index, len(ref_times) - 1))
    ts_ns = ref_times[frame_index]
    start_ns = ref_times[0]
    time_sec = round((ts_ns - start_ns) / 1e9, 6)
    mcap_reader = episode.mcap_reader

    def _encode_stream_images(stream: dict[str, str | None]) -> list[dict[str, Any]]:
        sid = stream["id"]
        label = stream.get("label") or sid
        rgb_topic = stream.get("rgb_topic")
        depth_topic = stream.get("depth_topic")
        rgb_msg = None
        depth_msg = None
        if rgb_topic:
            nearest = mcap_reader.get_nearest_messages(rgb_topic, ts_ns)
            rgb_msg = nearest[0].message if nearest else None
        if depth_topic:
            nearest = mcap_reader.get_nearest_messages(depth_topic, ts_ns)
            depth_msg = nearest[0].message if nearest else None
        out: list[dict[str, Any]] = []
        encode_rgb = kind_set is None or "rgb" in kind_set
        encode_depth = kind_set is None or "depth" in kind_set
        rgb_b64 = _camera_message_to_b64(rgb_msg, is_depth=False) if encode_rgb else None
        depth_b64 = _camera_message_to_b64(depth_msg, is_depth=True) if encode_depth else None
        if rgb_b64:
            out.append(
                {
                    "stream_id": sid,
                    "label": label,
                    "kind": "rgb",
                    "topic": rgb_topic,
                    "thumbnail_base64": rgb_b64,
                }
            )
        if depth_b64:
            out.append(
                {
                    "stream_id": sid,
                    "label": label,
                    "kind": "depth",
                    "topic": depth_topic,
                    "thumbnail_base64": depth_b64,
                }
            )
        return out

    images: list[dict[str, Any]] = []
    if len(stream_list) <= 1:
        for stream in stream_list:
            images.extend(_encode_stream_images(stream))
    else:
        workers = min(_ENCODE_WORKERS, len(stream_list))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for part in pool.map(_encode_stream_images, stream_list):
                images.extend(part)

    return {
        "ok": True,
        "episode_id": episode_id,
        "frame_index": frame_index,
        "timestamp_ns": ts_ns,
        "time_sec": time_sec,
        "images": images,
    }
