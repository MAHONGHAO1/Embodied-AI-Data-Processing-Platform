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
STATE_NAMES = [f"puppet_joint_{index}" for index in range(16)]
ACTION_NAMES = [f"master_joint_{index}" for index in range(16)]


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


def build_preview(source: Path, output: Path) -> dict:
    import h5py
    import numpy as np
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

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
        video_backend="pyav", vcodec="h264", image_writer_processes=0,
        image_writer_threads=0, batch_encoding_size=1, streaming_encoding=True,
    )
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
    info = dataset.meta.info
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
        "limitations": limitations,
        "info_fps": info.get("fps"),
    }
    write_json(output / "source_manifest.json", manifest)
    write_json(output / "preview_metadata.json", {"profile": PREVIEW_PROFILE, "episodes": episode_records,
                                                   "fps": FPS, "cameras": cameras})
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
