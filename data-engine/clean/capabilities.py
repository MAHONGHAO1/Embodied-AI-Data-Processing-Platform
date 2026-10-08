"""
clean 领域能力声明。
在导入 clean 包时注册到 SDK 全局能力表，供 ``quic_op.invoke`` 统一调度。
"""
from __future__ import annotations

from quic_op.core.capability import CapabilitySpec, register_capability
from quic_op.core.data_types import (
    BLUR_OPERATOR_ID,
    FRAME_DROP_OPERATOR_ID,
    LOWLIGHT_OPERATOR_ID,
)
from quic_op.clean.quality_inspector import __version__ as _QUALITY_VERSION

_TARGET = "quic_op.clean.quality_inspector:QualityInspector"

CLEAN_CAPABILITIES = (
    CapabilitySpec(
        operator_id=LOWLIGHT_OPERATOR_ID,
        domain="clean",
        name="quality_lowlight",
        description="长视频低光质量检测（ffmpeg 灰度降维采样）",
        target=_TARGET,
        method="detect_lowlight_metrics",
        version=_QUALITY_VERSION,
        input_kind="video_path",
        output_kind="LowLightOperatorResult",
        persist_json=True,
        tags=("quality", "lowlight", "cpu"),
        params={
            "video_path": "str",
            "light_mean_threshold": "float=35.0",
            "dark_pixel_thresh": "int=30",
            "dark_area_ratio": "float=0.28",
            "sample_fps": "int=5",
            "scale_height": "int=360",
            "output_dir": "str|None",
        },
    ),
    CapabilitySpec(
        operator_id=BLUR_OPERATOR_ID,
        domain="clean",
        name="quality_blur",
        description="长视频模糊质量检测（Laplacian 方差）",
        target=_TARGET,
        method="detect_blur_metrics",
        version=_QUALITY_VERSION,
        input_kind="video_path",
        output_kind="BlurOperatorResult",
        persist_json=True,
        tags=("quality", "blur", "cpu"),
        params={
            "video_path": "str",
            "blur_threshold": "float=22.0",
            "sample_fps": "int=5",
            "scale_height": "int=360",
            "output_dir": "str|None",
        },
    ),
    CapabilitySpec(
        operator_id=FRAME_DROP_OPERATOR_ID,
        domain="clean",
        name="quality_frame_drop",
        description="长视频掉帧检测（ffprobe PTS，不解帧）",
        target=_TARGET,
        method="detect_frame_drop_metrics",
        version=_QUALITY_VERSION,
        input_kind="video_path",
        output_kind="FrameDropOperatorResult",
        persist_json=True,
        tags=("quality", "frame_drop", "cpu"),
        params={
            "video_path": "str",
            "expected_fps": "float=15.0",
            "drop_threshold_sec": "float|None",
            "timeout_sec": "int=600",
            "output_dir": "str|None",
        },
    ),
)


def register_clean_capabilities(*, overwrite: bool = False) -> None:
    """注册 clean 质量检测能力。"""
    for spec in CLEAN_CAPABILITIES:
        register_capability(spec, overwrite=overwrite)


# 导入即注册，保证 SDK 层可直接 list/invoke
register_clean_capabilities()
