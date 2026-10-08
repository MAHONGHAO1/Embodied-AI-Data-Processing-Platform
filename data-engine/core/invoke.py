"""
SDK 统一 invoke 调度层。
根据能力声明懒加载目标类/方法并执行，返回算子结构化结果。
严禁在本模块顶层导入领域重型依赖。
支持将构造参数从 invoke kwargs 中剥离。
"""
from __future__ import annotations

import importlib
import inspect
from typing import Any, Dict, List, Optional, Tuple

from quic_op.core.capability import (
    CapabilitySpec,
    get_capability,
    list_capabilities,
    register_capability,
)
from quic_op.core.exceptions import CapabilityInvokeException


def _load_symbol(target: str):
    """
    解析 ``module.path:Symbol`` 形式的入口。
    """
    if ":" not in target:
        raise CapabilityInvokeException(
            f"非法能力入口 target，应为 module.path:Symbol，收到: {target}"
        )
    module_path, symbol_name = target.split(":", 1)
    try:
        module = importlib.import_module(module_path)
        return getattr(module, symbol_name)
    except Exception as exc:
        raise CapabilityInvokeException(
            f"无法加载能力入口 {target}: {exc}"
        ) from exc


def _split_init_and_call_params(
    cls: type, params: Dict[str, Any]
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    将 kwargs 拆成构造参数与方法参数。
    仅剥离 ``__init__`` 显式声明的参数名（不含 ``self`` / ``**kwargs``）。
    """
    signature = inspect.signature(cls.__init__)
    init_names = set()
    for name, param in signature.parameters.items():
        if name == "self":
            continue
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        init_names.add(name)

    init_kwargs = {key: params[key] for key in list(params) if key in init_names}
    call_kwargs = {key: value for key, value in params.items() if key not in init_names}
    return init_kwargs, call_kwargs


def invoke(operator_id: str, /, **params: Any) -> Any:
    """
    统一调用已声明的算子能力。

    :param operator_id: 能力 ID，如 ``perception.hand_pose_yolo`` ``clean.quality_lowlight``
    :param params: 透传参数，包含关键字参数（如 ``video_path``、``output_dir``）
    :return: 算子结构化返回值（通常含 ``json_path``）
    """
    spec = get_capability(operator_id)
    symbol = _load_symbol(spec.target)
    call_params = dict(params)

    try:
        if inspect.isclass(symbol):
            init_kwargs, call_kwargs = _split_init_and_call_params(symbol, call_params)
            instance = symbol(**init_kwargs)
            method = getattr(instance, spec.method)
            return method(**call_kwargs)

        if callable(symbol):
            if spec.method and hasattr(symbol, spec.method):
                bound = getattr(symbol, spec.method)
                return bound(**call_params)
            return symbol(**call_params)

        method = getattr(symbol, spec.method)
        return method(**call_params)

    try:
        if inspect.isclass(symbol):
            instance = symbol()
            method = getattr(instance, spec.method)
            return method(**params)

        if callable(symbol):
            # 若 target 直接指向函数，忽略 method；否则视为工厂/命名空间
            if spec.method and hasattr(symbol, spec.method):
                bound = getattr(symbol, spec.method)
                return bound(**params)
            return symbol(**params)

        method = getattr(symbol, spec.method)
        return method(**params)
    except TypeError as exc:
        raise CapabilityInvokeException(
            f"调用 {operator_id} ({spec.target}.{spec.method}) 参数错误: {exc}"
        ) from exc
    except CapabilityInvokeException:
        raise
    except Exception as exc:
        raise CapabilityInvokeException(
            f"调用 {operator_id} 失败: {exc}"
        ) from exc


def describe_capabilities(
    domain: Optional[str] = None,
    tag: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """返回能力声明的字典列表，便于平台/编排层消费。"""
    return [spec.to_dict() for spec in list_capabilities(domain=domain, tag=tag)]


__all__ = [
    "invoke",
    "describe_capabilities",
    "get_capability",
    "list_capabilities",
    "register_capability",
    "CapabilitySpec",
]
