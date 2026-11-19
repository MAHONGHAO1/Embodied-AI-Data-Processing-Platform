"""Pluggable source adapters (contract v1)."""

from .contract import (ADAPTER_CONTRACT_VERSION, AdapterDescriptor, AdapterError, Detection,
                       Preflight, PreparedInput, SourceAdapter, SourceChanged,
                       UnsupportedSource, UnsupportedVersion, rejected, supported)
from .registry import (AdapterRegistry, REGISTRY, adapter_ids, adapters, best, descriptors,
                       detect, get_adapter, register, unregister)

__all__ = ["ADAPTER_CONTRACT_VERSION", "AdapterDescriptor", "AdapterError", "AdapterRegistry",
           "Detection", "Preflight", "PreparedInput", "REGISTRY", "SourceAdapter", "SourceChanged",
           "UnsupportedSource", "UnsupportedVersion", "adapter_ids", "adapters", "best",
           "descriptors", "detect", "get_adapter", "register", "rejected", "supported", "unregister"]
