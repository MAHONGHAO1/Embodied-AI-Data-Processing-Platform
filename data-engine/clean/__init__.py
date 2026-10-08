"""
清洗库：导出对外接口。

发行版本由此处 ``__version__`` 提供；根目录 ``pyproject.toml`` 通过
``tool.setuptools.dynamic.version`` attr 引用，勿在 toml 中写死版本号。
"""
__version__ = "0.1.0"

from .video_editor import VideoEditor
from .geometry import GeometryProcessor
from .quality_inspector import QualityInspector
from .quality_inspector import __version__ as QUALITY_INSPECTOR_VERSION
from . import capabilities as _capabilities  # noqa: F401  # 注册 SDK 能力声明

__all__ = [
    "__version__",
    "VideoEditor",
    "GeometryProcessor",
    "QUALITY_INSPECTOR_VERSION",
    "VideoEditor",
    "GeometryProcessor",
    "QualityInspector",
]
