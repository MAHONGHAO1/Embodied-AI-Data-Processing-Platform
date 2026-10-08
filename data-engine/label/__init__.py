"""
标注库：导出对外接口。
"""
__version__ = "0.1.0"

from .vlm_client import VLMClient
from .prompt_builder import PromptBuilder

__all__ = [
    "__version__",
    "VLMClient",
    "PromptBuilder",
]
