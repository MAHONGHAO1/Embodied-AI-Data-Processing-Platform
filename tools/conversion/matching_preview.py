"""Build a LeRobot v3 preview from Matching Cable per-frame HDF5 files."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sys
import traceback

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from robodata.matching import (ACTION_FIELD, CAMERA_SOURCE_FIELDS, FPS,
                               OPTIONAL_FEATURE_KEYS, OPTIONAL_NUMERIC_FIELDS, SOURCE_KIND, STATE_FIELD,
                               discover_episodes, inspect_frame_capabilities)

PREVIEW_PROFILE = "matching_cable_frames_v1"
REUSE_SCHEMA_VERSION = 1
LEROBOT_VERSION = "0.4.4"
VCODEC = (os.environ.get("ROBODATA_MATCHING_VCODEC") or "h264").strip() or "h264"


def _default_encoder_threads() -> int:
    """Four camera encoders share the CPU; leave headroom for decode and I/O."""
    return max(1, min(4, (os.cpu_count() or 4) // 4))


_ENCODER_THREADS = (os.environ.get("ROBODATA_MATCHING_ENCODER_THREADS") or "").strip()
ENCODER_THREADS = int(_ENCODER_THREADS) if _ENCODER_THREADS else _default_encoder_threads()
STATE_NAMES = [f"puppet_joint_{index}" for index in range(16)]
ACTION_NAMES = [f"master_joint_{index}" for index in range(16)]


def assert_lerobot_compatibility() -> None:
    """The reuse path uses LeRobot 0.4.4 writer internals; fail loudly on drift."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        actual = version("lerobot")
    except PackageNotFoundError as exc:
        raise RuntimeError("转换环境未安装 lerobot==0.4.4") from exc
    if actual != LEROBOT_VERSION:
        raise RuntimeError(
            f"Matching Cable 预览/复用只验证过 lerobot=={LEROBOT_VERSION}，当前为 {actual}；"
            "请同步 tools/conversion/pyproject.toml 与 uv.lock 后再试"
        )


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def probe_video(path: Path, expected_frames: int | None = None) -> dict:
    """Read container-level metadata; decode only when the frame count is not trustworthy."""
    import av

    with av.open(str(path)) as container:
        if not container.streams.video:
            raise ValueError(f"视频没有视频轨：{path}")
        stream = container.streams.video[0]
        frame_count = int(stream.frames or 0)
        if frame_count <= 0 or (expected_frames is not None and frame_count != expected_frames):
            frame_count = sum(1 for _ in container.decode(stream))
        duration = None
        if stream.duration is not None and stream.time_base is not None:
            duration = float(stream.duration * stream.time_base)
        elif container.duration is not None:
            duration = float(container.duration / av.time_base)
        fps = float(stream.average_rate) if stream.average_rate else None
        if duration is None and fps:
            duration = frame_count / fps
        return {
            "frame_count": frame_count,
            "width": int(stream.codec_context.width),
            "height": int(stream.codec_context.height),
            "codec": stream.codec_context.codec.canonical_name,
            "fps": fps,
            "duration": duration,
            "has_audio": bool(container.streams.audio),
        }


def decode_rgb(raw, field: str, expected_shape: tuple[int, int, int]):
    import numpy as np
    from PIL import Image
    try:
        image = Image.open(io.BytesIO(bytes(raw))).convert("RGB")
        array = np.asarray(image, dtype=np.uint8)
    except Exception as exc:
        raise ValueError(f"{field} 图像无法解码：{exc}") from exc
    if tuple(array.shape) != expected_shape:
        raise ValueError(f"{field} 图像尺寸发生变化：期望 {expected_shape}，实际 {array.shape}")
    return array


def validate_depth(raw, field: str) -> None:
    from PIL import Image
    try:
        image = Image.open(io.BytesIO(bytes(raw)))
        image.load()
    except Exception as exc:
        raise ValueError(f"{field} 深度图无法解码：{exc}") from exc


def feature_definitions(shapes: dict[str, tuple[int, int, int]],
                        optional_present: list[str] | None = None) -> dict:
    features = {
        "observation.state": {"dtype": "float32", "shape": (16,), "names": STATE_NAMES},
        "action": {"dtype": "float32", "shape": (16,), "names": ACTION_NAMES},
        "source.timestamp_ms": {"dtype": "float32", "shape": (1,), "names": None},
    }
    names = {
        "observation.eef": [f"puppet_eef_{i}" for i in range(28)],
        "master.eef": [f"master_eef_{i}" for i in range(28)],
        "observation.joint_effort": [f"joint_effort_{i}" for i in range(16)],
        "observation.force_torque": [f"force_torque_{i}" for i in range(12)],
    }
    for field in optional_present or []:
        key = OPTIONAL_FEATURE_KEYS[field]
        features[key] = {"dtype": "float32", "shape": (OPTIONAL_NUMERIC_FIELDS[field],),
                         "names": names[key]}
    for camera in shapes:
        features[camera] = {"dtype": "video", "shape": shapes[camera], "names": ["height", "width", "channels"]}
    return features


def inspect_shapes(first_frame: Path, cameras: list[str]) -> dict[str, tuple[int, int, int]]:
    import h5py
    from PIL import Image
    shapes = {}
    with h5py.File(first_frame, "r") as handle:
        for camera in cameras:
            raw = handle[CAMERA_SOURCE_FIELDS[camera]][0]
            image = Image.open(io.BytesIO(bytes(raw))).convert("RGB")
            shapes[camera] = (image.height, image.width, 3)
    return shapes


def read_numeric(group, field: str, width: int):
    import numpy as np
    if field not in group:
        raise ValueError(f"缺少字段 /{field}")
    array = np.asarray(group[field][()], dtype=np.float32).reshape(-1)
    if array.size != width:
        raise ValueError(f"/{field} 维度为 {array.size}，期望 {width}")
    if not np.isfinite(array).all():
        raise ValueError(f"/{field} 包含 NaN 或无穷值")
    return array


def _file_record(root: Path, path: Path) -> dict:
    return {"path": path.relative_to(root).as_posix(), "size": path.stat().st_size, "sha256": sha256(path)}


def _preview_reuse_manifest(preview: Path, episode_records: list[dict], cameras: list[str],
                            shapes: dict[str, tuple[int, int, int]], info: dict) -> dict:
    """Describe exactly one finalized MP4 per episode and camera for conversion reuse."""
    import pandas as pd

    episode_files = sorted((preview / "meta" / "episodes").rglob("*.parquet"))
    if not episode_files:
        raise ValueError("预览缺少 meta/episodes 元数据")
    episodes_meta = pd.concat([pd.read_parquet(path) for path in episode_files], ignore_index=True)
    by_index = {int(row["episode_index"]): row for _, row in episodes_meta.iterrows()}
    expected_episodes = {int(item["episode_index"]): int(item["length"]) for item in episode_records}
    if set(by_index) != set(expected_episodes):
        raise ValueError("预览 meta/episodes 与清单 Episode 集不一致")
    if int(info.get("total_episodes", -1)) != len(episode_records):
        raise ValueError("预览 info.total_episodes 与清单不一致")
    if int(info.get("total_frames", -1)) != sum(expected_episodes.values()):
        raise ValueError("预览 info.total_frames 与清单不一致")

    reuse_episodes = []
    shared_paths = set()
    for record in episode_records:
        index = int(record["episode_index"])
        length = expected_episodes[index]
        row = by_index[index]
        if int(row["length"]) != length:
            raise ValueError(f"预览 Episode {index} 长度与清单不一致")
        data_path = preview / "data" / f"chunk-{int(row['data/chunk_index']):03d}" / \
            f"file-{int(row['data/file_index']):03d}.parquet"
        if not data_path.is_file():
            raise ValueError(f"预览 Episode {index} 缺少数值 parquet")
        videos = {}
        for camera in cameras:
            chunk = int(row[f"videos/{camera}/chunk_index"])
            file_index = int(row[f"videos/{camera}/file_index"])
            relative = f"videos/{camera}/chunk-{chunk:03d}/file-{file_index:03d}.mp4"
            video_path = preview / relative
            if not video_path.is_file():
                raise ValueError(f"预览 Episode {index} 缺少 {camera} 视频")
            if relative in shared_paths:
                raise ValueError(f"预览视频被多个 Episode 共用，无法安全复用：{relative}")
            shared_paths.add(relative)
            from_timestamp = float(row[f"videos/{camera}/from_timestamp"])
            to_timestamp = float(row[f"videos/{camera}/to_timestamp"])
            if from_timestamp != 0.0 or not (to_timestamp > 0.0):
                raise ValueError(f"预览 Episode {index} {camera} 视频时间范围不是单集")
            probe = probe_video(video_path, length)
            expected_height, expected_width = [int(value) for value in shapes[camera][:2]]
            if probe["frame_count"] != length:
                raise ValueError(
                    f"预览 Episode {index} {camera} 视频帧数为 {probe['frame_count']}，期望 {length}"
                )
            if (probe["height"], probe["width"]) != (expected_height, expected_width):
                raise ValueError(f"预览 Episode {index} {camera} 视频分辨率与源图像不一致")
            if probe["fps"] is not None and abs(probe["fps"] - FPS) > 0.01:
                raise ValueError(f"预览 Episode {index} {camera} 视频帧率不是 {FPS}")
            if probe["duration"] is not None and abs(probe["duration"] - to_timestamp) > 0.1:
                raise ValueError(f"预览 Episode {index} {camera} 视频时长与元数据不一致")
            videos[camera] = {
                **_file_record(preview, video_path),
                "from_timestamp": from_timestamp, "to_timestamp": to_timestamp,
                "frame_count": probe["frame_count"], "width": probe["width"], "height": probe["height"],
                "codec": probe["codec"], "fps": probe["fps"], "duration": probe["duration"],
                "has_audio": probe["has_audio"],
            }
        reuse_episodes.append({**record, "data": _file_record(preview, data_path), "videos": videos})

    meta_files = [_file_record(preview, path) for path in sorted((preview / "meta").rglob("*")) if path.is_file()]
    return {"schema_version": REUSE_SCHEMA_VERSION, "layout": "one_video_per_episode", "fps": FPS,
            "episodes": reuse_episodes, "meta_files": meta_files}


def build_preview(source: Path, output: Path) -> dict:
    import h5py
    import numpy as np
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    assert_lerobot_compatibility()
    from lerobot_stats import patch_running_quantile_stats
    patch_running_quantile_stats()
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise FileExistsError(f"预览目录已有内容，不能覆盖：{output}")
        output.rmdir()
    episodes = discover_episodes(source, os.environ.get("ROBODATA_MATCHING_EPISODES"))
    capabilities = inspect_frame_capabilities(episodes[0]["frames"][0])
    cameras = capabilities["cameras"]
    shapes = inspect_shapes(episodes[0]["frames"][0], cameras)
    output.parent.mkdir(parents=True, exist_ok=True)
    dataset = LeRobotDataset.create(
        repo_id="local/matching-cable-teleoperation", root=output, fps=FPS,
        features=feature_definitions(shapes, capabilities["optional_present"]), robot_type="dual_arm_teleoperation",
        video_backend="pyav", vcodec=VCODEC, image_writer_processes=0,
        image_writer_threads=0, batch_encoding_size=1, metadata_buffer_size=1, streaming_encoding=True,
        encoder_threads=ENCODER_THREADS,
    )
    # One parquet and one MP4 per episode make preview artifacts addressable and
    # guarantee that conversion never has to trim or re-encode a shared video.
    dataset.meta.update_chunk_settings(data_files_size_in_mb=0.001, video_files_size_in_mb=0.001)
    hashes = []
    episode_records = []
    for episode in episodes:
        source_id = int(episode["source_episode_id"])
        source_frames = [Path(frame) for frame in episode["frames"]]
        previous_ms = None
        for frame_path in source_frames:
            digest = sha256(frame_path)
            hashes.append({"path": frame_path.relative_to(source).as_posix(), "size": frame_path.stat().st_size,
                           "mtime_ns": frame_path.stat().st_mtime_ns, "episode_id": source_id, "sha256": digest})
            with h5py.File(frame_path, "r") as handle:
                state = read_numeric(handle, STATE_FIELD, 16)
                action = read_numeric(handle, ACTION_FIELD, 16)
                if "timestamps" not in handle or np.asarray(handle["timestamps"]).size != 1:
                    raise ValueError(f"{frame_path.name} 缺少单值 /timestamps")
                source_ms = float(handle["timestamps"][0])
                if not math.isfinite(source_ms) or source_ms < 0:
                    raise ValueError(f"{frame_path.name} 的时间戳无效")
                if previous_ms is not None and abs((source_ms - previous_ms) - 50.0) > 10.0:
                    raise ValueError(
                        f"Episode {source_id} 的 {frame_path.name} 时间戳间隔为 "
                        f"{source_ms - previous_ms:.1f} ms，超出 50±10 ms；不静默按 20 FPS 继续")
                previous_ms = source_ms
                frame = {
                    "task": f"Matching Cable episode {source_id}",
                    "observation.state": state,
                    "action": action,
                    "source.timestamp_ms": np.asarray([source_ms], dtype=np.float32),
                }
                for field in capabilities["optional_present"]:
                    if field not in handle:
                        raise ValueError(f"可选字段 /{field} 在首帧存在，但 {frame_path.name} 缺失")
                    frame[OPTIONAL_FEATURE_KEYS[field]] = read_numeric(
                        handle, field, OPTIONAL_NUMERIC_FIELDS[field])
                for field in capabilities["optional_missing"]:
                    if field in handle:
                        raise ValueError(f"可选字段 /{field} 在首帧缺失，但 {frame_path.name} 出现")
                for camera in cameras:
                    source_field = CAMERA_SOURCE_FIELDS[camera]
                    if source_field not in handle:
                        raise ValueError(f"{camera} 在首帧存在，但 {frame_path.name} 缺失")
                    frame[camera] = decode_rgb(handle[source_field][0], source_field, shapes[camera])
                for camera in capabilities["missing_cameras"]:
                    if CAMERA_SOURCE_FIELDS[camera] in handle:
                        raise ValueError(f"{camera} 在首帧缺失，但 {frame_path.name} 出现")
                for depth_field in capabilities["depth_present"]:
                    if depth_field not in handle:
                        raise ValueError(f"深度字段 /{depth_field} 在首帧存在，但 {frame_path.name} 缺失")
                    validate_depth(handle[depth_field][0], depth_field.split("/")[-1])
                for depth_field in capabilities["depth_missing"]:
                    if depth_field in handle:
                        raise ValueError(f"深度字段 /{depth_field} 在首帧缺失，但 {frame_path.name} 出现")
                dataset.add_frame(frame)
        dataset.save_episode(parallel_encoding=False)
        episode_records.append({"episode_index": int(episode["episode_index"]), "source_episode_id": source_id,
                                "length": len(source_frames)})
    # Parquet footers and the streaming encoder must be closed before hashing or
    # handing any preview artifact to another process.
    dataset.finalize()
    info = dataset.meta.info
    reuse = _preview_reuse_manifest(output, episode_records, cameras, shapes, info)
    optional_status = {field: ("provided" if field in capabilities["optional_present"] else "not_provided")
                       for field in OPTIONAL_NUMERIC_FIELDS}
    limitations = []
    if capabilities["missing_cameras"]:
        limitations.append("首帧缺少的 RGB 相机不写入预览：" + "、".join(capabilities["missing_cameras"]))
    if capabilities["depth_missing"]:
        limitations.append("首帧缺少的深度图不校验、不写入：" + "、".join(capabilities["depth_missing"]))
    if capabilities["optional_missing"]:
        limitations.append("首帧缺少的可选数值字段不写入：" + "、".join(capabilities["optional_missing"]))
    limitations.append("适配器不读取触觉字段；缺失不阻断，存在也不进入输出特征。")
    limitations.append("文件级 20 FPS 与 RGB 映射不代表相机硬件同步。")
    limitations.append("最终转换复用预览 MP4；数值逐帧与源 HDF5 复核，不进行第二次视频编码。")
    manifest = {
        "schema_version": 1, "profile": PREVIEW_PROFILE, "format_version": "v3.0",
        "source": {
            "kind": SOURCE_KIND, "source_root": str(source), "repo_id": "local/matching-cable-teleoperation",
            "revision": "local-source", "license": "用户本机数据；许可由数据提供方确认",
            "robot": "双主从臂遥操作平台", "fps": FPS, "camera": cameras[0], "cameras": cameras,
            "missing_cameras": capabilities["missing_cameras"],
            "depth_source_cameras": capabilities["depth_present"],
            "missing_depth_source_cameras": capabilities["depth_missing"],
            "optional_fields": optional_status,
            "tactile": capabilities["tactile"],
            "episode_mapping": {str(item["episode_index"]): item["source_episode_id"] for item in episode_records},
            "state_field": STATE_FIELD, "action_field": ACTION_FIELD,
            "timestamp_semantics": "源 timestamps 为毫秒，50 ms/帧；输出时间按 20 FPS 派生，不声明硬件同步",
        },
        "episodes": episode_records, "total_episodes": len(episode_records),
        "total_frames": sum(item["length"] for item in episode_records),
        "camera_metadata": {camera: {"fps": FPS, "resolution": list(shapes[camera]), "encoding": "JPEG -> H.264 preview"}
                            for camera in cameras},
        "timestamp_warnings": [],
        "files": hashes,
        "reuse": reuse,
        "limitations": limitations,
        "info_fps": info.get("fps"),
    }
    write_json(output / "source_manifest.json", manifest)
    write_json(output / "preview_metadata.json", {"profile": PREVIEW_PROFILE, "episodes": episode_records,
                                                   "fps": FPS, "cameras": cameras,
                                                   "video_layout": "one_file_per_episode",
                                                   "reuse_schema_version": REUSE_SCHEMA_VERSION})
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = build_preview(args.input.resolve(), args.output.resolve())
        print(json.dumps({"status": "completed", "episodes": manifest["total_episodes"],
                          "frames": manifest["total_frames"]}, ensure_ascii=False))
        return 0
    except Exception as exc:
        diagnostic = {"stage": "preview", "status": "failed", "message_zh": str(exc),
                      "exception_type": type(exc).__name__, "exception_message": str(exc),
                      "file": str(args.input), "traceback": traceback.format_exc()}
        write_json(args.output / "diagnostic.json", diagnostic)
        print(json.dumps(diagnostic, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
