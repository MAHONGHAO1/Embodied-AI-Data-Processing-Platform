"""QuicStudio environment-suffixed logical bucket configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from data.infra.object_storage import BucketRole

VALID_ENVIRONMENTS = frozenset({"dev", "test", "uat", "prod"})
BUCKET_ROLES: tuple[BucketRole, ...] = ("raw", "process", "export")


class BucketConfigError(ValueError):
    """Raised when storage configuration would create an unsupported bucket."""


def normalize_environment(value: str) -> str:
    environment = str(value or "").strip().lower()
    if environment in {"development", "develop"}:
        environment = "dev"
    elif environment in {"production", "production-like"}:
        environment = "prod"
    if environment not in VALID_ENVIRONMENTS:
        raise BucketConfigError(f"storage environment must be one of {sorted(VALID_ENVIRONMENTS)}")
    return environment


def bucket_name(environment: str, role: BucketRole) -> str:
    environment = normalize_environment(environment)
    if role not in BUCKET_ROLES:
        raise BucketConfigError(f"unsupported QuicStudio bucket role: {role}")
    return f"quicstudio-{environment}-{role}"


@dataclass(frozen=True)
class StorageBucketConfig:
    environment: str
    buckets: Mapping[BucketRole, str]

    @classmethod
    def for_environment(cls, environment: str) -> StorageBucketConfig:
        normalized = normalize_environment(environment)
        return cls(
            environment=normalized,
            buckets={role: bucket_name(normalized, role) for role in BUCKET_ROLES},
        )

    @classmethod
    def from_overrides(
        cls,
        environment: str,
        overrides: Mapping[str, str] | None = None,
    ) -> StorageBucketConfig:
        base = cls.for_environment(environment)
        values = dict(base.buckets)
        for role in BUCKET_ROLES:
            override = str((overrides or {}).get(role) or "").strip()
            if override:
                values[role] = override
        rejected = sorted(set(overrides or {}) - set(BUCKET_ROLES))
        if rejected:
            raise BucketConfigError(f"unsupported bucket roles: {', '.join(rejected)}")
        return cls(environment=base.environment, buckets=values)
