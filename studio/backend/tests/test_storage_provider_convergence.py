"""Storage converges on MinIO or Aliyun OSS only."""

import pytest

from data.services.storage_mode import (
    apply_storage_mode_derived_fields,
    normalize_storage_mode,
    validate_storage_mode_payload,
)


def test_legacy_storage_modes_are_rejected():
    for legacy in ("local", "cloud", "hybrid", "nas", "file", "filesystem"):
        with pytest.raises(ValueError):
            normalize_storage_mode(legacy)


def test_supported_providers_are_accepted():
    assert normalize_storage_mode("minio") == "minio"
    assert normalize_storage_mode("MINIO") == "minio"
    assert normalize_storage_mode("aliyun_oss") == "aliyun_oss"
    assert normalize_storage_mode("aliyun-oss") == "aliyun_oss"
    assert normalize_storage_mode("oss") == "aliyun_oss"
    assert normalize_storage_mode(None) == "minio"


def test_settings_payload_rejects_legacy_storage_mode():
    with pytest.raises(ValueError):
        validate_storage_mode_payload({"storage_mode": "hybrid"})

    accepted = validate_storage_mode_payload({"storage_mode": "aliyun_oss"})
    assert accepted["storage_mode"] == "aliyun_oss"


def test_derived_fields_follow_the_provider(monkeypatch):
    from data.config import settings

    monkeypatch.setattr(settings, "storage_provider", "aliyun-oss", raising=False)
    derived = apply_storage_mode_derived_fields({})

    assert derived["storage_mode"] == "aliyun_oss"
    assert derived["provider"] == "aliyun_oss"
    assert derived["uri_scheme"] == "oss"
    assert derived["cloud_enabled"] is True
    assert derived["keep_local_cache"] is False
