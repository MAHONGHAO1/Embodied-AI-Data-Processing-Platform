"""Unified import entrypoint for vendored QRDF SDK.

At runtime, data.bootstrap registers vendor/qrdf to sys.path;
type checking resolves via extraPaths in backend/pyrightconfig.json.
Business adapter layers should import SDK symbols from this module rather than directly using `from qrdf...`.
"""

from __future__ import annotations

from qrdf.registry.topics import (  # noqa: E402
    ACTION_JOINT,
    ACTION_LEFT_GRIPPER,
    ACTION_RIGHT_GRIPPER,
    CAMERA_FRONT_RGB,
    OBS_JOINT_STATE,
    OBS_LEFT_GRIPPER_STATE,
    OBS_RIGHT_GRIPPER_STATE,
)

from data import bootstrap  # noqa: F401 — ensure vendor/qrdf is on sys.path

__all__ = [
    "ACTION_JOINT",
    "ACTION_LEFT_GRIPPER",
    "ACTION_RIGHT_GRIPPER",
    "CAMERA_FRONT_RGB",
    "OBS_JOINT_STATE",
    "OBS_LEFT_GRIPPER_STATE",
    "OBS_RIGHT_GRIPPER_STATE",
]
