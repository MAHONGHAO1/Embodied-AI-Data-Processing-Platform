"""
合规库：暴露对外的接口。
"""
__version__ = "0.1.0"

from .image_redact import ImageRedactor

__all__ = [
    "__version__",
    "ImageRedactor",
]
