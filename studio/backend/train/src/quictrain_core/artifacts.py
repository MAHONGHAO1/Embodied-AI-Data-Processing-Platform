"""Artifact kind enumeration and filename inference (V1.1 Workstream L)."""

from __future__ import annotations

from typing import Literal

ArtifactKind = Literal[
    "config",
    "checkpoint",
    "log",
    "summary",
    "manifest",
    "metadata",
    "export",
]

ARTIFACT_KINDS: frozenset[str] = frozenset(
    {"config", "checkpoint", "log", "summary", "manifest", "metadata", "export"}
)


def infer_artifact_kind(name: str) -> ArtifactKind:
    """Map artifact file name to a stable kind. Unknown suffixes → metadata (never checkpoint)."""

    lower = name.lower()
    if lower.endswith(".safetensors"):
        return "checkpoint"
    if lower.endswith(".log"):
        return "log"
    if lower == "summary.json":
        return "summary"
    if lower in {"artifact_manifest.json", "manifest.json"} or lower.endswith("manifest.json"):
        return "manifest"
    if lower.endswith("config.json") or lower in {
        "resolved_config.json",
        "source.json",
        "environment.json",
    }:
        return "config"
    if lower.startswith("export") and lower.endswith(".json"):
        return "export"
    return "metadata"
