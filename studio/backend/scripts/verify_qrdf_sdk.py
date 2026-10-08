#!/usr/bin/env python3
"""Fail fast when a vendored QRDF SDK lacks QuicData's v0.2 API surface."""

from __future__ import annotations

import argparse
import sys
from dataclasses import fields
from pathlib import Path

REQUIRED_MESSAGE_FIELDS = frozenset(
    {
        "topic",
        "schema_name",
        "canonical_schema_name",
        "descriptor_format",
        "is_legacy_schema",
        "log_time",
        "publish_time",
        "message",
    }
)


def verify_qrdf_sdk(vendor_dir: Path) -> str:
    """Import and validate the exact QRDF API used by QuicData."""
    package_dir = vendor_dir / "qrdf"
    if not package_dir.is_dir():
        raise RuntimeError(f"QRDF package directory is missing: {package_dir}")
    sys.path.insert(0, str(vendor_dir))
    try:
        import qrdf
        from qrdf.mcap.reader import McapEpisodeReader, McapMessage
        from qrdf.models.episode import EpisodeMetadata
    except Exception as exc:  # pragma: no cover - dependency-specific import failures
        raise RuntimeError(f"QRDF import failed: {type(exc).__name__}: {exc}") from exc

    try:
        message_fields = {field.name for field in fields(McapMessage)}
    except TypeError as exc:
        raise RuntimeError("QRDF McapMessage must be a dataclass") from exc
    missing_fields = sorted(REQUIRED_MESSAGE_FIELDS - message_fields)
    if missing_fields:
        raise RuntimeError(f"QRDF McapMessage is missing fields: {', '.join(missing_fields)}")
    if not callable(getattr(McapEpisodeReader, "get_canonical_schema_name", None)):
        raise RuntimeError("QRDF McapEpisodeReader is missing get_canonical_schema_name")
    if not callable(getattr(EpisodeMetadata, "resolve_data_file", None)):
        raise RuntimeError("QRDF EpisodeMetadata is missing resolve_data_file")
    return str(getattr(qrdf, "__version__", "unknown"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vendor-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        version = verify_qrdf_sdk(args.vendor_dir.resolve())
    except RuntimeError as exc:
        print(f"QRDF SDK verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"QRDF v0.2 API verified: {version} -> {args.vendor_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
