"""The Adapter Contract v1.

An adapter answers four questions about a local input:

1. Is this input mine? (``detect``)
2. What does it contain, and is the version supported? (``preflight``)
3. How do I copy it into a managed, fingerprinted batch? (``prepare``)
4. How do I build the read-only preview and the inventory? (``preview`` / ``inventory``)

The core pipeline only depends on this module. A new input format must not
require edits to ``robodata/importing.py`` or the business workflow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ADAPTER_CONTRACT_VERSION = "1.0"

INPUT_FILE = "file"
INPUT_DIRECTORY = "directory"


class AdapterError(Exception):
    """Base class for adapter contract failures."""


class UnsupportedSource(AdapterError):
    """No adapter claims this input, or the input is structurally invalid."""


class UnsupportedVersion(UnsupportedSource):
    """The format is recognised but this version is not supported."""


class SourceChanged(AdapterError):
    """The managed copy no longer matches the fingerprint recorded at import."""


@dataclass(frozen=True)
class AdapterDescriptor:
    """Static, machine-readable declaration of what an adapter supports."""

    adapter_id: str
    label: str
    source_kind: str
    input_kind: str
    supported_versions: tuple[str, ...]
    pinned: bool
    supports_test_label: bool
    description: str
    inspection_only: bool = False

    def to_dict(self) -> dict:
        return {
            "contract_version": ADAPTER_CONTRACT_VERSION,
            "adapter_id": self.adapter_id,
            "label": self.label,
            "source_kind": self.source_kind,
            "input_kind": self.input_kind,
            "supported_versions": list(self.supported_versions),
            "pinned": self.pinned,
            "supports_test_label": self.supports_test_label,
            "description": self.description,
            "inspection_only": self.inspection_only,
        }


@dataclass
class Detection:
    """One adapter's answer to "is this input mine?"."""

    adapter_id: str
    supported: bool
    confidence: float
    detected_version: str | None = None
    reasons: list[str] = field(default_factory=list)
    unsupported_reason: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "adapter_id": self.adapter_id,
            "supported": self.supported,
            "confidence": round(float(self.confidence), 3),
            "detected_version": self.detected_version,
            "reasons": list(self.reasons),
            "unsupported_reason": self.unsupported_reason,
            "evidence": self.evidence,
        }


@dataclass
class Preflight:
    """Cheap structural facts, before any managed copy or preview is built."""

    adapter_id: str
    supported: bool
    detected_version: str | None
    summary: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    unsupported_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "adapter_id": self.adapter_id,
            "supported": self.supported,
            "detected_version": self.detected_version,
            "summary": self.summary,
            "warnings": list(self.warnings),
            "unsupported_reason": self.unsupported_reason,
        }


@dataclass
class PreparedInput:
    """The managed copy, ready to be fingerprinted and written to input_manifest."""

    files: list[dict]
    interpretation: dict


def supported(adapter_id: str, version: str | None, confidence: float,
              reasons: list[str], evidence: dict | None = None) -> Detection:
    return Detection(adapter_id=adapter_id, supported=True, confidence=confidence,
                     detected_version=version, reasons=reasons, evidence=evidence or {})


def rejected(adapter_id: str, reason: str, *, confidence: float = 0.0,
             version: str | None = None, reasons: list[str] | None = None,
             evidence: dict | None = None) -> Detection:
    return Detection(adapter_id=adapter_id, supported=False, confidence=confidence,
                     detected_version=version, reasons=list(reasons or []),
                     unsupported_reason=reason, evidence=evidence or {})


class SourceAdapter:
    """Base class documenting the contract; subclasses may override any method."""

    descriptor: AdapterDescriptor

    def detect(self, path: Path, *, test_label: str | None = None) -> Detection:
        raise NotImplementedError

    def preflight(self, path: Path, *, test_label: str | None = None) -> Preflight:
        detection = self.detect(path, test_label=test_label)
        return Preflight(adapter_id=self.descriptor.adapter_id, supported=detection.supported,
                         detected_version=detection.detected_version,
                         summary={}, warnings=list(detection.reasons),
                         unsupported_reason=detection.unsupported_reason)

    def prepare(self, request: dict, raw: Path, managed: Path) -> PreparedInput:
        raise NotImplementedError

    def preview(self, request: dict, managed: Path, run_dir: Path) -> dict:
        raise NotImplementedError

    def inventory(self, request: dict, managed: Path, run_dir: Path) -> dict:
        raise NotImplementedError
