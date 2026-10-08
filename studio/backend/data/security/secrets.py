"""Local and extensible envelope encryption and key management."""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import Any, Protocol

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("quicdata.security")

ENC_MARKER = "__enc__"
CURRENT_KEY_VERSION = 1


class SecretProvider(Protocol):
    def encrypt(self, plaintext: str, *, key_version: int | None = None) -> dict[str, Any]: ...

    def decrypt(self, envelope: dict[str, Any] | str) -> str: ...


def _derive_fernet_key(secret: str, version: int = CURRENT_KEY_VERSION) -> bytes:
    """Derive Fernet key from application SECRET_KEY (urlsafe base64 32 bytes)."""
    material = hashlib.sha256(f"quicdata:kek:v{version}:{secret}".encode()).digest()
    return base64.urlsafe_b64encode(material)


class LocalSecretProvider:
    """Development/standalone: derive KEK using SECRET_KEY, wrap plaintext with Fernet."""

    def __init__(self, master_secret: str) -> None:
        if not master_secret:
            raise ValueError("master_secret required")
        self._master = master_secret
        self._fernets: dict[int, Fernet] = {
            CURRENT_KEY_VERSION: Fernet(_derive_fernet_key(master_secret, CURRENT_KEY_VERSION))
        }

    def encrypt(self, plaintext: str, *, key_version: int | None = None) -> dict[str, Any]:
        version = key_version or CURRENT_KEY_VERSION
        if version not in self._fernets:
            self._fernets[version] = Fernet(_derive_fernet_key(self._master, version))
        token = self._fernets[version].encrypt(plaintext.encode("utf-8")).decode("ascii")
        return {ENC_MARKER: True, "v": version, "ciphertext": token}

    def decrypt(self, envelope: dict[str, Any] | str) -> str:
        if isinstance(envelope, str):
            # Plaintext compatibility: legacy fields not yet encrypted
            return envelope
        if not isinstance(envelope, dict) or not envelope.get(ENC_MARKER):
            raise ValueError("invalid envelope")
        version = int(envelope.get("v") or CURRENT_KEY_VERSION)
        if version not in self._fernets:
            self._fernets[version] = Fernet(_derive_fernet_key(self._master, version))
        try:
            raw = self._fernets[version].decrypt(envelope["ciphertext"].encode("ascii"))
        except (InvalidToken, KeyError, TypeError) as exc:
            raise ValueError("decrypt failed") from exc
        return raw.decode("utf-8")


class KeyManager:
    """Unified entry point: encrypt_field / decrypt_field / wrap_mapping."""

    def __init__(self, provider: SecretProvider) -> None:
        self.provider = provider

    def encrypt_field(self, value: str | None) -> Any:
        if value is None or value == "":
            return value
        if isinstance(value, dict) and value.get(ENC_MARKER):
            return value
        return self.provider.encrypt(str(value))

    def decrypt_field(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, dict) and value.get(ENC_MARKER):
            return self.provider.decrypt(value)
        return str(value)

    def encrypt_oss_block(self, oss: dict[str, Any]) -> dict[str, Any]:
        out = dict(oss or {})
        if "access_key_secret" in out and out["access_key_secret"]:
            out["access_key_secret"] = self.encrypt_field(out["access_key_secret"])
        return out

    def decrypt_oss_block(self, oss: dict[str, Any]) -> dict[str, Any]:
        out = dict(oss or {})
        if "access_key_secret" in out:
            try:
                out["access_key_secret"] = self.decrypt_field(out["access_key_secret"])
            except ValueError:
                logger.warning("OSS secret 解密失败，保留原值结构")
        return out


_key_manager: KeyManager | None = None


def get_key_manager() -> KeyManager:
    global _key_manager
    if _key_manager is None:
        from data.security.kms import build_key_manager

        _key_manager = build_key_manager()
    return _key_manager


def reset_key_manager_for_tests() -> None:
    global _key_manager
    _key_manager = None
