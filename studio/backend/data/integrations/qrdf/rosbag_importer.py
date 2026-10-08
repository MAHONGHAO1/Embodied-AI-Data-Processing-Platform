"""Fast ROS2 rosbag2 MCAP -> QRDF Recorder importer (rosbags reader + OpenCV images + multithreading)."""

from __future__ import annotations

import math
import os
import re
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal

from filelock import FileLock
from qrdf.converters.source.helpers import (
    register_manipulation_topics,
    write_camera_frame,
    write_dual_arm_actions_from_states,
)
from qrdf.models.episode import ConversionInfo, EpisodeMetadata, RobotInfo, TaskInfo
from qrdf.recorder.recorder import QRDFRecorder
from qrdf.registry.topic_layout import TOPIC_LAYOUT_SPLIT
from qrdf.registry.topics import (
    OBS_JOINT_STATE,
    OBS_LEFT_EEF_STATE,
    OBS_LEFT_GRIPPER_STATE,
    OBS_RIGHT_EEF_STATE,
    OBS_RIGHT_GRIPPER_STATE,
)
from qrdf.schema.qrdf.v0 import common_pb2, robot_state_pb2

from data.integrations.qrdf.atom_mcap_reader import iter_atom_mcap_messages, rosbags_available
from data.integrations.qrdf.image_accel import ros_image_to_jpeg

try:
    from mcap_ros2.reader import read_ros2_messages
except ImportError as exc:  # pragma: no cover
    read_ros2_messages = None  # type: ignore[assignment]
    _MCAP_ROS2_IMPORT_ERROR = exc
else:
    _MCAP_ROS2_IMPORT_ERROR = None

ReaderBackend = Literal["auto", "rosbags", "mcap_ros2"]

DEFAULT_CAMERA_MAP: dict[str, tuple[str, str]] = {
    "/camera/rs2_head/color/image_raw": ("front", "/camera/front/rgb"),
    "/camera/left_hand/color/image_raw": ("left_wrist", "/camera/left/wrist/rgb"),
    "/camera/right_hand/color/image_raw": ("right_wrist", "/camera/right/wrist/rgb"),
}

UPPER_STATE_TOPIC = "/upper/state"
LEFT_GRIPPER_TOPIC = "/left_gripper/state"
RIGHT_GRIPPER_TOPIC = "/right_gripper/state"

LEFT_MOTOR_SLICE = slice(0, 7)
RIGHT_MOTOR_SLICE = slice(7, 14)

DEFAULT_IMAGE_WORKERS = max(4, min(16, (os.cpu_count() or 4)))

STATE_TOPICS = [UPPER_STATE_TOPIC, LEFT_GRIPPER_TOPIC, RIGHT_GRIPPER_TOPIC]


def _require_mcap_ros2() -> None:
    if read_ros2_messages is None:
        raise ImportError(
            "mcap-ros2-support 未安装，请执行: pip install mcap-ros2-support"
        ) from _MCAP_ROS2_IMPORT_ERROR


def _resolve_reader_backend(backend: ReaderBackend) -> ReaderBackend:
    if backend == "auto":
        return "rosbags" if rosbags_available() else "mcap_ros2"
    if backend == "rosbags" and not rosbags_available():
        raise ImportError("rosbags 未安装，请执行: pip install rosbags")
    if backend == "mcap_ros2":
        _require_mcap_ros2()
    return backend


def _iter_mcap_messages(
    mcap_path: Path,
    topics: list[str],
    backend: ReaderBackend,
) -> Iterator[tuple[str, int, Any]]:
    if backend == "rosbags":
        try:
            yield from iter_atom_mcap_messages(mcap_path, topics)
            return
        except Exception as exc:
            err = str(exc).lower()
            if "profile" in err and "ros2" in err and read_ros2_messages is not None:
                backend = "mcap_ros2"
            else:
                raise
    _require_mcap_ros2()
    for msg in read_ros2_messages(str(mcap_path), topics=topics):
        yield msg.channel.topic, int(msg.log_time_ns), msg.ros_msg


def _normalize_quaternion(values: list[float]) -> list[float]:
    if len(values) < 4:
        return [0.0, 0.0, 0.0, 1.0]
    x, y, z, w = values[:4]
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if norm <= 1e-9:
        return [0.0, 0.0, 0.0, 1.0]
    return [x / norm, y / norm, z / norm, w / norm]


def _motors_to_eef(motors: list[Any], arm: str, timestamp_ns: int) -> robot_state_pb2.EEFState:
    qs = [float(getattr(m, "q", 0.0)) for m in motors]
    position = qs[:3] if len(qs) >= 3 else [0.0, 0.0, 0.0]
    orientation = _normalize_quaternion(qs[3:7])
    return robot_state_pb2.EEFState(
        header=common_pb2.Header(timestamp_ns=timestamp_ns, frame_id="base_link"),
        arm=arm,
        position=position,
        orientation_xyzw=orientation,
    )


def _gripper_to_state(msg: Any | None, arm: str, timestamp_ns: int) -> robot_state_pb2.GripperState:
    ratio = float(getattr(msg, "realtime_position", 0.5) if msg is not None else 0.5)
    ratio = max(0.0, min(1.0, ratio))
    return robot_state_pb2.GripperState(
        header=common_pb2.Header(timestamp_ns=timestamp_ns),
        arm=arm,
        open_ratio=ratio,
    )


def _joint_state_from_motors(
    motors: list[Any],
    timestamp_ns: int,
    *,
    names: list[str] | None = None,
) -> robot_state_pb2.JointState:
    if names is None:
        names = [f"motor_{idx:02d}" for idx in range(len(motors))]
    positions = [float(getattr(m, "q", 0.0)) for m in motors]
    velocities = [float(getattr(m, "dq", 0.0)) for m in motors]
    efforts = [float(getattr(m, "tau_est", 0.0)) for m in motors]
    return robot_state_pb2.JointState(
        header=common_pb2.Header(timestamp_ns=timestamp_ns, frame_id="base_link"),
        names=names,
        position=positions,
        velocity=velocities,
        effort=efforts,
    )


def _parse_yaml_summary(yaml_path: Path | None) -> dict[str, Any]:
    if not yaml_path or not yaml_path.is_file():
        return {}
    try:
        import yaml
    except ImportError:
        return {}
    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except UnicodeDecodeError:
        data = yaml.safe_load(yaml_path.read_text(encoding="latin-1")) or {}
    except Exception:
        return {}
    info = data.get("rosbag2_bagfile_information") or {}
    duration_ns = (info.get("duration") or {}).get("nanoseconds")
    start_ns = (info.get("starting_time") or {}).get("nanoseconds_since_epoch")
    return {
        "duration_ns": duration_ns,
        "start_ns": start_ns,
        "message_count": info.get("message_count"),
    }


def _episode_id_from_mcap(mcap_path: Path, index: int) -> str:
    match = re.search(r"_(\d+)\.mcap$", mcap_path.name)
    suffix = match.group(1) if match else str(index)
    return f"episode_{int(suffix):06d}"


def _dataset_lock_path(dataset_dir: Path) -> Path:
    return dataset_dir / ".dataset.lock"


class RosBagMcapImporter:
    """Import single ROS2 rosbag2 MCAP file as QRDF episode."""

    def __init__(
        self,
        *,
        camera_map: dict[str, tuple[str, str]] | None = None,
        max_messages_per_topic: int | None = None,
        image_workers: int = DEFAULT_IMAGE_WORKERS,
        skip_preview: bool = True,
        reader_backend: ReaderBackend = "auto",
    ) -> None:
        self.camera_map = camera_map or dict(DEFAULT_CAMERA_MAP)
        self.max_messages_per_topic = max_messages_per_topic
        self.image_workers = max(1, image_workers)
        self.skip_preview = skip_preview
        self.reader_backend = reader_backend
        self.last_import_timing: dict[str, float] | None = None

    def import_episode(
        self,
        mcap_path: str | Path,
        dataset_dir: str | Path,
        *,
        episode_id: str | None = None,
        yaml_path: str | Path | None = None,
        task_name: str | None = None,
        dataset_name: str | None = None,
        bag_folder: str | None = None,
    ) -> QRDFRecorder:
        backend = _resolve_reader_backend(self.reader_backend)
        mcap_path = Path(mcap_path)
        dataset_dir = Path(dataset_dir)
        if not mcap_path.is_file():
            raise FileNotFoundError(f"MCAP 文件不存在: {mcap_path}")

        yaml_path = Path(yaml_path) if yaml_path else mcap_path.with_suffix(".yaml")
        episode_id = episode_id or _episode_id_from_mcap(mcap_path, 1)

        metadata = EpisodeMetadata(
            episode_id=episode_id,
            task=TaskInfo(
                name=task_name or bag_folder or mcap_path.stem,
                language="robot manipulation demonstration",
            ),
            robot=RobotInfo(
                name="dobot_atom",
                type="dual_arm",
                num_arms=2,
                control_mode="joint_position",
            ),
            conversion=ConversionInfo(
                source_format="ros2_mcap",
                source_path=str(mcap_path),
                action_source="next_state_fallback",
            ),
        )

        recorder = QRDFRecorder(
            dataset_dir, episode_id, metadata=metadata, dataset_name=dataset_name
        )
        register_manipulation_topics(recorder, num_arms=2, topic_layout=TOPIC_LAYOUT_SPLIT)
        recorder.add_lowdim(OBS_JOINT_STATE, "qrdf.v0.JointState")

        for _ros_topic, (cam_name, qrdf_topic) in self.camera_map.items():
            recorder.add_camera(cam_name, qrdf_topic, encoding="jpeg", width=640, height=480)

        recorder.start()

        import_started = time.perf_counter()
        state_started = time.perf_counter()
        self._import_state_pass(recorder, mcap_path, backend)
        state_elapsed = time.perf_counter() - state_started
        camera_started = time.perf_counter()
        self._import_camera_pass(recorder, mcap_path, backend)
        camera_elapsed = time.perf_counter() - camera_started
        self.last_import_timing = {
            "state_pass_sec": round(state_elapsed, 3),
            "camera_pass_sec": round(camera_elapsed, 3),
            "total_sec": round(time.perf_counter() - import_started, 3),
        }

        lock = FileLock(str(_dataset_lock_path(dataset_dir)))
        with lock:
            recorder.stop()

        if recorder.episode_path:
            from data.integrations.qrdf.episode_metrics import refresh_native_metrics
            from data.integrations.qrdf.episode_timing import repair_episode_timing

            repair_episode_timing(recorder.episode_path)
            refresh_native_metrics(recorder.episode_path)

            preview_path = recorder.episode_path / "preview.mp4"
            if self.skip_preview:
                if preview_path.is_file():
                    preview_path.unlink()
            else:
                try:
                    from data.integrations.qrdf.preview_export import export_episode_preview

                    export_episode_preview(recorder.episode_path, preview_path)
                except Exception:
                    if preview_path.is_file():
                        preview_path.unlink()

        return recorder

    def _import_state_pass(
        self,
        recorder: QRDFRecorder,
        mcap_path: Path,
        backend: ReaderBackend,
    ) -> None:
        """First pass: write full joint / EEF / gripper data (preserving original timestamps and count, no downsampling)."""
        stats: dict[str, int] = {}
        latest_gripper: dict[str, robot_state_pb2.GripperState] = {}
        prev_bundle: dict[str, Any] | None = None
        motor_names: list[str] | None = None

        for topic, ts, ros_msg in _iter_mcap_messages(mcap_path, STATE_TOPICS, backend):
            stats[topic] = stats.get(topic, 0) + 1
            if self.max_messages_per_topic and stats[topic] > self.max_messages_per_topic:
                continue

            if topic == LEFT_GRIPPER_TOPIC:
                grip = _gripper_to_state(ros_msg, "left", ts)
                latest_gripper["left"] = grip
                recorder.write(OBS_LEFT_GRIPPER_STATE, grip)
                continue

            if topic == RIGHT_GRIPPER_TOPIC:
                grip = _gripper_to_state(ros_msg, "right", ts)
                latest_gripper["right"] = grip
                recorder.write(OBS_RIGHT_GRIPPER_STATE, grip)
                continue

            if topic != UPPER_STATE_TOPIC:
                continue

            motors = list(getattr(ros_msg, "motor_state", []))
            if not motors:
                continue
            if motor_names is None:
                motor_names = [f"motor_{idx:02d}" for idx in range(len(motors))]

            left_eef = _motors_to_eef(motors[LEFT_MOTOR_SLICE], "left", ts)
            right_eef = _motors_to_eef(motors[RIGHT_MOTOR_SLICE], "right", ts)
            left_grip = latest_gripper.get("left") or _gripper_to_state(None, "left", ts)
            right_grip = latest_gripper.get("right") or _gripper_to_state(None, "right", ts)

            joint = _joint_state_from_motors(motors, ts, names=motor_names)
            recorder.write(OBS_JOINT_STATE, joint)
            recorder.write(OBS_LEFT_EEF_STATE, left_eef)
            recorder.write(OBS_RIGHT_EEF_STATE, right_eef)

            if prev_bundle is not None:
                write_dual_arm_actions_from_states(
                    recorder,
                    prev_bundle["eef_states"],
                    prev_bundle["gripper_states"],
                    prev_bundle["timestamp_ns"],
                    topic_layout=TOPIC_LAYOUT_SPLIT,
                )

            prev_bundle = {
                "timestamp_ns": ts,
                "eef_states": [left_eef, right_eef],
                "gripper_states": [left_grip, right_grip],
            }

    def _import_camera_pass(
        self,
        recorder: QRDFRecorder,
        mcap_path: Path,
        backend: ReaderBackend,
    ) -> None:
        """Second pass: camera topics only, multi-threaded OpenCV conversion to JPEG before writing."""
        camera_topics = list(self.camera_map.keys())
        stats: dict[str, int] = {}
        pending_images: list[tuple[Any, str, str, int]] = []
        batch_size = self.image_workers * 8

        def _flush_images(pool: ThreadPoolExecutor) -> None:
            if not pending_images:
                return
            futures = [
                pool.submit(ros_image_to_jpeg, ros_msg)
                for ros_msg, _cam, _topic, _ts in pending_images
            ]
            for (_ros_msg, cam_name, qrdf_topic, ts), fut in zip(
                pending_images, futures, strict=False
            ):
                jpeg, width, height = fut.result()
                write_camera_frame(
                    recorder,
                    qrdf_topic,
                    cam_name,
                    jpeg,
                    ts,
                    width=width,
                    height=height,
                )
            pending_images.clear()

        with ThreadPoolExecutor(max_workers=self.image_workers) as pool:
            for topic, ts, ros_msg in _iter_mcap_messages(mcap_path, camera_topics, backend):
                stats[topic] = stats.get(topic, 0) + 1
                if self.max_messages_per_topic and stats[topic] > self.max_messages_per_topic:
                    continue
                cam_name, qrdf_topic = self.camera_map[topic]
                pending_images.append((ros_msg, cam_name, qrdf_topic, ts))
                if len(pending_images) >= batch_size:
                    _flush_images(pool)
            _flush_images(pool)

    def iter_mcap_files(self, folder: str | Path) -> Iterator[Path]:
        folder = Path(folder)
        if not folder.is_dir():
            raise FileNotFoundError(f"Bag 目录不存在: {folder}")
        yield from sorted(folder.glob("*.mcap"))


def scan_bag_folder(folder: str | Path) -> list[dict[str, Any]]:
    """Scan bag directory, returning MCAP file list and yaml metadata."""
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise FileNotFoundError(f"Bag 目录不存在: {folder}")

    files: list[dict[str, Any]] = []
    for mcap in sorted(folder.glob("*.mcap")):
        yaml_path = mcap.with_suffix(".yaml")
        info = _parse_yaml_summary(yaml_path if yaml_path.is_file() else None)
        files.append(
            {
                "mcap_path": str(mcap),
                "yaml_path": str(yaml_path) if yaml_path.is_file() else None,
                "file_name": mcap.name,
                "size_bytes": mcap.stat().st_size,
                "duration_ns": info.get("duration_ns"),
                "message_count": info.get("message_count"),
            }
        )
    return files
