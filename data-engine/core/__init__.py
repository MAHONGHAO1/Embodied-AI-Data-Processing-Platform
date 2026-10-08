"""
基座库：导出对外接口。
"""
__version__ = "0.1.0"

from .data_types import (
    CameraIntrinsics,
    HandMesh3D,
    HandDetectionResult,
    HandPoseOperatorResult,
    QRDFEpisode,
    DEFAULT_COORDINATE_SYSTEM,
    MAX_JOINTS_COUNT,
    HAND_POSE_OPERATOR_ID,
    LowLightSample,
    BlurSample,
    FrameDropEvent,
    LowLightOperatorResult,
    BlurOperatorResult,
    FrameDropOperatorResult,
    QRDFEpisode,
    DEFAULT_COORDINATE_SYSTEM,
    MAX_JOINTS_COUNT,
    LOWLIGHT_OPERATOR_ID,
    BLUR_OPERATOR_ID,
    FRAME_DROP_OPERATOR_ID,
)
from .config_manager import ConfigManager
from .config_manager import global_config
from .persistence import JsonResultPersister, PERSISTENCE_SCHEMA_VERSION
from .capability import (
    CapabilitySpec,
    CapabilityRegistry,
    global_registry,
    register_capability,
    get_capability,
    list_capabilities,
)
from .invoke import invoke, describe_capabilities
from .exceptions import (
    QuicOperatorException,
    VideoCorruptedException,
    ModelWeightNotFoundException,
    SchemaValidationException,
    ExporterException,
    CapabilityNotFoundException,
    CapabilityInvokeException,
)

__all__ = [
    "__version__",
    "CameraIntrinsics",
    "HandMesh3D",
    "HandDetectionResult",
    "HandPoseOperatorResult",
    "QRDFEpisode",
    "DEFAULT_COORDINATE_SYSTEM",
    "MAX_JOINTS_COUNT",
    "HAND_POSE_OPERATOR_ID",
    "LowLightSample",
    "BlurSample",
    "FrameDropEvent",
    "LowLightOperatorResult",
    "BlurOperatorResult",
    "FrameDropOperatorResult",
    "QRDFEpisode",
    "DEFAULT_COORDINATE_SYSTEM",
    "MAX_JOINTS_COUNT",
    "LOWLIGHT_OPERATOR_ID",
    "BLUR_OPERATOR_ID",
    "FRAME_DROP_OPERATOR_ID",
    "ConfigManager",
    "global_config",
    "JsonResultPersister",
    "PERSISTENCE_SCHEMA_VERSION",
    "CapabilitySpec",
    "CapabilityRegistry",
    "global_registry",
    "register_capability",
    "get_capability",
    "list_capabilities",
    "invoke",
    "describe_capabilities",
    "QuicOperatorException",
    "VideoCorruptedException",
    "ModelWeightNotFoundException",
    "SchemaValidationException",
    "ExporterException",
    "CapabilityNotFoundException",
    "CapabilityInvokeException",
]
