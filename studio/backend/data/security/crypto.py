"""Cryptographic primitives re-export (see secrets.KeyManager for envelope encryption)."""

from data.security.secrets import (
    CURRENT_KEY_VERSION,
    ENC_MARKER,
    KeyManager,
    LocalSecretProvider,
    get_key_manager,
)

__all__ = [
    "CURRENT_KEY_VERSION",
    "ENC_MARKER",
    "KeyManager",
    "LocalSecretProvider",
    "get_key_manager",
]
