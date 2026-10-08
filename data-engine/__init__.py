"""
quic_op SDK
具身智能数据处理流水线企业级算子库

发行版本由此处提供；pyproject.toml 通过 dynamic version 引用本变量。
统一能力声明与调度：``list_capabilities`` / ``invoke``。
发行版本由此处提供；clean 独立包发行版本见 ``quic_op.clean.__version__``。
统一能力声明与调度：``list_capabilities`` / ``invoke``。
"""

__version__ = "0.1.0"

from quic_op.core.capability import (
    CapabilitySpec,
    get_capability,
    list_capabilities,
    register_capability,
)
from quic_op.core.invoke import describe_capabilities, invoke

# 导入 perception 以完成人手 Pose 能力注册
from quic_op import perception as _perception  # noqa: F401
# 导入 clean 以完成当前已发布能力的注册（低光/模糊/掉帧）
from quic_op import clean as _clean  # noqa: F401

__all__ = [
    "__version__",
    "CapabilitySpec",
    "register_capability",
    "get_capability",
    "list_capabilities",
    "describe_capabilities",
    "invoke",
]
