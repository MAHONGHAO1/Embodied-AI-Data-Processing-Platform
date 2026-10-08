"""QRDF -> LeRobot extended export (without modifying vendor/qrdf, runtime patch extends multi-camera and joint/gripper fields).

Supports two export templates:
- generic: generic template, joints/grippers exported as scalar fields
- atom: Dobot ATOM dedicated template, merges state/action vectors and performs strict timing checks
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

from data.integrations.qrdf.vendor_sdk import (
    ACTION_JOINT,
    ACTION_LEFT_GRIPPER,
    ACTION_RIGHT_GRIPPER,
    CAMERA_FRONT_RGB,
    OBS_JOINT_STATE,
    OBS_LEFT_GRIPPER_STATE,
    OBS_RIGHT_GRIPPER_STATE,
)

EXPORT_TEMPLATE_GENERIC = "generic"
EXPORT_TEMPLATE_ATOM = "atom"

LEROBOT_EXPORT_TEMPLATES: dict[str, dict[str, str]] = {
    EXPORT_TEMPLATE_GENERIC: {
        "key": EXPORT_TEMPLATE_GENERIC,
        "label": "通用模板",
        "description": "三路相机 + 关节/夹爪标量字段，适用于一般双臂机器人数据",
    },
    EXPORT_TEMPLATE_ATOM: {
        "key": EXPORT_TEMPLATE_ATOM,
        "label": "越疆 ATOM 专用",
        "description": "ATOM 关节空间向量映射，相机/action 严格时序校验（Δt ≤ 100ms/30ms）",
    },
}

GENERIC_IMAGE_MAP: dict[str, str] = {
    CAMERA_FRONT_RGB: "observation.images.image",
    "/camera/left/wrist/rgb": "observation.images.left_wrist",
    "/camera/right/wrist/rgb": "observation.images.right_wrist",
}

ATOM_IMAGE_MAP: dict[str, str] = {
    CAMERA_FRONT_RGB: "observation.images.top",
    "/camera/left/wrist/rgb": "observation.images.left_wrist",
    "/camera/right/wrist/rgb": "observation.images.right_wrist",
}

EXTRA_SCALAR_KEYS: tuple[str, ...] = (
    "observation.joint_position",
    "observation.joint_velocity",
    "observation.joint_effort",
    "observation.gripper",
)

MAX_ATOM_IMAGE_DELTA_MS = 100.0
MAX_ATOM_ACTION_DELTA_MS = 30.0

_originals: dict[str, Any] = {}
_patch_state: dict[str, Any] = {
    "template": EXPORT_TEMPLATE_GENERIC,
    "joint_shapes": {},
    "atom_joint_export_seen": False,
}


def normalize_export_template(template: str | None) -> str:
    key = (template or EXPORT_TEMPLATE_GENERIC).strip().lower()
    if key not in LEROBOT_EXPORT_TEMPLATES:
        raise ValueError(
            f"不支持的导出模板: {template!r}，可选: {', '.join(LEROBOT_EXPORT_TEMPLATES)}"
        )
    return key


def _first_message(value: Any) -> Any | None:
    if value is None:
        return None
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _gripper_open_ratio(msg: Any, default: float = 0.5) -> float:
    if msg is None:
        return default
    if msg.HasField("open_ratio"):
        return float(msg.open_ratio)
    if msg.HasField("width_m"):
        return float(msg.width_m)
    return default


def _generic_build_frame_record(
    frame_data: dict[str, Any],
    *,
    num_arms: int,
    task_language: str | None,
    image_size: tuple[int, int] | None = None,
) -> dict[str, Any]:
    record = _originals["build_frame_record"](
        frame_data,
        num_arms=num_arms,
        task_language=task_language,
        image_size=image_size,
    )

    joint = _first_message(frame_data.get(OBS_JOINT_STATE))
    if joint is not None:
        pos = list(joint.position)
        vel = list(joint.velocity)
        eff = list(joint.effort)
        record["observation.joint_position"] = pos
        record["observation.joint_velocity"] = vel
        record["observation.joint_effort"] = eff
        shapes = _patch_state["joint_shapes"]
        shapes["position"] = max(shapes.get("position", 0), len(pos))
        shapes["velocity"] = max(shapes.get("velocity", 0), len(vel))
        shapes["effort"] = max(shapes.get("effort", 0), len(eff))

    left_grip = _first_message(frame_data.get(OBS_LEFT_GRIPPER_STATE))
    right_grip = _first_message(frame_data.get(OBS_RIGHT_GRIPPER_STATE))
    record["observation.gripper"] = [
        _gripper_open_ratio(left_grip),
        _gripper_open_ratio(right_grip),
    ]
    return record


def _generic_feature_specs(
    sa_dim: int,
    video_keys: list[str],
    image_size: tuple[int, int] | None,
    fps: float,
    *,
    lerobot_version: str,
) -> dict[str, Any]:
    features = _originals["_feature_specs"](
        sa_dim,
        video_keys,
        image_size,
        fps,
        lerobot_version=lerobot_version,
    )
    shapes = _patch_state["joint_shapes"]
    if shapes.get("position"):
        features["observation.joint_position"] = {
            "dtype": "float32",
            "shape": [shapes["position"]],
        }
    if shapes.get("velocity"):
        features["observation.joint_velocity"] = {
            "dtype": "float32",
            "shape": [shapes["velocity"]],
        }
    if shapes.get("effort"):
        features["observation.joint_effort"] = {
            "dtype": "float32",
            "shape": [shapes["effort"]],
        }
    features["observation.gripper"] = {"dtype": "float32", "shape": [2]}
    return features


def _generic_write_modality_v2(
    output_path: Path,
    sa_dim: int,
    num_arms: int,
    video_keys: list[str],
    image_size: tuple[int, int] | None,
) -> None:
    import json

    _originals["_write_modality_v2"](output_path, sa_dim, num_arms, video_keys, image_size)
    modality_path = output_path / "meta" / "modality.json"
    if not modality_path.is_file():
        return
    modality = json.loads(modality_path.read_text(encoding="utf-8"))
    state = dict(modality.get("state", {}))
    offset = sa_dim
    shapes = _patch_state["joint_shapes"]
    if shapes.get("position"):
        state["joint_position"] = {"start": offset, "end": offset + shapes["position"]}
        offset += shapes["position"]
    if shapes.get("velocity"):
        state["joint_velocity"] = {"start": offset, "end": offset + shapes["velocity"]}
        offset += shapes["velocity"]
    if shapes.get("effort"):
        state["joint_effort"] = {"start": offset, "end": offset + shapes["effort"]}
        offset += shapes["effort"]
    state["gripper_open_ratio"] = {"start": offset, "end": offset + 2}
    modality["state"] = state
    modality_path.write_text(json.dumps(modality, indent=2) + "\n", encoding="utf-8")


def _generic_write_stats_v3(output_path: Path, bundle: Any) -> None:
    import json

    import numpy as np

    _originals["_write_stats_v3"](output_path, bundle)
    stats_path = output_path / "meta" / "stats.json"
    stats: dict[str, Any] = {}
    if stats_path.is_file():
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
    for key in EXTRA_SCALAR_KEYS:
        rows = [f.get(key) for f in bundle.all_frames if key in f]
        if not rows:
            continue
        arr = np.array(rows, dtype=np.float32)
        stats[key] = {
            "mean": arr.mean(axis=0).tolist(),
            "std": arr.std(axis=0).tolist(),
            "min": arr.min(axis=0).tolist(),
            "max": arr.max(axis=0).tolist(),
        }
    stats_path.write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")


def _atom_gripper_action_target(msg: Any, default: float = 0.5) -> float:
    from data.integrations.qrdf.atom_schema import normalize_gripper_value

    if msg is None:
        return default
    if msg.HasField("target_open_ratio"):
        return normalize_gripper_value(float(msg.target_open_ratio), default=default)
    if msg.HasField("target_width_m"):
        return normalize_gripper_value(float(msg.target_width_m), default=default)
    return default


def _atom_joint_positions(msg: Any, values_attr: str) -> list[float]:
    from data.integrations.qrdf.atom_schema import atom_arm_values_from_joint_msg

    return atom_arm_values_from_joint_msg(msg, values_attr=values_attr)


def _require_fresh_atom_action(frame_data: dict[str, Any]) -> None:
    deltas = frame_data.get("_alignment_deltas_ms") or {}
    delta_ms = deltas.get(ACTION_JOINT)
    if delta_ms is None:
        raise ValueError("ATOM LeRobot export requires /action/joint aligned from /upper/cmd")
    if float(delta_ms) > MAX_ATOM_ACTION_DELTA_MS:
        raise ValueError(
            f"ATOM /action/joint sample is {float(delta_ms):.1f}ms from frame time; "
            f"limit is {MAX_ATOM_ACTION_DELTA_MS:.1f}ms"
        )


def _require_synced_atom_images(frame_data: dict[str, Any], image_map: dict[str, str]) -> None:
    deltas = frame_data.get("_alignment_deltas_ms") or {}
    for topic in image_map:
        delta_ms = deltas.get(topic)
        if delta_ms is None:
            raise ValueError(f"ATOM LeRobot export requires camera topic {topic}")
        if float(delta_ms) > MAX_ATOM_IMAGE_DELTA_MS:
            raise ValueError(
                f"ATOM camera topic {topic} is {float(delta_ms):.1f}ms from frame time; "
                f"limit is {MAX_ATOM_IMAGE_DELTA_MS:.1f}ms"
            )


def _atom_build_frame_record(
    frame_data: dict[str, Any],
    *,
    num_arms: int,
    task_language: str | None,
    image_size: tuple[int, int] | None = None,
) -> dict[str, Any]:
    from data.integrations.qrdf.atom_schema import (
        ATOM_LEROBOT_FEATURE_NAMES,
        build_atom_vector_with_grippers,
    )

    record = _originals["build_frame_record"](
        frame_data,
        num_arms=num_arms,
        task_language=task_language,
        image_size=image_size,
    )

    joint = _first_message(frame_data.get(OBS_JOINT_STATE))
    if joint is not None:
        _require_synced_atom_images(frame_data, ATOM_IMAGE_MAP)
        left_grip = _first_message(frame_data.get(OBS_LEFT_GRIPPER_STATE))
        right_grip = _first_message(frame_data.get(OBS_RIGHT_GRIPPER_STATE))
        record["observation.state"] = build_atom_vector_with_grippers(
            _atom_joint_positions(joint, "position"),
            left_gripper=_gripper_open_ratio(left_grip),
            right_gripper=_gripper_open_ratio(right_grip),
        )
        action_joint = _first_message(frame_data.get(ACTION_JOINT))
        if action_joint is not None:
            _require_fresh_atom_action(frame_data)
            left_action = _first_message(frame_data.get(ACTION_LEFT_GRIPPER))
            right_action = _first_message(frame_data.get(ACTION_RIGHT_GRIPPER))
            record["action"] = build_atom_vector_with_grippers(
                _atom_joint_positions(action_joint, "target_position"),
                left_gripper=_atom_gripper_action_target(left_action),
                right_gripper=_atom_gripper_action_target(right_action),
            )
        elif record.get("observation.state") is not None:
            # Compatibility for labeling-dev generic MCAP import: use observation vector as action when /action/joint is absent
            record["action"] = list(record["observation.state"])
            left_action = _first_message(frame_data.get(ACTION_LEFT_GRIPPER))
            right_action = _first_message(frame_data.get(ACTION_RIGHT_GRIPPER))
            if left_action is not None:
                record["action"][7] = _atom_gripper_action_target(
                    left_action, default=float(record["action"][7])
                )
            if right_action is not None:
                record["action"][15] = _atom_gripper_action_target(
                    right_action, default=float(record["action"][15])
                )
        _patch_state["atom_joint_export_seen"] = True
        _patch_state["atom_feature_dim"] = len(ATOM_LEROBOT_FEATURE_NAMES)
    return record


def _atom_feature_specs(
    sa_dim: int,
    video_keys: list[str],
    image_size: tuple[int, int] | None,
    fps: float,
    *,
    lerobot_version: str,
) -> dict[str, Any]:
    from data.integrations.qrdf.atom_schema import ATOM_LEROBOT_FEATURE_NAMES

    features = _originals["_feature_specs"](
        sa_dim,
        video_keys,
        image_size,
        fps,
        lerobot_version=lerobot_version,
    )
    if _patch_state["atom_joint_export_seen"]:
        feature_dim = _patch_state.get("atom_feature_dim") or len(ATOM_LEROBOT_FEATURE_NAMES)
        features["observation.state"] = {
            "dtype": "float32",
            "shape": [feature_dim],
            "names": list(ATOM_LEROBOT_FEATURE_NAMES),
        }
        features["action"] = {
            "dtype": "float32",
            "shape": [feature_dim],
            "names": list(ATOM_LEROBOT_FEATURE_NAMES),
        }
    img_shape = [image_size[1], image_size[0], 3] if image_size else [480, 640, 3]
    for video_key in video_keys:
        if video_key in features:
            features[video_key]["shape"] = img_shape
            features[video_key]["names"] = ["height", "width", "channels"]
            if "info" in features[video_key]:
                features[video_key]["info"].update(
                    {
                        "video.height": img_shape[0],
                        "video.width": img_shape[1],
                        "video.channels": 3,
                    }
                )
    return features


def _atom_write_modality_v2(
    output_path: Path,
    sa_dim: int,
    num_arms: int,
    video_keys: list[str],
    image_size: tuple[int, int] | None,
) -> None:
    _originals["_write_modality_v2"](output_path, sa_dim, num_arms, video_keys, image_size)


def _atom_write_stats_v3(output_path: Path, bundle: Any) -> None:
    _originals["_write_stats_v3"](output_path, bundle)


def _append_extra_parquet_columns(columns: dict[str, Any], frames: list[dict[str, Any]]) -> None:
    for key in EXTRA_SCALAR_KEYS:
        if any(key in frame for frame in frames):
            columns[key] = [frame.get(key, []) for frame in frames]


def _extended_write_parquet_v3(output_path: Path, bundle: Any) -> None:
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    frames = bundle.all_frames
    states = np.array([f["observation.state"] for f in frames], dtype=np.float32)
    actions = np.array([f["action"] for f in frames], dtype=np.float32)
    columns: dict[str, Any] = {
        "index": list(range(len(frames))),
        "timestamp": [f["timestamp"] for f in frames],
        "episode_index": [f["episode_index"] for f in frames],
        "frame_index": [f["frame_index"] for f in frames],
        "task_index": [bundle.task_to_index.get(f["task"], 0) for f in frames],
        "observation.state": [s.tolist() for s in states],
        "action": [a.tolist() for a in actions],
    }
    if _patch_state["template"] == EXPORT_TEMPLATE_GENERIC:
        _append_extra_parquet_columns(columns, frames)
    pq.write_table(
        pa.table(columns),
        output_path / "data" / "chunk-000" / "file-000.parquet",
    )


def _extended_write_parquet_v2(
    output_path: Path,
    frames: list[dict[str, Any]],
    video_keys: list[str],
) -> None:
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    states = np.array([f["observation.state"] for f in frames], dtype=np.float32)
    actions = np.array([f["action"] for f in frames], dtype=np.float32)
    columns: dict[str, Any] = {
        "timestamp": [f["timestamp"] for f in frames],
        "episode_index": [f["episode_index"] for f in frames],
        "frame_index": [f["frame_index"] for f in frames],
        "task": [f["task"] for f in frames],
        "observation.state": [s.tolist() for s in states],
        "action": [a.tolist() for a in actions],
    }
    for video_key in video_keys:
        columns[video_key] = [
            f"videos/{video_key}/episode_{f['episode_index']:06d}.mp4" for f in frames
        ]
    if _patch_state["template"] == EXPORT_TEMPLATE_GENERIC:
        _append_extra_parquet_columns(columns, frames)
    pq.write_table(pa.table(columns), output_path / "data" / "episode_data.parquet")


def _camera_aligned_collect_frames(
    qrdf_path: str | Path,
    *,
    fps: float,
    image_size: tuple[int, int] | None,
    max_time_delta_ms: float,
) -> Any:
    """Align frames against head camera timeline; ATOM template additionally retains alignment_deltas_ms for timing checks."""
    import qrdf.converters.qrdf_to_lerobot as lerobot_mod
    from qrdf.reader.episode import Episode

    orig_resolve = lerobot_mod.resolve_eef_reference_topic
    orig_episode_frames = Episode.frames
    include_alignment = _patch_state["template"] == EXPORT_TEMPLATE_ATOM

    def patched_resolve(topics: Any) -> str | None:
        if CAMERA_FRONT_RGB in set(topics):
            return CAMERA_FRONT_RGB
        return orig_resolve(topics)

    def patched_episode_frames(
        self: Episode,
        *,
        reference_topic: str | None = None,
        fps: float | None = 10.0,
        max_time_delta_ms: float = 100.0,
        topics: list[str] | None = None,
        as_dict: bool = False,
    ) -> Any:
        align_fps = None if reference_topic == CAMERA_FRONT_RGB else fps
        frames = orig_episode_frames(
            self,
            reference_topic=reference_topic,
            fps=align_fps,
            max_time_delta_ms=max_time_delta_ms,
            topics=topics,
            as_dict=False,
        )
        from qrdf.frames.builder import frame_to_dict

        for frame in frames:
            if not as_dict:
                yield frame
                continue
            data = frame_to_dict(frame)
            if include_alignment:
                data["_alignment_deltas_ms"] = dict(frame.alignment_deltas_ms)
            yield data

    lerobot_mod.resolve_eef_reference_topic = patched_resolve
    Episode.frames = patched_episode_frames
    try:
        return _originals["_collect_frames"](
            qrdf_path,
            fps=fps,
            image_size=image_size,
            max_time_delta_ms=max_time_delta_ms,
        )
    finally:
        lerobot_mod.resolve_eef_reference_topic = orig_resolve
        Episode.frames = orig_episode_frames


def _ensure_originals() -> None:
    if _originals:
        return
    import qrdf.converters.mapping as mapping
    import qrdf.converters.qrdf_to_lerobot as lerobot_mod

    _originals["build_frame_record"] = mapping.build_frame_record
    _originals["LEROBOT_IMAGE_MAP"] = deepcopy(mapping.LEROBOT_IMAGE_MAP)
    _originals["lerobot_build_frame_record"] = lerobot_mod.build_frame_record
    _originals["_feature_specs"] = lerobot_mod._feature_specs
    _originals["_write_parquet_v3"] = lerobot_mod._write_parquet_v3
    _originals["_write_parquet_v2"] = lerobot_mod._write_parquet_v2
    _originals["_write_modality_v2"] = lerobot_mod._write_modality_v2
    _originals["_write_stats_v3"] = lerobot_mod._write_stats_v3
    _originals["_collect_frames"] = lerobot_mod._collect_frames


def _apply_patches(template: str) -> None:
    import qrdf.converters.mapping as mapping
    import qrdf.converters.qrdf_to_lerobot as lerobot_mod

    _patch_state["template"] = template
    _patch_state["joint_shapes"] = {}
    _patch_state["atom_joint_export_seen"] = False
    _patch_state.pop("atom_feature_dim", None)

    image_map = ATOM_IMAGE_MAP if template == EXPORT_TEMPLATE_ATOM else GENERIC_IMAGE_MAP
    build_frame_record = (
        _atom_build_frame_record
        if template == EXPORT_TEMPLATE_ATOM
        else _generic_build_frame_record
    )
    feature_specs = (
        _atom_feature_specs if template == EXPORT_TEMPLATE_ATOM else _generic_feature_specs
    )
    write_modality_v2 = (
        _atom_write_modality_v2 if template == EXPORT_TEMPLATE_ATOM else _generic_write_modality_v2
    )
    write_stats_v3 = (
        _atom_write_stats_v3 if template == EXPORT_TEMPLATE_ATOM else _generic_write_stats_v3
    )

    mapping.LEROBOT_IMAGE_MAP.clear()
    mapping.LEROBOT_IMAGE_MAP.update(image_map)
    mapping.build_frame_record = build_frame_record
    lerobot_mod.build_frame_record = build_frame_record
    lerobot_mod._feature_specs = feature_specs
    lerobot_mod._write_parquet_v3 = _extended_write_parquet_v3
    lerobot_mod._write_parquet_v2 = _extended_write_parquet_v2
    lerobot_mod._write_modality_v2 = write_modality_v2
    lerobot_mod._write_stats_v3 = write_stats_v3
    lerobot_mod._collect_frames = _camera_aligned_collect_frames


def _restore_patches() -> None:
    import qrdf.converters.mapping as mapping
    import qrdf.converters.qrdf_to_lerobot as lerobot_mod

    mapping.LEROBOT_IMAGE_MAP.clear()
    mapping.LEROBOT_IMAGE_MAP.update(_originals["LEROBOT_IMAGE_MAP"])
    mapping.build_frame_record = _originals["build_frame_record"]
    lerobot_mod.build_frame_record = _originals["lerobot_build_frame_record"]
    lerobot_mod._feature_specs = _originals["_feature_specs"]
    lerobot_mod._write_parquet_v3 = _originals["_write_parquet_v3"]
    lerobot_mod._write_parquet_v2 = _originals["_write_parquet_v2"]
    lerobot_mod._write_modality_v2 = _originals["_write_modality_v2"]
    lerobot_mod._write_stats_v3 = _originals["_write_stats_v3"]
    lerobot_mod._collect_frames = _originals["_collect_frames"]
    _patch_state["joint_shapes"] = {}
    _patch_state["atom_joint_export_seen"] = False
    _patch_state.pop("atom_feature_dim", None)


@contextmanager
def lerobot_export_extensions(*, template: str = EXPORT_TEMPLATE_GENERIC) -> Iterator[None]:
    """Install / restore LeRobot extension patches around vendor convert_dataset calls."""
    normalized = normalize_export_template(template)
    _ensure_originals()
    _apply_patches(normalized)
    try:
        yield
    finally:
        _restore_patches()


def convert_dataset_extended(
    qrdf_path: str | Path,
    output_path: str | Path,
    *,
    export_template: str = EXPORT_TEMPLATE_GENERIC,
    **kwargs: Any,
) -> Path:
    """QRDF -> LeRobot (select mapping strategy by template)."""
    from qrdf.converters.qrdf_to_lerobot import convert_dataset

    normalized = normalize_export_template(export_template)
    with lerobot_export_extensions(template=normalized):
        return convert_dataset(qrdf_path, output_path, **kwargs)
