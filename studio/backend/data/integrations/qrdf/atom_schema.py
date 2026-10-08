"""ATOM joint-space schema helpers shared by QRDF import/export."""

from __future__ import annotations

from typing import Any

import numpy as np

ATOM_UPPER_JOINTS_17: tuple[str, ...] = (
    "torso_joint",
    "left_shoulder_pitch_joint",
    "left_shoulder_roll_joint",
    "left_shoulder_yaw_joint",
    "left_elbow_pitch_joint",
    "left_elbow_roll_joint",
    "left_wrist_pitch_joint",
    "left_wrist_roll_joint",
    "right_shoulder_pitch_joint",
    "right_shoulder_roll_joint",
    "right_shoulder_yaw_joint",
    "right_elbow_pitch_joint",
    "right_elbow_roll_joint",
    "right_wrist_pitch_joint",
    "right_wrist_roll_joint",
    "head_yaw_joint",
    "head_pitch_joint",
)

ATOM_V1_CONTROLLED_JOINTS: tuple[str, ...] = (
    "left_shoulder_pitch_joint",
    "left_shoulder_roll_joint",
    "left_shoulder_yaw_joint",
    "left_elbow_pitch_joint",
    "left_elbow_roll_joint",
    "left_wrist_pitch_joint",
    "left_wrist_roll_joint",
    "right_shoulder_pitch_joint",
    "right_shoulder_roll_joint",
    "right_shoulder_yaw_joint",
    "right_elbow_pitch_joint",
    "right_elbow_roll_joint",
    "right_wrist_pitch_joint",
    "right_wrist_roll_joint",
    "head_yaw_joint",
    "head_pitch_joint",
)

ATOM_ARM_JOINTS: tuple[str, ...] = ATOM_V1_CONTROLLED_JOINTS[:14]
ATOM_LEFT_ARM_JOINTS: tuple[str, ...] = ATOM_ARM_JOINTS[:7]
ATOM_RIGHT_ARM_JOINTS: tuple[str, ...] = ATOM_ARM_JOINTS[7:14]
ATOM_UPPER_INDEX_BY_JOINT: dict[str, int] = {
    joint: index for index, joint in enumerate(ATOM_UPPER_JOINTS_17)
}
ATOM_V1_UPPER_INDEX_BY_JOINT: dict[str, int] = {
    joint: ATOM_UPPER_INDEX_BY_JOINT[joint] for joint in ATOM_V1_CONTROLLED_JOINTS
}

ATOM_LEROBOT_FEATURE_NAMES: tuple[str, ...] = (
    *(f"{joint}.pos" for joint in ATOM_LEFT_ARM_JOINTS),
    "left_gripper.pos",
    *(f"{joint}.pos" for joint in ATOM_RIGHT_ARM_JOINTS),
    "right_gripper.pos",
)

WRIST_JOINT_ALIASES: dict[str, tuple[str, ...]] = {
    "left_wrist_pitch_joint": ("left_wrist_pitch_joint", "left_wirst_pitch_joint"),
    "left_wrist_roll_joint": (
        "left_wrist_roll_joint",
        "left_wrist_yaw_joint",
        "left_wirst_yaw_joint",
    ),
    "right_wrist_pitch_joint": ("right_wrist_pitch_joint", "right_wirst_pitch_joint"),
    "right_wrist_roll_joint": (
        "right_wrist_roll_joint",
        "right_wrist_yaw_joint",
        "right_wirst_yaw_joint",
    ),
}


def normalize_gripper_value(value: float, default: float = 0.5) -> float:
    """Normalize AG95 values recorded as 0..1 or 0..100 into 0..1."""
    if not np.isfinite(value):
        return default
    if 0.0 <= value <= 1.0:
        return float(value)
    if 0.0 <= value <= 100.0:
        return float(np.float32(value / 100.0).item())
    return float(max(0.0, min(1.0, value)))


def atom_joint_values_from_upper_items(items: list[Any], attr: str) -> list[float]:
    values = [float(getattr(item, attr, 0.0)) for item in items]
    if len(values) < len(ATOM_UPPER_JOINTS_17):
        values.extend([0.0] * (len(ATOM_UPPER_JOINTS_17) - len(values)))
    return [values[ATOM_V1_UPPER_INDEX_BY_JOINT[joint]] for joint in ATOM_V1_CONTROLLED_JOINTS]


def atom_values_by_name(
    names: list[str], values: list[float], joints: tuple[str, ...]
) -> list[float]:
    name_to_value = {name: float(value) for name, value in zip(names, values, strict=False)}
    result: list[float] = []
    missing: list[str] = []
    for joint in joints:
        aliases = WRIST_JOINT_ALIASES.get(joint, (joint,))
        matched = next((alias for alias in aliases if alias in name_to_value), None)
        if matched is None:
            missing.append(joint)
        else:
            result.append(name_to_value[matched])
    if missing:
        raise ValueError(f"ATOM joint values missing joints: {missing}")
    return result


def is_motor_named_joint_state(names: list[str]) -> bool:
    """Check whether joint_state uses motor_XX naming (product of labeling-dev generic MCAP -> QRDF)."""
    if not names:
        return False
    return all(str(name).startswith("motor_") for name in names)


def atom_arm_values_from_motor_positions(values: list[float]) -> list[float]:
    """Export stage: map 17-channel motor positions to dual-arm 14 joints based on ATOM layout (without modifying QRDF source data)."""
    padded = list(values)
    if len(padded) < len(ATOM_UPPER_JOINTS_17):
        padded.extend([0.0] * (len(ATOM_UPPER_JOINTS_17) - len(padded)))
    return [float(padded[ATOM_V1_UPPER_INDEX_BY_JOINT[joint]]) for joint in ATOM_ARM_JOINTS]


def atom_arm_values_from_joint_msg(msg: Any, *, values_attr: str = "position") -> list[float]:
    """Dedicated for export stage: extract ATOM dual-arm 14-joint values from QRDF JointState/JointAction.

    Compatible with motor_XX joint names written by labeling-dev generic MCAP -> QRDF pipeline, without relying on rewriting joint names during import.
    """
    names = list(getattr(msg, "names", []))
    values = list(getattr(msg, values_attr, []))
    if not values:
        raise ValueError("ATOM export requires non-empty joint values")

    if is_motor_named_joint_state(names):
        return atom_arm_values_from_motor_positions(values)

    if names:
        return atom_values_by_name(names, values, ATOM_ARM_JOINTS)

    # No names: infer layout based on value length
    if len(values) == len(ATOM_V1_CONTROLLED_JOINTS):
        return atom_values_by_name(list(ATOM_V1_CONTROLLED_JOINTS), values, ATOM_ARM_JOINTS)
    return atom_arm_values_from_motor_positions(values)


def build_atom_vector_with_grippers(
    joint_values: list[float],
    *,
    left_gripper: float,
    right_gripper: float,
) -> list[float]:
    if len(joint_values) < len(ATOM_ARM_JOINTS):
        raise ValueError(
            f"Expected at least {len(ATOM_ARM_JOINTS)} arm joints, got {len(joint_values)}"
        )
    return [
        *joint_values[:7],
        normalize_gripper_value(left_gripper),
        *joint_values[7:14],
        normalize_gripper_value(right_gripper),
    ]
