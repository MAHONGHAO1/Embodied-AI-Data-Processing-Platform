"""Persistent, admin-managed external import and AI runtime policy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from sqlalchemy.orm import Session

from data.config import _is_public_oss_browser_endpoint, settings
from data.database import ExternalOssImportScope, PlatformAiSetting, TaskSet, Workspace
from data.security.secrets import get_key_manager


class PlatformSettingsError(ValueError):
    pass


class PlatformSettingsConflict(PlatformSettingsError):
    pass


_OSS_BUCKET_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?")


@dataclass(frozen=True)
class BehaviorAiRuntimeConfig:
    enabled: bool
    outbound_enabled: bool
    signed_url_delivery_enabled: bool
    public_endpoint: str
    signed_url_ttl_seconds: int
    api_key: str
    app_id: str
    provider_user_id: str
    provider_device_uuid: str
    annotation_version: str
    sdk_dir: str
    checksums_file: str

    @property
    def can_call_provider(self) -> bool:
        return bool(
            self.enabled
            and self.outbound_enabled
            and self.signed_url_delivery_enabled
            and self.public_endpoint
            and self.api_key
            and self.app_id
            and not settings.test_mode
        )

    @property
    def dashscope_api_key(self) -> str:
        return self.api_key

    @property
    def dashscope_app_id(self) -> str:
        return self.app_id

    @property
    def dashscope_user_id(self) -> str:
        return self.provider_user_id

    @property
    def dashscope_device_uuid(self) -> str:
        return self.provider_device_uuid

    @property
    def vla_anno_version(self) -> str:
        return self.annotation_version

    @property
    def embodied_vl_sdk_dir(self) -> str:
        return self.sdk_dir

    @property
    def embodied_vl_checksums_file(self) -> str:
        return self.checksums_file


def _env_ai_runtime() -> BehaviorAiRuntimeConfig:
    return BehaviorAiRuntimeConfig(
        enabled=bool(settings.behavior_ai_enabled),
        outbound_enabled=bool(settings.behavior_ai_outbound_enabled),
        signed_url_delivery_enabled=bool(settings.oss_browser_direct_enabled),
        public_endpoint=str(settings.oss_browser_endpoint or "").strip(),
        signed_url_ttl_seconds=int(settings.oss_browser_url_ttl_seconds),
        api_key=str(settings.dashscope_api_key or ""),
        app_id=str(settings.dashscope_app_id or ""),
        provider_user_id=str(settings.dashscope_user_id or ""),
        provider_device_uuid=str(settings.dashscope_device_uuid or ""),
        annotation_version=str(settings.vla_anno_version or ""),
        sdk_dir=str(settings.embodied_vl_sdk_dir),
        checksums_file=str(settings.embodied_vl_checksums_file),
    )


def load_behavior_ai_runtime(db: Session) -> BehaviorAiRuntimeConfig:
    row = db.get(PlatformAiSetting, 1)
    if row is None:
        return _env_ai_runtime()
    manager = get_key_manager()
    try:
        api_key = manager.decrypt_field(row.api_key_envelope_json)
        app_id = manager.decrypt_field(row.app_id_envelope_json)
    except ValueError as exc:
        raise PlatformSettingsError("platform AI secret cannot be decrypted") from exc
    return BehaviorAiRuntimeConfig(
        enabled=bool(row.enabled),
        outbound_enabled=bool(row.outbound_enabled),
        signed_url_delivery_enabled=bool(row.signed_url_delivery_enabled),
        public_endpoint=str(row.public_endpoint or "").strip(),
        signed_url_ttl_seconds=int(row.signed_url_ttl_seconds),
        api_key=api_key,
        app_id=app_id,
        provider_user_id=str(row.provider_user_id or ""),
        provider_device_uuid=str(row.provider_device_uuid or ""),
        annotation_version=str(row.annotation_version or ""),
        sdk_dir=str(settings.embodied_vl_sdk_dir),
        checksums_file=str(settings.embodied_vl_checksums_file),
    )


def ai_settings_public_view(db: Session) -> dict[str, object]:
    row = db.get(PlatformAiSetting, 1)
    runtime = load_behavior_ai_runtime(db)
    return {
        "enabled": runtime.enabled,
        "outbound_enabled": runtime.outbound_enabled,
        "signed_url_delivery_enabled": runtime.signed_url_delivery_enabled,
        "public_endpoint": runtime.public_endpoint,
        "signed_url_ttl_seconds": runtime.signed_url_ttl_seconds,
        "provider_user_id": runtime.provider_user_id,
        "provider_device_uuid": runtime.provider_device_uuid,
        "annotation_version": runtime.annotation_version,
        "api_key_configured": bool(runtime.api_key),
        "app_id_configured": bool(runtime.app_id),
        "revision": int(row.revision) if row is not None else 0,
        "source": "database" if row is not None else "environment",
    }


def _apply_secret_action(
    current: object,
    *,
    action: Literal["keep", "replace", "clear"],
    value: str,
    env_value: str,
) -> object:
    if action == "clear":
        return None
    if action == "replace":
        secret = value.strip()
        if not secret:
            raise PlatformSettingsError("replacement secret is required")
        return get_key_manager().encrypt_field(secret)
    if current:
        return current
    return get_key_manager().encrypt_field(env_value) if env_value else None


def update_ai_settings(
    db: Session,
    *,
    expected_revision: int,
    enabled: bool,
    outbound_enabled: bool,
    signed_url_delivery_enabled: bool,
    public_endpoint: str,
    signed_url_ttl_seconds: int,
    provider_user_id: str,
    provider_device_uuid: str,
    annotation_version: str,
    api_key_action: Literal["keep", "replace", "clear"],
    api_key_value: str,
    app_id_action: Literal["keep", "replace", "clear"],
    app_id_value: str,
    actor_id: int | None,
) -> PlatformAiSetting:
    row = (
        db.query(PlatformAiSetting)
        .filter(PlatformAiSetting.id == 1)
        .with_for_update()
        .one_or_none()
    )
    current_revision = int(row.revision) if row is not None else 0
    if current_revision != expected_revision:
        raise PlatformSettingsConflict("platform settings revision conflict")
    endpoint = public_endpoint.strip()
    if signed_url_delivery_enabled and not _is_public_oss_browser_endpoint(endpoint):
        raise PlatformSettingsError("AI signed URL endpoint must be a public HTTPS endpoint")
    if not 60 <= int(signed_url_ttl_seconds) <= 3600:
        raise PlatformSettingsError("AI signed URL TTL must be between 60 and 3600 seconds")
    if row is None:
        row = PlatformAiSetting(id=1)
        db.add(row)
    row.enabled = enabled
    row.outbound_enabled = outbound_enabled
    row.signed_url_delivery_enabled = signed_url_delivery_enabled
    row.public_endpoint = endpoint
    row.signed_url_ttl_seconds = int(signed_url_ttl_seconds)
    row.provider_user_id = provider_user_id.strip()
    row.provider_device_uuid = provider_device_uuid.strip()
    row.annotation_version = annotation_version.strip()
    row.api_key_envelope_json = _apply_secret_action(
        row.api_key_envelope_json,
        action=api_key_action,
        value=api_key_value,
        env_value=settings.dashscope_api_key,
    )
    row.app_id_envelope_json = _apply_secret_action(
        row.app_id_envelope_json,
        action=app_id_action,
        value=app_id_value,
        env_value=settings.dashscope_app_id,
    )
    row.revision = current_revision + 1
    row.updated_by_user_id = actor_id
    db.flush()
    return row


def normalize_oss_scope(
    *, workspace_id: int, task_set_id: int, bucket: str, prefixes: list[str]
) -> dict[str, object]:
    normalized_bucket = str(bucket or "").strip()
    if (
        len(normalized_bucket) < 3
        or len(normalized_bucket) > 63
        or normalized_bucket != normalized_bucket.lower()
        or not _OSS_BUCKET_RE.fullmatch(normalized_bucket)
    ):
        raise PlatformSettingsError("bucket must be a valid OSS bucket name")
    normalized_prefixes: list[str] = []
    for raw_prefix in prefixes:
        if not isinstance(raw_prefix, str):
            raise PlatformSettingsError("prefix must be a non-empty relative object prefix")
        raw = raw_prefix.strip().replace("\\", "/").strip("/")
        path = PurePosixPath(raw)
        if not raw or raw == "." or path.is_absolute() or ".." in path.parts:
            raise PlatformSettingsError("prefix must be a non-empty relative object prefix")
        normalized = path.as_posix()
        if normalized not in normalized_prefixes:
            normalized_prefixes.append(normalized)
    if not normalized_prefixes:
        raise PlatformSettingsError("prefixes must contain at least one prefix")
    return {
        "workspace_id": int(workspace_id),
        "task_set_id": int(task_set_id),
        "bucket": normalized_bucket,
        "prefixes": normalized_prefixes,
    }


def validate_scope_ownership(db: Session, *, workspace_id: int, task_set_id: int) -> None:
    if db.get(Workspace, workspace_id) is None:
        raise PlatformSettingsError("workspace does not exist")
    task_set = db.get(TaskSet, task_set_id)
    if task_set is None or int(task_set.workspace_id) != workspace_id:
        raise PlatformSettingsError("task set does not belong to workspace")


def scope_public_view(scope: ExternalOssImportScope) -> dict[str, object]:
    return {
        "id": int(scope.id),
        "workspace_id": int(scope.workspace_id),
        "task_set_id": int(scope.task_set_id),
        "bucket": scope.bucket,
        "prefixes": list(scope.prefixes_json or []),
        "is_enabled": bool(scope.is_enabled),
        "revision": int(scope.revision),
    }


def list_oss_scopes(db: Session) -> list[ExternalOssImportScope]:
    return db.query(ExternalOssImportScope).order_by(ExternalOssImportScope.id.asc()).all()
