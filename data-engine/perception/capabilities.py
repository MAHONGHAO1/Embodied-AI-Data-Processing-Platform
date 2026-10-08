"""
perception 领域能力声明。
在导入 perception 包时注册到 SDK 全局能力表，供 ``quic_op.invoke`` 统一调度。
声明方式与 ``dev_qcy`` 分支 ``clean/capabilities.py`` 保持一致。
"""
from __future__ import annotations

from quic_op.core.capability import CapabilitySpec, register_capability
from quic_op.core.data_types import HAND_POSE_OPERATOR_ID
from quic_op.perception.detecthands import __version__ as _HAND_POSE_VERSION

_TARGET = "quic_op.perception.detecthands:HandDetector"

PERCEPTION_CAPABILITIES = (
    CapabilitySpec(
        operator_id=HAND_POSE_OPERATOR_ID,
        domain="perception",
        name="hand_pose_yolo",
        description="YOLOv8m-pose 人手 Pose 检测（ONNX，默认 CPU；可 detect_video）",
        target=_TARGET,
        method="detect_video",
        version=_HAND_POSE_VERSION,
        input_kind="video_path",
        output_kind="HandPoseOperatorResult",
        persist_json=True,
        tags=("hand_pose", "yolo", "onnx", "cpu"),
        params={
            "video_path": "str",
            "conf": "float=0.25",
            "output_dir": "str|None",
            "weights_path": "str|None  # HandDetector.__init__",
            "device": "str|None  # HandDetector.__init__",
        },
    ),
    CapabilitySpec(
        operator_id="perception.hand_pose_yolo.frames",
        domain="perception",
        name="hand_pose_yolo_frames",
        description="YOLOv8m-pose 人手 Pose 检测（内存帧序列入口）",
        target=_TARGET,
        method="detect_frames",
        version=_HAND_POSE_VERSION,
        input_kind="frames",
        output_kind="HandPoseOperatorResult",
        persist_json=True,
        tags=("hand_pose", "yolo", "onnx", "memory"),
        params={
            "frames": "Sequence[np.ndarray]",
            "conf": "float=0.25",
            "output_dir": "str|None",
            "artifact_name": "str=memory_frames",
            "fps": "float|None",
            "weights_path": "str|None  # HandDetector.__init__",
            "device": "str|None  # HandDetector.__init__",
        },
    ),
    CapabilitySpec(
        operator_id="perception.hand_pose_yolo.hdf5",
        domain="perception",
        name="hand_pose_yolo_hdf5",
        description="YOLOv8m-pose 人手 Pose 检测（HDF5 episode 入口）",
        target=_TARGET,
        method="detect_hdf5",
        version=_HAND_POSE_VERSION,
        input_kind="hdf5_path",
        output_kind="HandPoseOperatorResult",
        persist_json=True,
        tags=("hand_pose", "yolo", "onnx", "hdf5"),
        params={
            "hdf5_path": "str",
            "conf": "float=0.25",
            "output_dir": "str|None",
            "weights_path": "str|None  # HandDetector.__init__",
            "device": "str|None  # HandDetector.__init__",
        },
    ),
)


def register_perception_capabilities(*, overwrite: bool = False) -> None:
    """注册 perception 人手 Pose 相关能力。"""
    for spec in PERCEPTION_CAPABILITIES:
        register_capability(spec, overwrite=overwrite)


# 导入即注册
register_perception_capabilities()
