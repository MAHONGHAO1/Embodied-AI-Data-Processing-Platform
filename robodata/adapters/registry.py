"""Adapter discovery and selection.

Built-ins are registered explicitly so tests and offline installs never depend
on packaging metadata. Third-party adapters can register through the
``robodata.adapters`` entry-point group or by calling ``register``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .contract import (ADAPTER_CONTRACT_VERSION, AdapterError, Detection, SourceAdapter,
                       UnsupportedSource)

_BUILTIN_MODULES = (
    "robodata.adapters.builtin.hdf5",
    "robodata.adapters.builtin.so100",
    "robodata.adapters.builtin.aloha",
    "robodata.adapters.builtin.matching_cable",
)


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, SourceAdapter] = {}
        self._loaded_entry_points = False

    def register(self, adapter: SourceAdapter, *, replace: bool = False) -> None:
        adapter_id = adapter.descriptor.adapter_id
        if not adapter_id or adapter_id != adapter.descriptor.source_kind:
            # The storage key and the persisted source kind must stay identical,
            # otherwise a batch can no longer be routed after a restart.
            raise ValueError("adapter_id 必须与 descriptor.source_kind 一致")
        if adapter_id in self._adapters and not replace:
            raise ValueError(f"适配器已注册：{adapter_id}")
        self._adapters[adapter_id] = adapter

    def unregister(self, adapter_id: str) -> None:
        self._adapters.pop(adapter_id, None)

    def get(self, adapter_id: str) -> SourceAdapter:
        self._load_entry_points()
        adapter = self._adapters.get(adapter_id)
        if adapter is None:
            known = ", ".join(sorted(self._adapters)) or "(none)"
            raise UnsupportedSource(f"未知数据源适配器 {adapter_id!r}；已注册：{known}")
        return adapter

    def ids(self) -> list[str]:
        self._load_entry_points()
        return sorted(self._adapters)

    def adapters(self) -> list[SourceAdapter]:
        self._load_entry_points()
        return [self._adapters[key] for key in sorted(self._adapters)]

    def detect(self, path: Path, *, test_label: str | None = None) -> list[Detection]:
        path = Path(path)
        results = []
        for adapter in self.adapters():
            try:
                results.append(adapter.detect(path, test_label=test_label))
            except Exception as exc:  # a broken third-party adapter must not hide the others
                results.append(Detection(adapter_id=adapter.descriptor.adapter_id, supported=False,
                                         confidence=0.0, unsupported_reason=f"检测器异常：{exc}"))
        return sorted(results, key=lambda item: (item.supported, item.confidence), reverse=True)

    def best(self, path: Path, *, test_label: str | None = None) -> Detection:
        results = self.detect(path, test_label=test_label)
        for result in results:
            if result.supported:
                return result
        reasons = "；".join(f"{item.adapter_id}: {item.unsupported_reason}" for item in results
                           if item.unsupported_reason)
        raise UnsupportedSource(f"没有适配器支持该输入。{reasons}")

    def _load_entry_points(self) -> None:
        if self._loaded_entry_points:
            return
        self._loaded_entry_points = True
        try:
            from importlib.metadata import entry_points
            for entry in entry_points(group="robodata.adapters"):
                try:
                    adapter = entry.load()
                    if isinstance(adapter, type):
                        adapter = adapter()
                    self.register(adapter)
                except Exception:
                    # An optional third-party adapter must not break the built-ins.
                    continue
        except Exception:
            return


REGISTRY = AdapterRegistry()
for module_name in _BUILTIN_MODULES:
    module = __import__(module_name, fromlist=["adapter"])
    REGISTRY.register(module.adapter)


def register(adapter: SourceAdapter, *, replace: bool = False) -> None:
    REGISTRY.register(adapter, replace=replace)


def unregister(adapter_id: str) -> None:
    REGISTRY.unregister(adapter_id)


def adapter_ids() -> list[str]:
    return REGISTRY.ids()


def adapters() -> list[SourceAdapter]:
    return REGISTRY.adapters()


def get_adapter(adapter_id: str) -> SourceAdapter:
    return REGISTRY.get(adapter_id)


def detect(path: Path, *, test_label: str | None = None) -> list[dict]:
    return [item.to_dict() for item in REGISTRY.detect(path, test_label=test_label)]


def best(path: Path, *, test_label: str | None = None) -> dict:
    return REGISTRY.best(path, test_label=test_label).to_dict()


def descriptors() -> list[dict]:
    return [adapter.descriptor.to_dict() for adapter in adapters()]


__all__ = ["ADAPTER_CONTRACT_VERSION", "AdapterError", "AdapterRegistry", "REGISTRY",
           "adapter_ids", "adapters", "best", "descriptors", "detect", "get_adapter",
           "register", "unregister"]
