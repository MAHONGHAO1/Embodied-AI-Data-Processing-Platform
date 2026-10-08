"""
感知库：导出对外接口。
"""
__version__ = "0.1.0"

from .mesh import HandReconstructor
from .depth import DepthEstimator
from .physics import KinematicsAnalyzer
from .detecthands import HandDetector
from .detecthands import __version__ as HAND_POSE_VERSION
from . import capabilities as _capabilities  # noqa: F401  # 注册 SDK 能力声明

__all__ = [
    "__version__",
    "HAND_POSE_VERSION",
    "HandReconstructor",
    "DepthEstimator",
    "KinematicsAnalyzer",
    "HandDetector",
]
