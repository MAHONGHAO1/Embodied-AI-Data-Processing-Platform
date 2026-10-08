from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class ProviderState(StrEnum):
    PENDING = "PENDING"
    PROVISIONING = "PROVISIONING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    job_id: str
    attempt_id: str
    idempotency_key: str
    image_uri: str
    image_digest: str
    command: tuple[str, ...]
    environment: dict[str, str]
    mounts: tuple[dict[str, str], ...]
    resource: dict[str, object]
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderJob:
    external_id: str
    state: ProviderState
    raw_status: str
    message: str | None = None
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class LogLine:
    sequence: int
    timestamp: str
    level: str
    source: str
    message: str


class ComputeProvider(Protocol):
    def submit(self, spec: LaunchSpec) -> ProviderJob: ...

    def get(self, external_id: str) -> ProviderJob: ...

    def find_by_idempotency_key(self, key: str) -> ProviderJob | None: ...

    def cancel(self, external_id: str) -> ProviderJob: ...

    def get_logs(self, external_id: str, cursor: int = 0) -> tuple[list[LogLine], int]: ...

    def get_dashboard_url(self, external_id: str) -> str | None: ...
