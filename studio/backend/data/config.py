"""Platform configuration: environment security baselines, runtime configuration, and encrypted secret persistence."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from data.security.redact import safe_copy_config
from data.version import PRODUCT_VERSION

BASE_DIR = Path(__file__).resolve().parent.parent
logger = logging.getLogger("quicdata.config")

DEFAULT_SECRET = "quicdata-dev-secret-change-in-production"
EGO_ARCHIVE_DEFAULT_LIMIT_GB = 100
EGO_ARCHIVE_BYTES_PER_MB = 1024 * 1024
EGO_ARCHIVE_BYTES_PER_GB = 1024 * 1024 * 1024
IMPORT_DEFAULT_LIMIT_GB = 100
DEFAULT_VLA_ANNO_VERSION = "vla-anno#A2FM#AT9J"
DEFAULT_EMBODIED_VL_SDK_DIR = BASE_DIR.parent / "deploy" / "external-deps" / "embodied-vl"
DEFAULT_EMBODIED_VL_CHECKSUMS_FILE = (
    BASE_DIR.parent / "deploy" / "external-deps" / "checksums.sha256"
)
TRUE_ENV_VALUES = frozenset({"1", "on", "t", "true", "y", "yes"})
_DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        validate_assignment=True,
    )

    api_host: str = "0.0.0.0"  # nosec B104 - configurable server bind default
    api_port: int = 8000
    secret_key: str = DEFAULT_SECRET

    # Environment: development | production (also accepts prod / production)
    environment: str = "development"
    # Enable the isolated development OSS bucket defaults. Credentials remain
    # local-only and cloud writes still require OSS_CLOUD_ENABLED=true.
    dev_oss_enabled: bool = False
    # pytest sets this before importing the application. It is intentionally
    # not a deployment switch and prevents accidental calls to real OSS.
    test_mode: bool = False
    # Comma-separated CORS origins; empty allows local development origins only.
    cors_origins: str = ""
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    # Session cookie Secure flag: True in production, False in dev/test, or explicitly overridden
    session_cookie_secure: bool | None = None
    # Explicitly allow seed users; strictly disabled by default in production
    allow_default_user_seed: bool = True
    # User registration mode override: empty uses runtime_config.registration.mode;
    # disabled | admin_only | open (similar to GitLab signup switch)
    registration_mode: str = ""
    # Optional: merge registration section from JSON file at startup (e.g. deploy/registration.json)
    registration_config_file: str = ""
    # P1: Envelope encryption persistence for runtime_config sensitive fields
    encrypt_runtime_secrets: bool = True
    # Key provider: local | cloud_kms (cloud implementation is an extension placeholder)
    secret_provider: str = "local"
    kms_key_id: str = ""
    kms_region: str = ""
    kms_endpoint: str = ""
    # OSS SSE-KMS extension switch (disabled by default; attaches encryption header when enabled)
    oss_sse_kms_enabled: bool = False
    oss_sse_algorithm: str = "KMS"
    oss_sse_kms_key_id: str = ""
    oss_sse_kms_enforce: bool = False

    # P2 security operations. These remain configurable even though v0.2
    # replaced the legacy Qwen annotation integration.
    security_login_fail_threshold: int = 5
    security_login_fail_window_seconds: int = 300
    security_authz_denied_threshold: int = 20
    security_authz_denied_window_seconds: int = 300
    security_bulk_download_threshold: int = 30
    security_bulk_download_window_seconds: int = 300
    security_export_create_threshold: int = 10
    security_export_create_window_seconds: int = 600
    security_login_rate_limit_enabled: bool = True
    security_login_block_threshold: int = 10
    security_login_block_window_seconds: int = 600
    security_alert_webhook_url: str = ""
    security_alert_email: str = ""
    security_alert_smtp_host: str = ""
    security_alert_smtp_port: int = 587
    security_alert_smtp_user: str = ""
    security_alert_smtp_password: str = ""
    security_alert_smtp_from: str = ""
    security_alert_smtp_tls: bool = True
    security_headers_enabled: bool = True
    content_security_policy: str = ""
    hsts_max_age: int = 0
    # Production defaults to HSTS; isolated HTTP UAT can explicitly disable it.
    hsts_enabled: bool | None = None

    database_url: str = "postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata"
    redis_url: str = "redis://127.0.0.1:6379/0"
    celery_broker_url: str = "redis://127.0.0.1:6379/1"
    celery_result_backend: str = "redis://127.0.0.1:6379/2"
    # Production uses one standalone realtime dispatcher. Local development
    # may explicitly keep the legacy in-API loop for a single-process setup.
    realtime_dispatcher_in_api: bool = False
    realtime_dispatch_interval_seconds: int = Field(default=5, ge=1, le=60)
    celery_inspect_cache_seconds: int = Field(default=5, ge=1, le=60)

    # v0.2 supply-chain policy: production builds must declare domestic sources.
    pypi_index_url: str = ""
    docker_registry_prefix: str = ""
    apt_mirror_url: str = ""
    npm_registry_url: str = ""

    oss_endpoint: str = "oss-cn-beijing.aliyuncs.com"
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""
    oss_region: str = "cn-beijing"
    oss_cloud_enabled: bool = False
    oss_keep_local_cache: bool = True
    # Four logical bucket mappings. Can be explicitly overridden in local .env or production env file.
    oss_bucket_raw: str = "quicstudio-dev-raw"
    oss_bucket_process: str = "quicstudio-dev-process"
    oss_bucket_official: str = ""  # historical read-only field; never used for new writes
    oss_bucket_export: str = "quicstudio-dev-export"
    # Browser direct reads deliberately use a separate public endpoint. ECS and
    # workers keep using OSS_ENDPOINT, which can remain the same-region internal
    # endpoint for no-charge OSS traffic.
    oss_browser_direct_enabled: bool = False
    oss_browser_endpoint: str = ""
    oss_browser_url_ttl_seconds: int = 900
    # Mirrors the configured object provider: only minio or aliyun_oss.
    # Legacy local/cloud/hybrid values are rejected by storage_mode validation.
    storage_mode: str = "minio"
    # QuicStudio immutable object provider. Development defaults to MinIO;
    # credentials are intentionally empty and therefore fail closed.
    storage_provider: str = "minio"
    # Per-token request budget for external tools (0 disables the limit).
    api_token_rate_limit_per_minute: int = 600
    storage_endpoint: str = ""
    storage_access_key_id: str = ""
    storage_secret_access_key: str = ""
    storage_browser_endpoint: str = ""

    # Only worker scratch data may be written to disk. Persistent objects live
    # in the configured raw/process/export buckets.
    scratch_root: str = str(BASE_DIR / "runtime" / "scratch")
    scratch_max_bytes: int = Field(default=20 * 1024 * 1024 * 1024, ge=1)

    @model_validator(mode="after")
    def reject_legacy_storage_environment(self) -> Settings:
        """The old storage switches are deployment errors, never fallbacks.

        Keep the legacy attributes temporarily so historical migrations and
        read-only adapters can be imported, but fail at process configuration
        time when a deployment still supplies one of the retired variables.
        """
        retired = (
            "STORAGE_MODE",
            "OSS_BUCKET_OFFICIAL",
            "OSS_KEEP_LOCAL_CACHE",
            "STORAGE_ROOT",
        )
        supplied = [name for name in retired if os.getenv(name, "").strip()]
        supplied.extend(
            name
            for name, field in {
                "STORAGE_MODE": "storage_mode",
                "OSS_BUCKET_OFFICIAL": "oss_bucket_official",
                "OSS_KEEP_LOCAL_CACHE": "oss_keep_local_cache",
                "STORAGE_ROOT": "storage_root",
            }.items()
            if field in self.model_fields_set and name not in supplied
        )
        if supplied:
            raise ValueError("retired storage settings are not supported: " + ", ".join(supplied))
        return self

    @property
    def storage_root(self) -> str:
        """Compatibility alias for callers not yet migrated to scratch_root."""
        return self.scratch_root

    @storage_root.setter
    def storage_root(self, value: str) -> None:
        # pytest and the remaining import adapters still patch this old name;
        # keep that operation scoped to the scratch directory only.
        object.__setattr__(self, "scratch_root", str(value))

    bag_data_root: str = str(Path.home() / "data" / "bag")
    publication_source_cache_ttl_seconds: int = Field(default=86400, ge=300, le=604800)
    publication_source_cache_max_bytes: int = Field(
        default=20 * 1024 * 1024 * 1024,
        ge=1024 * 1024 * 1024,
        le=1024 * 1024 * 1024 * 1024,
    )

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_storage_input(cls, values: object) -> object:
        """Reject retired aliases even when pydantic would ignore extras."""
        if isinstance(values, dict):
            retired = {
                "storage_root",
                "STORAGE_ROOT",
                "STORAGE_MODE",
                "OSS_BUCKET_OFFICIAL",
                "OSS_KEEP_LOCAL_CACHE",
            }
            supplied = sorted(
                name for name in retired if name in values and str(values[name] or "").strip()
            )
            if supplied:
                raise ValueError(
                    "retired storage settings are not supported: " + ", ".join(supplied)
                )
        return values

    # EGO source discovery defaults to a local filesystem. Explicit OSS access
    # remains restricted to the configured offline/raw/process subroots.
    ego_source_backend: str = "filesystem"
    ego_source_root: str = str(BASE_DIR / "runtime" / "ego-source")
    ego_source_allowed_buckets: str = "ego-test"
    ego_source_allowed_prefixes: str = "raw/v1/"
    ego_source_oss_offline_ingress_prefixes: str = ""
    ego_source_oss_manual_staging_prefix: str = ""
    ego_source_oss_raw_prefixes: str = ""
    ego_source_oss_process_prefixes: str = ""
    ego_source_oss_raw_destination_prefix: str = "raw/ego/"
    # Generic source packages use this setting when present. None preserves the
    # historical EGO switch as a deployment-compatible fallback.
    source_oss_multipart_copy_enabled: bool | None = None
    ego_source_oss_multipart_copy_enabled: bool = False
    ego_max_rgb_gap_ms: int = 500
    ego_min_cut_duration_ms: int = 1000
    ego_archive_max_entries: int = 1000
    # GB settings take precedence. MB settings are kept for existing deployments.
    ego_archive_max_upload_gb: int | None = None
    ego_archive_max_uncompressed_gb: int | None = None
    ego_archive_max_upload_mb: int | None = None
    ego_archive_max_uncompressed_mb: int | None = None
    # Browser-to-Batch chunked imports. This is a deployment capacity bound,
    # not a client hint; the API remains authoritative for every chunk.
    import_max_upload_gb: int = IMPORT_DEFAULT_LIMIT_GB
    # Requests at or below this limit may traverse the API. Larger browser
    # files must use a short-lived OSS multipart capability.
    import_api_upload_max_mb: int = 256
    import_direct_upload_part_mb: int = 64
    import_direct_upload_expire_hours: int = 24
    import_direct_upload_concurrency: int = 4
    # Duance client admission (local QRDF validation and preview generation).
    # When disabled every source is admitted by the full server path.
    accept_client_admission: bool = True
    client_admission_qrdf_versions: list[str] = Field(default_factory=lambda: ["0.2.1"])
    client_admission_policy_versions: list[str] = Field(default_factory=lambda: ["v1"])
    # OSS candidate discovery is a worker-only operation.  These bounds keep
    # date-partition scans recoverable without inheriting the browser page
    # limit (500 rows) or issuing an unbounded provider listing.
    import_scan_default_days: int = Field(default=7, ge=1, le=31)
    import_scan_max_days: int = Field(default=31, ge=1, le=31)
    import_scan_page_size: int = Field(default=500, ge=1, le=1_000)
    import_scan_candidate_limit: int = Field(default=20_000, ge=1, le=100_000)
    import_scan_legacy_object_limit: int = Field(default=10_000, ge=1, le=100_000)

    # Runtime rows are cleaned in bounded hourly batches. Security audit rows
    # intentionally have no automatic retention setting.
    runtime_realtime_event_retention_days: int = Field(default=7, ge=1, le=365)
    runtime_job_retention_days: int = Field(default=30, ge=1, le=3650)
    runtime_import_session_retention_days: int = Field(default=30, ge=1, le=3650)
    runtime_retention_batch_size: int = Field(default=500, ge=1, le=5000)
    native_lerobot_bundle_max_gb: int = Field(default=100, ge=1, le=1024)
    native_lerobot_bundle_retention_days: int = Field(default=7, ge=1, le=30)

    log_level: str = "info"
    api_prefix: str = "/api/v1"

    behavior_ai_enabled: bool = False
    behavior_ai_outbound_enabled: bool = False
    dashscope_api_key: str = ""
    dashscope_app_id: str = ""
    dashscope_user_id: str = ""
    dashscope_device_uuid: str = ""
    vla_anno_version: str = DEFAULT_VLA_ANNO_VERSION
    embodied_vl_sdk_dir: str = str(DEFAULT_EMBODIED_VL_SDK_DIR)
    embodied_vl_checksums_file: str = str(DEFAULT_EMBODIED_VL_CHECKSUMS_FILE)
    sam_inference_url: str = "http://localhost:8090"
    sam_default_model: str = "sam2"
    feature_sam_auto_bbox: bool = False
    feature_manual_bbox: bool = False

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in {"prod", "production"}

    @property
    def cookie_secure(self) -> bool:
        if self.session_cookie_secure is not None:
            return bool(self.session_cookie_secure)
        return self.is_production

    @property
    def uses_dev_oss_defaults(self) -> bool:
        return self.environment.strip().lower() == "development" and self.dev_oss_enabled

    @property
    def source_package_oss_multipart_copy_enabled(self) -> bool:
        if self.source_oss_multipart_copy_enabled is not None:
            return self.source_oss_multipart_copy_enabled
        return self.ego_source_oss_multipart_copy_enabled

    @field_validator("source_oss_multipart_copy_enabled", mode="before")
    @classmethod
    def empty_source_multipart_setting_uses_legacy_fallback(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("test_mode")
    @classmethod
    def force_environment_test_mode(cls, value: bool) -> bool:
        raw_environment_value = os.getenv("TEST_MODE", "").strip().lower()
        return bool(value or raw_environment_value in TRUE_ENV_VALUES)

    @field_validator("database_url")
    @classmethod
    def require_postgresql_database_url(cls, value: str) -> str:
        database_url = value.strip()
        if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError("DATABASE_URL must be a PostgreSQL URL")
        return database_url

    @property
    def behavior_ai_can_call_provider(self) -> bool:
        return bool(
            self.behavior_ai_enabled and self.behavior_ai_outbound_enabled and not self.test_mode
        )

    @property
    def cors_origin_list(self) -> list[str]:
        raw = (self.cors_origins or "").strip()
        if not raw:
            origins = [f"http://localhost:{self.api_port}", f"http://127.0.0.1:{self.api_port}"]
            if self.environment != "production":
                origins.extend(["http://localhost:8090", "http://127.0.0.1:8090"])
            return origins
        if raw == "*":
            # Explicit * only allowed in development; production startup validation will reject
            return ["*"]
        return [part.strip() for part in raw.split(",") if part.strip()]

    @property
    def ego_source_bucket_set(self) -> frozenset[str]:
        return frozenset(
            item.strip() for item in self.ego_source_allowed_buckets.split(",") if item.strip()
        )

    @property
    def ego_source_prefixes(self) -> tuple[str, ...]:
        return tuple(
            item.strip() for item in self.ego_source_allowed_prefixes.split(",") if item.strip()
        )

    @property
    def ego_source_oss_prefixes(self) -> tuple[str, ...]:
        values = (
            self.ego_source_oss_offline_ingress_prefixes,
            self.ego_source_oss_manual_staging_prefix,
            self.ego_source_oss_raw_prefixes,
            self.ego_source_oss_process_prefixes,
        )
        return tuple(item.strip() for value in values for item in value.split(",") if item.strip())

    @field_validator(
        "ego_archive_max_upload_gb",
        "ego_archive_max_uncompressed_gb",
        "ego_archive_max_upload_mb",
        "ego_archive_max_uncompressed_mb",
    )
    @classmethod
    def validate_ego_archive_limit(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("EGO archive limits must be positive")
        return value

    @field_validator("import_max_upload_gb")
    @classmethod
    def validate_import_upload_limit(cls, value: int) -> int:
        if not 1 <= value <= 1024:
            raise ValueError("IMPORT_MAX_UPLOAD_GB must be between 1 and 1024")
        return value

    @field_validator("import_api_upload_max_mb")
    @classmethod
    def validate_import_api_upload_limit(cls, value: int) -> int:
        if not 1 <= value <= 4096:
            raise ValueError("IMPORT_API_UPLOAD_MAX_MB must be between 1 and 4096")
        return value

    @field_validator("import_direct_upload_part_mb")
    @classmethod
    def validate_import_direct_part_size(cls, value: int) -> int:
        if not 1 <= value <= 5120:
            raise ValueError("IMPORT_DIRECT_UPLOAD_PART_MB must be between 1 and 5120")
        return value

    @field_validator("import_direct_upload_expire_hours")
    @classmethod
    def validate_import_direct_expiry(cls, value: int) -> int:
        if not 1 <= value <= 168:
            raise ValueError("IMPORT_DIRECT_UPLOAD_EXPIRE_HOURS must be between 1 and 168")
        return value

    @field_validator("import_direct_upload_concurrency")
    @classmethod
    def validate_import_direct_concurrency(cls, value: int) -> int:
        if not 1 <= value <= 8:
            raise ValueError("IMPORT_DIRECT_UPLOAD_CONCURRENCY must be between 1 and 8")
        return value

    @property
    def import_max_upload_bytes(self) -> int:
        return self.import_max_upload_gb * EGO_ARCHIVE_BYTES_PER_GB

    @property
    def import_api_upload_max_bytes(self) -> int:
        return min(
            self.import_max_upload_bytes, self.import_api_upload_max_mb * EGO_ARCHIVE_BYTES_PER_MB
        )

    @property
    def import_direct_upload_part_bytes(self) -> int:
        return self.import_direct_upload_part_mb * EGO_ARCHIVE_BYTES_PER_MB

    @property
    def ego_archive_max_upload_bytes(self) -> int:
        return self._ego_archive_limit_bytes(
            self.ego_archive_max_upload_gb,
            self.ego_archive_max_upload_mb,
        )

    @property
    def ego_archive_max_uncompressed_bytes(self) -> int:
        return self._ego_archive_limit_bytes(
            self.ego_archive_max_uncompressed_gb,
            self.ego_archive_max_uncompressed_mb,
        )

    @property
    def ego_archive_max_upload_label(self) -> str:
        return self._ego_archive_limit_label(
            self.ego_archive_max_upload_gb,
            self.ego_archive_max_upload_mb,
        )

    @property
    def ego_archive_max_uncompressed_label(self) -> str:
        return self._ego_archive_limit_label(
            self.ego_archive_max_uncompressed_gb,
            self.ego_archive_max_uncompressed_mb,
        )

    @staticmethod
    def _ego_archive_limit_bytes(gb: int | None, mb: int | None) -> int:
        if gb is not None:
            return gb * EGO_ARCHIVE_BYTES_PER_GB
        if mb is not None:
            return mb * EGO_ARCHIVE_BYTES_PER_MB
        return EGO_ARCHIVE_DEFAULT_LIMIT_GB * EGO_ARCHIVE_BYTES_PER_GB

    @staticmethod
    def _ego_archive_limit_label(gb: int | None, mb: int | None) -> str:
        if gb is not None:
            return f"{gb} GB"
        if mb is not None:
            return f"{mb} MB"
        return f"{EGO_ARCHIVE_DEFAULT_LIMIT_GB} GB"

    def validate_security_baseline(self) -> None:
        """Production startup gate: weak secret keys / overly broad CORS / default seeds."""
        if not self.is_production:
            return
        if self.secret_key == DEFAULT_SECRET or len(self.secret_key) < 32:
            raise RuntimeError(
                "生产环境禁止使用默认或过短 SECRET_KEY，请通过环境变量注入强随机密钥"
            )
        if "*" in self.cors_origin_list:
            raise RuntimeError("生产环境禁止 CORS_ORIGINS=*")

    def validate_supply_chain_config(self) -> None:
        """Require explicit domestic dependency sources in production."""
        if not self.is_production:
            return
        required = {
            "PYPI_INDEX_URL": self.pypi_index_url,
            "DOCKER_REGISTRY_PREFIX": self.docker_registry_prefix,
            "APT_MIRROR_URL": self.apt_mirror_url,
            "NPM_REGISTRY_URL": self.npm_registry_url,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise RuntimeError(f"production requires domestic mirrors: {', '.join(missing)}")

    def validate_storage_deployment_config(self) -> None:
        """Require the provider, endpoint and credentials; never use disk fallback."""
        self.reject_legacy_storage_environment()
        provider = str(self.storage_provider or "").strip().lower()
        if provider not in {"minio", "s3", "s3-compatible", "aliyun", "oss", "aliyun-oss"}:
            raise RuntimeError(f"unsupported STORAGE_PROVIDER: {self.storage_provider}")
        missing = [
            name
            for name, value in {
                "STORAGE_ENDPOINT": self.storage_endpoint,
                "STORAGE_ACCESS_KEY_ID": self.storage_access_key_id,
                "STORAGE_SECRET_ACCESS_KEY": self.storage_secret_access_key,
            }.items()
            if _looks_like_placeholder_credential(value)
        ]
        if missing:
            raise RuntimeError(f"storage provider requires {', '.join(missing)}")
        if not self.is_production or not self.oss_browser_direct_enabled:
            return
        if missing:
            raise RuntimeError(f"browser direct upload requires {', '.join(missing)}")
        if not _is_public_oss_browser_endpoint(self.oss_browser_endpoint):
            raise RuntimeError(
                "OSS_BROWSER_ENDPOINT must be an HTTPS public OSS endpoint, not an internal endpoint"
            )
        if not 60 <= int(self.oss_browser_url_ttl_seconds) <= 3600:
            raise RuntimeError("OSS_BROWSER_URL_TTL_SECONDS must be between 60 and 3600")
        if self.import_max_upload_bytes > self.import_direct_upload_part_bytes * 10_000:
            raise RuntimeError("IMPORT_DIRECT_UPLOAD_PART_MB is too small for IMPORT_MAX_UPLOAD_GB")
        if self.content_security_policy.strip():
            from data.security.headers import (
                validate_browser_direct_content_security_policy,
            )

            try:
                validate_browser_direct_content_security_policy(
                    self.content_security_policy,
                    endpoint=self.oss_browser_endpoint,
                    buckets={
                        "raw": self.oss_bucket_raw,
                        "process": self.oss_bucket_process,
                        "export": self.oss_bucket_export,
                    },
                )
            except ValueError as exc:
                raise RuntimeError(f"custom CONTENT_SECURITY_POLICY is unsafe: {exc}") from exc


settings = Settings()

DEV_OSS_BUCKETS: dict[str, str] = {
    "raw": "dev-quic-data-platform",
    "process": "dev-quic-process-qrdf",
    "export": "dev-quic-lerobot",
}

_BUCKET_SETTING_FIELDS: dict[str, str] = {
    "raw": "oss_bucket_raw",
    "process": "oss_bucket_process",
    "official": "oss_bucket_official",
    "export": "oss_bucket_export",
}


def _looks_like_placeholder_credential(value: str) -> bool:
    candidate = (value or "").strip()
    if not candidate:
        return True
    if candidate in {"k", "s", "x", "test-key", "test-secret"}:
        return True
    return len(candidate) <= 2


def _public_oss_browser_endpoint_parts(value: str | None) -> tuple[str, int | None, bool] | None:
    """Return normalized host, port, and IP flag for a safe public OSS endpoint."""
    from data.security.browser_oss import public_oss_browser_endpoint_parts

    return public_oss_browser_endpoint_parts(value)


def _is_public_oss_browser_endpoint(value: str | None) -> bool:
    """Accept only a credential-free HTTPS OSS endpoint reachable by browsers."""
    return _public_oss_browser_endpoint_parts(value) is not None


def resolve_deployment_storage(source: Settings) -> dict[str, Any]:
    """Build the process-local storage view from Settings only.

    Persisted runtime configuration is deliberately not an input. Development
    defaults only select bucket names; they never provide credentials.
    """
    buckets = {
        role: (
            f"quicstudio-{'prod' if source.is_production else ('test' if source.test_mode else 'dev')}-{role}"
            if source.uses_dev_oss_defaults and field not in source.model_fields_set
            else str(getattr(source, field) or "").strip()
        )
        for role, field in _BUCKET_SETTING_FIELDS.items()
        if role != "official"
    }
    return {
        "storage_provider": source.storage_provider,
        "storage_endpoint": source.storage_endpoint,
        "scratch_root": source.scratch_root,
        "scratch_max_bytes": int(source.scratch_max_bytes),
        "type": "object",
        "uri_scheme": "s3",
        "max_chunk_size_mb": 10,
        "allowed_extensions": [".mcap", ".zip", ".tar", ".tar.gz", ".json"],
        "cloud_enabled": True,
        "keep_local_cache": False,
        "buckets": buckets,
        "oss": {
            "endpoint": source.storage_endpoint,
            "access_key_id": source.storage_access_key_id,
            "access_key_secret": source.storage_secret_access_key,
            "region": source.oss_region,
        },
        "browser_direct": {
            "enabled": bool(source.oss_browser_direct_enabled),
            "endpoint": source.storage_browser_endpoint or source.oss_browser_endpoint,
            "ttl_seconds": int(source.oss_browser_url_ttl_seconds),
        },
    }


settings.validate_storage_deployment_config()

DEFAULT_RBAC: dict[str, Any] = {
    "schema_version": 6,
    "roles": {
        "admin": {
            "label": "管理员",
            "permissions": ["*"],
        },
        "annotator": {
            "label": "标注员",
            "permissions": [
                "workspace:read",
                "batch:read",
                "import:read",
                "episode:read",
                "episode:annotate",
                "dataset:read",
            ],
        },
        "auditor": {
            "label": "审核员",
            "permissions": [
                "workspace:read",
                "batch:read",
                "import:read",
                "episode:read",
                "episode:review",
                "dataset:read",
            ],
        },
    },
    "default_role": "annotator",
}

DEFAULT_STORAGE: dict[str, Any] = resolve_deployment_storage(settings)

DEFAULT_USERS: list[dict[str, str]] = [
    {"email": "admin@quicdata.com", "password": "admin123", "role": "admin"},
    {"email": "annotator@quicdata.com", "password": "annotator123", "role": "annotator"},
    {"email": "auditor@quicdata.com", "password": "auditor123", "role": "auditor"},
]

# User registration policy: admin-created by default, public self-registration disabled
DEFAULT_REGISTRATION: dict[str, Any] = {
    "mode": "admin_only",  # disabled | admin_only | open
    "default_role": "annotator",
    "self_register_role": "annotator",
    "min_password_length": 10,
    "creator_roles": ["admin"],
    "creatable_roles": {
        "admin": ["admin", "annotator", "auditor"],
    },
    "allowed_email_domains": [],  # empty = unrestricted; e.g. ["company.com"]
}


def _copy_registration_defaults() -> dict[str, Any]:
    return {
        **DEFAULT_REGISTRATION,
        "creator_roles": list(DEFAULT_REGISTRATION["creator_roles"]),
        "creatable_roles": {k: list(v) for k, v in DEFAULT_REGISTRATION["creatable_roles"].items()},
        "allowed_email_domains": list(DEFAULT_REGISTRATION["allowed_email_domains"]),
    }


_runtime_config: dict[str, Any] = {
    "rbac": dict(DEFAULT_RBAC),
    "storage": dict(DEFAULT_STORAGE),
    "platform": {
        "name": "QuicData",
        "version": PRODUCT_VERSION,
        "description": "QuicData 数据流转平台 MVP",
    },
    "registration": _copy_registration_defaults(),
}


def get_runtime_config() -> dict[str, Any]:
    """Internal runtime configuration. Never return it directly from an API."""
    return _runtime_config


def get_public_runtime_config() -> dict[str, Any]:
    """External API configuration replica (sanitized)."""
    public = safe_copy_config(_runtime_config)
    from data.services.storage_mode import storage_mode_public_view

    public["storage"] = storage_mode_public_view()
    return public


def should_seed_default_users() -> bool:
    if settings.is_production:
        return False
    return bool(settings.allow_default_user_seed)


def update_runtime_config(section: str, data: dict[str, Any]) -> dict[str, Any]:
    if section not in _runtime_config:
        raise ValueError(f"Unknown config section: {section}")
    if section == "storage":
        raise ValueError(
            "storage deployment configuration is managed by environment and Docker secrets"
        )

    if section == "registration":
        from data.services.registration_service import validate_registration_payload

        data = validate_registration_payload(data)

    _runtime_config[section].update(dict(data))
    _persist_config()
    return get_public_runtime_config()[section]


def set_test_storage_config(data: dict[str, Any]) -> dict[str, Any]:
    """Replace selected storage fields for local-only pytest simulations.

    This does not persist anything and cannot be called by an application
    process. It exists because cloud-path unit tests need deterministic local
    mirror behavior without turning the runtime configuration API back into a
    deployment configuration mechanism.
    """
    if not settings.test_mode:
        raise RuntimeError("test storage configuration is only available in test mode")
    import copy

    from data.services.storage_mode import apply_storage_mode_derived_fields

    storage = copy.deepcopy(_runtime_config["storage"])
    for key, value in data.items():
        if key in {"oss", "buckets"} and isinstance(value, dict):
            storage[key] = {**dict(storage.get(key) or {}), **copy.deepcopy(value)}
        else:
            storage[key] = copy.deepcopy(value)
    _runtime_config["storage"] = apply_storage_mode_derived_fields(storage)
    return _runtime_config["storage"]


_RUNTIME_CONFIG_PATH = BASE_DIR / "runtime" / "runtime_config.json"


def _prepare_persist_payload() -> dict[str, Any]:
    """Return durable runtime settings without deployment-owned storage fields."""
    import copy

    payload = copy.deepcopy(_runtime_config)
    payload.pop("storage", None)
    return payload


def _persist_config() -> None:
    """Persist runtime-owned configuration only."""
    try:
        _RUNTIME_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = _prepare_persist_payload()
        _RUNTIME_CONFIG_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        logger.exception("persist runtime_config failed")


def _apply_env_storage_overrides() -> None:
    """Compatibility name for resetting the current process from Settings."""
    _runtime_config["storage"] = resolve_deployment_storage(settings)


def _load_persisted_config() -> None:
    """Load runtime-owned config while ignoring legacy deployment storage data."""
    try:
        if _RUNTIME_CONFIG_PATH.is_file():
            persisted = json.loads(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
            for section, data in (persisted or {}).items():
                if section in _runtime_config and isinstance(data, dict):
                    if section == "storage":
                        continue
                    if (
                        section == "rbac"
                        and data.get("schema_version") != DEFAULT_RBAC["schema_version"]
                    ):
                        logger.warning("ignoring incompatible persisted RBAC schema")
                        continue
                    if section == "registration":
                        # Replace nested structures like creatable_roles entirely to prevent shallow merge stale keys
                        _runtime_config[section] = {**_runtime_config[section], **data}
                        if "creatable_roles" in data and isinstance(data["creatable_roles"], dict):
                            _runtime_config[section]["creatable_roles"] = dict(
                                data["creatable_roles"]
                            )
                    else:
                        _runtime_config[section].update(data)
    except Exception:
        logger.exception("load persisted runtime_config failed")
    _apply_env_storage_overrides()
    # Ensure registration section has default structure
    if "registration" not in _runtime_config:
        _runtime_config["registration"] = _copy_registration_defaults()
    else:
        base = _copy_registration_defaults()
        current = _runtime_config.get("registration") or {}
        merged_reg = {**base, **current}
        if isinstance(current.get("creatable_roles"), dict):
            merged_reg["creatable_roles"] = dict(current["creatable_roles"])
        _runtime_config["registration"] = merged_reg

    file_path = (settings.registration_config_file or "").strip()
    if file_path:
        from data.services.registration_service import (
            load_registration_file_into_runtime,
        )

        load_registration_file_into_runtime(file_path)


_load_persisted_config()
