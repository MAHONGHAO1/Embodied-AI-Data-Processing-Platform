"""Pure helpers for immutable PublishedSample range and export sidecars."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def calculate_effective_range(
    *,
    core_start_ns: int,
    core_end_ns: int,
    target_start_ns: int,
    target_end_ns: int,
    pre_roll_s: float,
    post_roll_s: float,
) -> tuple[int, int]:
    """Return a half-open range clamped to the accepted annotation target."""
    if core_start_ns >= core_end_ns:
        raise ValueError("core range must be half-open and non-empty")
    if target_start_ns >= target_end_ns:
        raise ValueError("annotation target range is invalid")
    if core_start_ns < target_start_ns or core_end_ns > target_end_ns:
        raise ValueError("core range is outside annotation target")
    if pre_roll_s < 0 or post_roll_s < 0:
        raise ValueError("context window must be non-negative")
    start = max(target_start_ns, int(core_start_ns - pre_roll_s * 1e9))
    end = min(target_end_ns, int(core_end_ns + post_roll_s * 1e9))
    if start >= end:
        raise ValueError("effective range is empty")
    return start, end


def canonical_manifest_hash(value: Mapping[str, Any] | list[Any]) -> str:
    """Hash only canonical JSON values, never local paths or object handles."""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_export_sidecar(manifest: Mapping[str, Any], report: Mapping[str, Any]) -> dict[str, Any]:
    """Build a safe QuicData sidecar from a path-free revision manifest."""
    return {
        "schema": "quicdata.published_sample_export.v1",
        "revision_id": manifest.get("revision_id"),
        "dataset_id": manifest.get("dataset_id"),
        "workspace_id": manifest.get("workspace_id"),
        "version": manifest.get("version"),
        "items": _sanitize(manifest.get("items", [])),
        "report": _sanitize(report),
        "manifest_hash": canonical_manifest_hash(manifest),
    }


_FORBIDDEN_KEYS = {
    "source_qrdf_path",
    "package_uri",
    "storage_uri",
    "signed_url",
    "access_key",
    "secret_key",
    "secret",
    "credential",
}


def _is_forbidden_sidecar_key(key: object) -> bool:
    normalized = str(key).strip().lower()
    return (
        normalized in _FORBIDDEN_KEYS
        or normalized in {"path", "uri", "url", "signature"}
        or normalized.endswith(("_path", "_uri", "_url"))
        or "access_key" in normalized
        or "secret" in normalized
        or "credential" in normalized
        or "signature" in normalized
    )


def _sanitize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize(item)
            for key, item in value.items()
            if not _is_forbidden_sidecar_key(key)
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item) for item in value]
    return value
