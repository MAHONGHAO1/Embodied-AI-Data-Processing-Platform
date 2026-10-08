"""Key provider factory and Cloud KMS extension interface (currently defaults to Local; Cloud KMS is a placeholder)."""

from __future__ import annotations

import logging
from typing import Any

from data.security.secrets import (
    KeyManager,
    LocalSecretProvider,
    SecretProvider,
)

logger = logging.getLogger("quicdata.security")


class CloudKmsSecretProvider:
    """Cloud KMS envelope encryption extension point.

    Can integrate with Aliyun KMS / AWS KMS in the future: wrap local DEK with cloud KEK.
    Currently provides interface signature only; calling encrypt/decrypt will explicitly fail to prevent silent degradation.
    """

    provider_name = "cloud_kms"

    def __init__(self, *, key_id: str = "", region: str = "", endpoint: str = "") -> None:
        self.key_id = key_id
        self.region = region
        self.endpoint = endpoint

    def encrypt(self, plaintext: str, *, key_version: int | None = None) -> dict[str, Any]:
        raise NotImplementedError(
            "CloudKmsSecretProvider 尚未接入具体云 KMS；"
            "请设置 SECRET_PROVIDER=local，或实现 encrypt/decrypt 后注册。"
        )

    def decrypt(self, envelope: dict[str, Any] | str) -> str:
        raise NotImplementedError(
            "CloudKmsSecretProvider 尚未接入具体云 KMS；"
            "请设置 SECRET_PROVIDER=local，或实现 encrypt/decrypt 后注册。"
        )

    def healthcheck(self) -> dict[str, Any]:
        return {
            "provider": self.provider_name,
            "configured": bool(self.key_id),
            "ready": False,
            "message": "stub only — wire real KMS client here",
        }


def build_secret_provider(
    *,
    provider_name: str | None = None,
    master_secret: str | None = None,
) -> SecretProvider:
    """Build SecretProvider according to configuration."""
    from data.config import settings

    name = (
        (provider_name or getattr(settings, "secret_provider", "local") or "local").strip().lower()
    )
    secret = master_secret if master_secret is not None else settings.secret_key
    if name in {"local", "fernet", "dev"}:
        return LocalSecretProvider(secret)
    if name in {"cloud_kms", "kms", "aliyun_kms", "aws_kms"}:
        return CloudKmsSecretProvider(
            key_id=getattr(settings, "kms_key_id", "") or "",
            region=getattr(settings, "kms_region", "") or "",
            endpoint=getattr(settings, "kms_endpoint", "") or "",
        )
    raise ValueError(f"unknown SECRET_PROVIDER: {name}")


def build_key_manager(*, provider_name: str | None = None) -> KeyManager:
    return KeyManager(build_secret_provider(provider_name=provider_name))
