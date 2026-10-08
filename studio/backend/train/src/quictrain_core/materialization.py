"""Dataset materialization readiness states (immutable version content + mutable readiness)."""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlparse


def is_dataset_archive_uri(uri: str) -> bool:
    return urlparse(uri).path.lower().endswith((".tar.gz", ".tgz", ".tar", ".zip"))


class MaterializationState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class DatasetReadiness(StrEnum):
    """Side-channel readiness for an immutable dataset version."""

    REGISTERED = "REGISTERED"
    MATERIALIZING = "MATERIALIZING"
    READY = "READY"
    FAILED = "FAILED"
    DEPRECATED = "DEPRECATED"
    INVALID = "INVALID"


SCHEDULABLE_READINESS = frozenset({DatasetReadiness.READY.value})
MATERIALIZABLE_READINESS = frozenset(
    {DatasetReadiness.REGISTERED.value, DatasetReadiness.FAILED.value}
)
