"""Configuration secret migration utility interface (env <-> runtime_config envelope).

Full Cloud KMS migration to be extended later; local envelope migration is ready for immediate use.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger("quicdata.security")


@dataclass
class MigrationResult:
    ok: bool
    message: str
    changed_fields: list[str] = field(default_factory=list)
    dry_run: bool = True
    details: dict[str, Any] = field(default_factory=dict)


class SecretMigrationTool(Protocol):
    def migrate(self, *, dry_run: bool = True) -> MigrationResult: ...


class LocalRuntimeSecretMigration:
    """Upgrade plaintext OSS secrets in runtime_config.json to local envelope ciphertext, or inject from env."""

    def __init__(self, config_path: Path | None = None) -> None:
        from data.config import _RUNTIME_CONFIG_PATH, BASE_DIR

        self.config_path = config_path or _RUNTIME_CONFIG_PATH
        self.base_dir = BASE_DIR

    def migrate(self, *, dry_run: bool = True) -> MigrationResult:
        from data.security.secrets import ENC_MARKER, get_key_manager

        if not self.config_path.is_file():
            return MigrationResult(ok=True, message="no runtime_config.json", dry_run=dry_run)

        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        storage = raw.get("storage") or {}
        oss = dict(storage.get("oss") or {})
        secret = oss.get("access_key_secret")
        changed: list[str] = []

        if isinstance(secret, str) and secret:
            env = get_key_manager().encrypt_field(secret)
            oss["access_key_secret"] = env
            changed.append("storage.oss.access_key_secret")
            storage["oss"] = oss
            raw["storage"] = storage
            if not dry_run:
                self.config_path.write_text(
                    json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            return MigrationResult(
                ok=True,
                message="encrypted plaintext oss secret"
                if not dry_run
                else "would encrypt plaintext oss secret",
                changed_fields=changed,
                dry_run=dry_run,
            )

        if isinstance(secret, dict) and secret.get(ENC_MARKER):
            return MigrationResult(
                ok=True,
                message="already encrypted",
                dry_run=dry_run,
                details={"v": secret.get("v")},
            )

        return MigrationResult(ok=True, message="nothing to migrate", dry_run=dry_run)


class CloudKmsMigrationTool:
    """Placeholder: migrate local envelope DEK/fields to Cloud KMS wrapping."""

    def migrate(self, *, dry_run: bool = True) -> MigrationResult:
        return MigrationResult(
            ok=False,
            message="Cloud KMS migration not implemented — extend CloudKmsMigrationTool.migrate()",
            dry_run=dry_run,
            details={
                "hint": "re-encrypt LocalSecretProvider envelopes with CloudKmsSecretProvider"
            },
        )


def get_migration_tool(kind: str = "local") -> SecretMigrationTool:
    kind = (kind or "local").strip().lower()
    if kind in {"local", "runtime", "envelope"}:
        return LocalRuntimeSecretMigration()
    if kind in {"cloud_kms", "kms"}:
        return CloudKmsMigrationTool()
    raise ValueError(f"unknown migration kind: {kind}")
