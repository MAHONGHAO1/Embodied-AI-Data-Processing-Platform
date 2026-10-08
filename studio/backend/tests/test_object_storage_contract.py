from __future__ import annotations

import pytest

from data.services.storage_bucket_config import (
    BUCKET_ROLES,
    BucketConfigError,
    StorageBucketConfig,
    bucket_name,
    normalize_environment,
)


def test_bucket_names_use_environment_suffix_and_only_three_roles():
    config = StorageBucketConfig.for_environment("development")

    assert config.environment == "dev"
    assert tuple(config.buckets) == BUCKET_ROLES
    assert config.buckets == {
        "raw": "quicstudio-dev-raw",
        "process": "quicstudio-dev-process",
        "export": "quicstudio-dev-export",
    }


def test_bucket_config_rejects_official_and_unknown_roles():
    with pytest.raises(BucketConfigError):
        bucket_name("prod", "official")  # type: ignore[arg-type]

    with pytest.raises(BucketConfigError, match="unsupported bucket roles"):
        StorageBucketConfig.from_overrides("test", {"official": "legacy"})


def test_bucket_config_accepts_explicit_role_overrides_without_adding_roles():
    config = StorageBucketConfig.from_overrides(
        "uat",
        {"raw": "company-uat-raw", "process": "company-uat-process"},
    )

    assert config.buckets == {
        "raw": "company-uat-raw",
        "process": "company-uat-process",
        "export": "quicstudio-uat-export",
    }


def test_storage_environment_rejects_legacy_or_empty_values():
    assert normalize_environment("production") == "prod"
    for value in ("", "local", "staging", "official"):
        with pytest.raises(BucketConfigError):
            normalize_environment(value)
