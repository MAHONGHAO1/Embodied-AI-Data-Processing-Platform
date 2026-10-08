"""EEF / gripper timeseries extraction helpers (shared between multimodal_preview and timeseries to avoid circular imports)."""

from __future__ import annotations

from typing import Any

from qrdf.converters.mapping import collect_eef_states, collect_gripper_states
from qrdf.registry.topic_layout import infer_arm_from_topic
from qrdf.registry.topics import OBS_JOINT_STATE
from qrdf.schema.qrdf.v0 import robot_state_pb2

from data.integrations.qrdf.atom_schema import normalize_gripper_value

STATE_TOPIC_HINTS = ("eef", "gripper", "hand_state", "joint_state")

SERIES_META: dict[str, dict[str, str]] = {
    "left_eef_x": {"label": "左臂 EEF X", "group": "eef", "arm": "left", "unit": "m"},
    "left_eef_y": {"label": "左臂 EEF Y", "group": "eef", "arm": "left", "unit": "m"},
    "left_eef_z": {"label": "左臂 EEF Z", "group": "eef", "arm": "left", "unit": "m"},
    "right_eef_x": {"label": "右臂 EEF X", "group": "eef", "arm": "right", "unit": "m"},
    "right_eef_y": {"label": "右臂 EEF Y", "group": "eef", "arm": "right", "unit": "m"},
    "right_eef_z": {"label": "右臂 EEF Z", "group": "eef", "arm": "right", "unit": "m"},
    "left_gripper": {"label": "左夹爪开合", "group": "gripper", "arm": "left", "unit": "ratio"},
    "right_gripper": {"label": "右夹爪开合", "group": "gripper", "arm": "right", "unit": "ratio"},
    "eef_x": {"label": "EEF X", "group": "eef", "arm": "single", "unit": "m"},
    "eef_y": {"label": "EEF Y", "group": "eef", "arm": "single", "unit": "m"},
    "eef_z": {"label": "EEF Z", "group": "eef", "arm": "single", "unit": "m"},
    "gripper": {"label": "夹爪开合", "group": "gripper", "arm": "single", "unit": "ratio"},
}


def _gripper_ratio(msg: Any) -> float | None:
    if msg is None:
        return None
    if msg.HasField("open_ratio"):
        return normalize_gripper_value(float(msg.open_ratio))
    if msg.HasField("width_m"):
        return normalize_gripper_value(float(msg.width_m))
    return None


def _eef_xyz(msg: Any) -> tuple[float, float, float] | None:
    if msg is None:
        return None
    if hasattr(msg, "position") and msg.position:
        pos = list(msg.position)
        if len(pos) >= 3:
            return float(pos[0]), float(pos[1]), float(pos[2])
    return None


def _infer_arm_from_topic_key(topic: str) -> str | None:
    arm = infer_arm_from_topic(topic)
    if arm:
        return arm
    tl = topic.lower()
    if "left" in tl and "right" not in tl:
        return "left"
    if "right" in tl:
        return "right"
    return None


def eef_xyz_for_arm(msg: Any, arm: str) -> tuple[float, float, float] | None:
    """Extract single-arm XYZ from EEFState / JointState / hand_state and related messages."""
    if msg is None:
        return None
    if isinstance(msg, robot_state_pb2.JointState) and msg.position:
        pos = list(msg.position)
        if arm == "left" and len(pos) >= 3:
            return float(pos[0]), float(pos[1]), float(pos[2])
        if arm == "right" and len(pos) >= 10:
            return float(pos[7]), float(pos[8]), float(pos[9])
        if arm == "right" and len(pos) >= 6:
            return float(pos[3]), float(pos[4]), float(pos[5])
    return _eef_xyz(msg)


def _gripper_ratio_extended(msg: Any) -> float | None:
    ratio = _gripper_ratio(msg)
    if ratio is not None:
        return ratio
    if msg is None:
        return None
    for attr in ("open_ratio", "realtime_position"):
        if hasattr(msg, attr):
            val = getattr(msg, attr)
            if val is not None:
                try:
                    return normalize_gripper_value(float(val))
                except (TypeError, ValueError):
                    pass
    if hasattr(msg, "position") and msg.position:
        try:
            return normalize_gripper_value(float(list(msg.position)[0]))
        except (TypeError, ValueError, IndexError):
            pass
    return None


def collect_arm_states_extended(
    frame_data: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Collect EEF / gripper states, compatible with split topics and hand_state / joint_state."""
    eef_list = collect_eef_states(frame_data)
    grip_list = collect_gripper_states(frame_data)
    eef_arms = _by_arm(eef_list)
    grip_arms = _by_arm(grip_list)

    if not eef_arms:
        skip_keys = {"timestamp_ns", "valid"}
        for topic, raw in frame_data.items():
            if topic in skip_keys or not isinstance(topic, str):
                continue
            tl = topic.lower()
            if not any(hint in tl for hint in STATE_TOPIC_HINTS):
                continue
            if "gripper" in tl:
                continue
            msgs = raw if isinstance(raw, list) else [raw]
            arm = _infer_arm_from_topic_key(topic)
            for msg in msgs:
                if msg is None:
                    continue
                if topic == OBS_JOINT_STATE or "joint_state" in tl:
                    if hasattr(msg, "position") and len(msg.position) >= 10:
                        eef_arms["left"] = msg
                        eef_arms["right"] = msg
                    elif arm:
                        eef_arms[arm] = msg
                    else:
                        eef_arms.setdefault("single", msg)
                elif any(k in tl for k in ("eef", "hand_state")):
                    key = arm or "single"
                    eef_arms[key] = msg

    if not grip_arms:
        skip_keys = {"timestamp_ns", "valid"}
        for topic, raw in frame_data.items():
            if topic in skip_keys or not isinstance(topic, str):
                continue
            if "gripper" not in topic.lower():
                continue
            msgs = raw if isinstance(raw, list) else [raw]
            arm = _infer_arm_from_topic_key(topic)
            for msg in msgs:
                if msg is None:
                    continue
                key = arm or "single"
                grip_arms[key] = msg

    dual = bool(eef_arms.get("left") and eef_arms.get("right")) or bool(
        grip_arms.get("left") and grip_arms.get("right")
    )
    if not dual:
        dual = any(
            (getattr(m, "arm", None) or "") in ("left", "right") for m in eef_list + grip_list
        )
    return eef_arms, grip_arms, dual


def _by_arm(messages: list[Any]) -> dict[str, Any]:
    arms: dict[str, Any] = {}
    for msg in messages:
        arm = (getattr(msg, "arm", None) or "").strip() or "single"
        if arm == "single":
            arms.setdefault("single", msg)
            arms.setdefault("left", msg)
        elif arm in ("left", "right"):
            arms[arm] = msg
    return arms


def _has_any_value(values: list[float | None]) -> bool:
    return any(v is not None for v in values)


def _downsample(
    times: list[float],
    buffers: dict[str, list[float | None]],
    max_points: int,
) -> tuple[list[float], dict[str, list[float | None]]]:
    n = len(times)
    if n <= max_points:
        return times, buffers
    step = (n - 1) / (max_points - 1) if max_points > 1 else 0
    indices = [round(i * step) for i in range(max_points)]
    new_times = [times[i] for i in indices]
    new_buffers = {k: [vals[i] for i in indices] for k, vals in buffers.items()}
    return new_times, new_buffers
