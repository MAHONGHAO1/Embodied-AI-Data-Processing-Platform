"""Deterministic checksums for immutable file packages."""

from __future__ import annotations

import hashlib
from pathlib import Path


def tree_sha256(root: Path) -> str:
    """Hash a package tree, including paths, while rejecting unsafe entries."""
    digest = hashlib.sha256()
    for item in sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix()):
        if item.is_symlink():
            raise ValueError("package contains a symlink")
        if item.is_dir():
            continue
        if not item.is_file():
            raise ValueError("package contains an unsupported entry")
        relative = item.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with item.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def legacy_batch_tree_sha256(root: Path) -> str:
    """Read the checksum emitted by Batch publication before canonicalization.

    This algorithm is retained only for verifying immutable artifacts that were
    already published. New artifacts must always persist :func:`tree_sha256`.
    Callers must run ``tree_sha256`` first so unsafe filesystem entries are
    rejected before this compatibility digest is considered.
    """
    digest = hashlib.sha256()
    for item in sorted(
        path for path in root.rglob("*") if path.is_file() and not path.is_symlink()
    ):
        digest.update(item.relative_to(root).as_posix().encode("utf-8"))
        with item.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()
