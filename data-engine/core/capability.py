"""
SDK 能力声明与注册表。
仅依赖标准库，供各领域库声明可被统一 invoke 调度的算子能力。
严禁在本模块引入 torch / cv2 等重型依赖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from quic_op.core.exceptions import CapabilityNotFoundException


@dataclass(frozen=True)
class CapabilitySpec:
    """
    单条算子能力声明。
    """
    operator_id: str
    domain: str
    name: str
    description: str
    target: str
    method: str
    version: str = "0.1.0"
    input_kind: str = "video_path"
    output_kind: str = "operator_result"
    persist_json: bool = True
    tags: tuple = ()
    params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "operator_id": self.operator_id,
            "domain": self.domain,
            "name": self.name,
            "description": self.description,
            "target": self.target,
            "method": self.method,
            "version": self.version,
            "input_kind": self.input_kind,
            "output_kind": self.output_kind,
            "persist_json": self.persist_json,
            "tags": list(self.tags),
            "params": dict(self.params),
        }


class CapabilityRegistry:
    """
    进程内能力注册表（单例用法见 module 级 ``global_registry``）。
    """

    def __init__(self):
        self._capabilities: Dict[str, CapabilitySpec] = {}

    def register(self, spec: CapabilitySpec, *, overwrite: bool = False) -> None:
        if not overwrite and spec.operator_id in self._capabilities:
            existing = self._capabilities[spec.operator_id]
            if existing == spec:
                return
            raise ValueError(
                f"能力已注册且内容不同: {spec.operator_id} "
                f"(existing={existing.target}.{existing.method})"
            )
        self._capabilities[spec.operator_id] = spec

    def get(self, operator_id: str) -> CapabilitySpec:
        try:
            return self._capabilities[operator_id]
        except KeyError as exc:
            raise CapabilityNotFoundException(
                f"未声明的能力: {operator_id}"
            ) from exc

    def has(self, operator_id: str) -> bool:
        return operator_id in self._capabilities

    def list(
        self,
        domain: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> List[CapabilitySpec]:
        items = list(self._capabilities.values())
        if domain:
            items = [c for c in items if c.domain == domain]
        if tag:
            items = [c for c in items if tag in c.tags]
        return sorted(items, key=lambda c: c.operator_id)

    def clear(self) -> None:
        self._capabilities.clear()


# 全局注册表
global_registry = CapabilityRegistry()


def register_capability(spec: CapabilitySpec, *, overwrite: bool = False) -> None:
    """向全局注册表声明一条能力。"""
    global_registry.register(spec, overwrite=overwrite)


def get_capability(operator_id: str) -> CapabilitySpec:
    """按 operator_id 查询能力声明。"""
    return global_registry.get(operator_id)


def list_capabilities(
    domain: Optional[str] = None,
    tag: Optional[str] = None,
) -> List[CapabilitySpec]:
    """列出已声明能力。"""
    return global_registry.list(domain=domain, tag=tag)
