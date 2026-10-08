"""Production OSS environment and Docker-secret wiring."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import data.config as config

ROOT = Path(__file__).resolve().parents[2]


def test_settings_reject_sqlite_database_url():
    with pytest.raises(ValueError, match="PostgreSQL"):
        config.Settings(
            _env_file=None,
            database_url="sqlite:////tmp/quicdata-test.db",
        )


def test_batch_episode_reset_starts_only_new_jobrun_workers():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    compose = (ROOT / "deploy" / "docker-compose.prod.yml").read_text(encoding="utf-8")
    worker_script = (ROOT / "scripts" / "start-worker.sh").read_text(encoding="utf-8")

    assert "prod-up: prod-init" in makefile
    assert "$(MAKE) --no-print-directory oss-browser-cors-check" not in makefile
    assert "$(PROD_COMPOSE) up migrate" in makefile
    assert (
        "$(PROD_COMPOSE) up -d --build api realtime-dispatcher worker-control worker-ingest worker-media worker-publish worker-export worker-analytics celery-beat"
        in makefile
    )
    for service in (
        "realtime-dispatcher",
        "worker-control",
        "worker-ingest",
        "worker-media",
        "worker-publish",
        "worker-export",
        "worker-analytics",
        "celery-beat",
    ):
        assert f"\n  {service}:" in compose
    assert '"--queues=control,general"' in compose
    assert 'control) QUEUES="control,general" ;;' in worker_script
    assert (
        'ingest|media|publish|export|analytics|governance|ai) QUEUES="$QUEUE" ;;' in worker_script
    )
    assert "--max-tasks-per-child" in compose
    assert "--max-memory-per-child" in compose
    assert "\n  worker-ai:" in compose
    assert 'profiles: ["ai"]' in compose
    assert "--queues=ai" in compose
    assert "external-deps/embodied-vl:ro" in compose
    assert "legacy Task" not in compose


def test_local_beat_runs_from_the_backend_package_directory():
    service_script = (ROOT / "scripts" / "dev-screen-service.sh").read_text(encoding="utf-8")

    assert (
        'cd "$ROOT/backend"\n    exec "$PYTHON_BIN" -m celery -A data.celery_app:celery_app beat'
        in service_script
    )


def test_api_only_deployment_excludes_legacy_worker_configuration():
    compose = (ROOT / "deploy" / "docker-compose.prod.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    template = (ROOT / "deploy" / ".env.example").read_text(encoding="utf-8")
    secret_wrapper = (ROOT / "deploy" / "run-with-secrets.sh").read_text(encoding="utf-8")

    for legacy_setting in ("MCAP_CLI_", "BEHAVIOR_AI_", "DASHSCOPE_"):
        assert legacy_setting not in compose
        assert legacy_setting not in template
    allowed_ego_source_settings = {"EGO_SOURCE_OSS_MULTIPART_COPY_ENABLED"}
    assert {
        line.strip().split(":", maxsplit=1)[0]
        for line in compose.splitlines()
        if line.strip().startswith("EGO_SOURCE_")
    } == allowed_ego_source_settings
    assert {
        line.strip().split("=", maxsplit=1)[0]
        for line in template.splitlines()
        if line.strip().startswith("EGO_SOURCE_")
    } == allowed_ego_source_settings
    assert "MCAP_CLI_" not in dockerfile
    assert "DASHSCOPE_" not in secret_wrapper
    assert "python -m scripts.verify_redis" in secret_wrapper


def test_reset_data_is_bounded_to_the_worktree_runtime_storage_root():
    reset_script = (ROOT / "scripts" / "reset-data.sh").read_text(encoding="utf-8")

    assert 'STORAGE="$ROOT/deploy/runtime/scratch"' in reset_script
    assert "STORAGE_ROOT" not in reset_script


def test_reset_removes_unavailable_legacy_import_scripts():
    assert not (ROOT / "scripts" / "seed-demo-qrdf.sh").exists()
    assert not (ROOT / "backend" / "scripts" / "import_bag_batch.py").exists()


def test_production_oss_settings_define_all_storage_fields_without_runtime_config():
    settings = config.Settings(
        _env_file=None,
        environment="production",
        storage_provider="minio",
        storage_endpoint="http://minio:9000",
        storage_access_key_id="production-access-key",
        storage_secret_access_key="production-access-secret",
        oss_bucket_raw="company-raw",
        oss_bucket_process="company-process",
        oss_bucket_export="company-export",
    )

    storage = config.resolve_deployment_storage(settings)
    assert storage["storage_provider"] == "minio"
    assert storage["storage_endpoint"] == "http://minio:9000"
    assert storage["buckets"] == {
        "raw": "company-raw",
        "process": "company-process",
        "export": "company-export",
    }


@pytest.mark.parametrize(
    "name,value",
    (
        ("STORAGE_MODE", "local"),
        ("OSS_BUCKET_OFFICIAL", "retired"),
        ("OSS_KEEP_LOCAL_CACHE", "true"),
        ("STORAGE_ROOT", "/tmp/retired"),
    ),
)
def test_retired_storage_environment_is_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="retired storage settings"):
        config.Settings(_env_file=None)


def test_production_secret_wrapper_exports_a_complete_storage_secret_pair(tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python"
    fake_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_python.chmod(0o755)
    for name, value in {
        "app_secret_key": "app-secret",
        "postgres_password": "postgres-secret",
        "redis_password": "redis-secret",
        "storage_access_key_id": "storage-access-key",
        "storage_secret_access_key": "storage-access-secret",
    }.items():
        (secrets / name).write_text(value, encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "deploy" / "run-with-secrets.sh"),
            "bash",
            "-c",
            'test "$STORAGE_ACCESS_KEY_ID" = storage-access-key && '
            'test "$STORAGE_SECRET_ACCESS_KEY" = storage-access-secret',
        ],
        env={
            **os.environ,
            "SECRETS_DIR": str(secrets),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_production_compose_mounts_provider_secrets_and_three_bucket_settings():
    compose = (ROOT / "deploy" / "docker-compose.prod.yml").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    for variable in (
        "STORAGE_PROVIDER",
        "STORAGE_ENDPOINT",
        "STORAGE_ACCESS_KEY_ID",
        "STORAGE_SECRET_ACCESS_KEY",
        "OSS_BUCKET_RAW",
        "OSS_BUCKET_PROCESS",
        "OSS_BUCKET_EXPORT",
    ):
        assert f"{variable}:" in compose
    assert "OSS_BUCKET_OFFICIAL" not in compose
    assert "storage_access_key_id:" in compose
    assert "storage_secret_access_key:" in compose
    assert "OSS_ACCESS_KEY_ID:" not in compose
    assert "OSS_ACCESS_KEY_SECRET:" not in compose
    assert "deploy/secrets/storage_access_key_id" in makefile
    assert "deploy/secrets/storage_secret_access_key" in makefile


def test_batch_import_limit_defaults_to_100_gib_and_is_deployment_configurable():
    default_settings = config.Settings(_env_file=None)
    configured = config.Settings(_env_file=None, import_max_upload_gb=128)
    compose = (ROOT / "deploy" / "docker-compose.prod.yml").read_text(encoding="utf-8")
    template = (ROOT / "deploy" / ".env.example").read_text(encoding="utf-8")

    assert default_settings.import_max_upload_bytes == 100 * 1024 * 1024 * 1024
    assert configured.import_max_upload_bytes == 128 * 1024 * 1024 * 1024
    assert "IMPORT_MAX_UPLOAD_GB: ${IMPORT_MAX_UPLOAD_GB:-100}" in compose
    assert "IMPORT_MAX_UPLOAD_GB=100" in template


def test_browser_upload_thresholds_are_deployment_configurable():
    defaults = config.Settings(_env_file=None)
    compose = (ROOT / "deploy" / "docker-compose.prod.yml").read_text(encoding="utf-8")
    template = (ROOT / "deploy" / ".env.example").read_text(encoding="utf-8")

    assert defaults.import_api_upload_max_bytes == 256 * 1024 * 1024
    assert defaults.import_direct_upload_part_bytes == 64 * 1024 * 1024
    expected = {
        "IMPORT_API_UPLOAD_MAX_MB": "256",
        "IMPORT_DIRECT_UPLOAD_PART_MB": "64",
        "IMPORT_DIRECT_UPLOAD_EXPIRE_HOURS": "24",
        "IMPORT_DIRECT_UPLOAD_CONCURRENCY": "4",
    }
    for variable, default in expected.items():
        assert f"{variable}: ${{{variable}:-{default}}}" in compose
        assert f"{variable}={default}" in template


def test_native_lerobot_bundle_limits_are_deployment_configurable():
    defaults = config.Settings(_env_file=None)
    configured = config.Settings(
        _env_file=None,
        native_lerobot_bundle_max_gb=256,
        native_lerobot_bundle_retention_days=14,
    )
    compose = (ROOT / "deploy" / "docker-compose.prod.yml").read_text(encoding="utf-8")
    template = (ROOT / "deploy" / ".env.example").read_text(encoding="utf-8")

    assert defaults.native_lerobot_bundle_max_gb == 100
    assert defaults.native_lerobot_bundle_retention_days == 7
    assert configured.native_lerobot_bundle_max_gb == 256
    assert configured.native_lerobot_bundle_retention_days == 14
    assert "NATIVE_LEROBOT_BUNDLE_MAX_GB: ${NATIVE_LEROBOT_BUNDLE_MAX_GB:-100}" in compose
    assert (
        "NATIVE_LEROBOT_BUNDLE_RETENTION_DAYS: ${NATIVE_LEROBOT_BUNDLE_RETENTION_DAYS:-7}"
        in compose
    )
    assert "NATIVE_LEROBOT_BUNDLE_MAX_GB=100" in template
    assert "NATIVE_LEROBOT_BUNDLE_RETENTION_DAYS=7" in template


def test_production_rejects_direct_upload_part_size_below_provider_part_limit():
    settings = config.Settings(
        _env_file=None,
        environment="production",
        storage_provider="minio",
        storage_endpoint="https://minio.example",
        storage_access_key_id="access-key",
        storage_secret_access_key="secret-key",
        oss_browser_direct_enabled=True,
        oss_browser_endpoint="https://oss-cn-beijing.aliyuncs.com",
        import_max_upload_gb=1024,
        import_direct_upload_part_mb=64,
    )

    with pytest.raises(RuntimeError, match="too small"):
        settings.validate_storage_deployment_config()


def test_production_rejects_custom_csp_without_exact_browser_oss_sources():
    base = {
        "_env_file": None,
        "environment": "production",
        "storage_provider": "minio",
        "storage_endpoint": "https://minio.example",
        "storage_access_key_id": "access-key",
        "storage_secret_access_key": "secret-key",
        "oss_browser_direct_enabled": True,
        "oss_browser_endpoint": "https://oss-cn-beijing.aliyuncs.com",
        "content_security_policy": "default-src 'self'; connect-src 'self' *",
    }

    with pytest.raises(RuntimeError, match="custom CONTENT_SECURITY_POLICY"):
        config.Settings(**base).validate_storage_deployment_config()


def test_source_multipart_copy_setting_prefers_generic_name_and_keeps_ego_fallback():
    legacy = config.Settings(
        _env_file=None,
        ego_source_oss_multipart_copy_enabled=True,
    )
    generic = config.Settings(
        _env_file=None,
        source_oss_multipart_copy_enabled=False,
        ego_source_oss_multipart_copy_enabled=True,
    )

    assert legacy.source_package_oss_multipart_copy_enabled is True
    assert generic.source_package_oss_multipart_copy_enabled is False


def test_production_compose_passes_nonsecret_browser_direct_settings():
    compose = (ROOT / "deploy" / "docker-compose.prod.yml").read_text(encoding="utf-8")
    template = (ROOT / "deploy" / ".env.example").read_text(encoding="utf-8")

    for variable in (
        "OSS_BROWSER_DIRECT_ENABLED",
        "OSS_BROWSER_ENDPOINT",
        "OSS_BROWSER_URL_TTL_SECONDS",
    ):
        assert f"{variable}: ${{{variable}" in compose
        assert f"{variable}=" in template

    assert "storage_access_key_id:" in compose
    assert "OSS_ACCESS_KEY_ID:" not in compose


def test_uat_up_starts_only_the_current_jobrun_workers():
    script = (ROOT / "scripts" / "uat-up.sh").read_text(encoding="utf-8")

    for service in (
        "api",
        "worker-control",
        "worker-ingest",
        "worker-media",
        "worker-publish",
        "worker-export",
        "worker-analytics",
        "celery-beat",
    ):
        assert service in script
    assert "UAT_AI_WORKER_ENABLED:-" in script
    assert '"$ENV_FILE"' in script
    assert "unsupported UAT_AI_WORKER_ENABLED value" in script
    assert "services+=(worker-ai)" in script
    assert "--remove-orphans" in script
    assert "quicstudio-uat" in script
    assert "/opt/quicdata/new" not in script
    assert '"$SCRIPT_DIR/quic_studio/deploy"' in script


def test_browser_cors_deployment_preflight_is_explicit_and_environment_scoped():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    uat_script = (ROOT / "scripts" / "uat-up.sh").read_text(encoding="utf-8")
    cors_script = (ROOT / "scripts" / "oss-browser-cors.sh").read_text(encoding="utf-8")
    prod_up_recipe = makefile.split("prod-up: prod-init\n", maxsplit=1)[1].split(
        "\n\n", maxsplit=1
    )[0]
    prod_redis_ready = "$(PROD_COMPOSE) up -d --wait redis-state"
    uat_redis_ready = '"${compose[@]}" up -d --wait redis-state'

    assert "oss-browser-cors-check" in makefile
    assert "oss-browser-cors-apply" in makefile
    assert "--apply" in makefile
    assert "$(PROD_COMPOSE) build migrate" in makefile
    assert "$(PROD_COMPOSE) build api" not in makefile
    assert prod_redis_ready in prod_up_recipe
    assert prod_up_recipe.index("$(PROD_COMPOSE) build migrate") < prod_up_recipe.index(
        prod_redis_ready
    )
    assert prod_up_recipe.index(prod_redis_ready) < prod_up_recipe.index(
        "$(PROD_COMPOSE) up migrate"
    )
    assert "retired oss2-only configure helper is not run" in uat_script
    assert '"${compose[@]}" build migrate' in uat_script
    assert '"${compose[@]}" build api' not in uat_script
    assert uat_redis_ready in uat_script
    assert uat_script.index('"${compose[@]}" build migrate') < uat_script.index(uat_redis_ready)
    assert uat_script.index(uat_redis_ready) < uat_script.index('"${compose[@]}" up migrate')
    assert "--env-file" in cors_script
    assert "--compose-file" in cors_script
    assert "--no-deps" in cors_script
    assert "OSS_BROWSER_DIRECT_ENABLED" in cors_script


def test_reset_production_documentation_declares_the_new_worker_baseline():
    production = (ROOT / "docs" / "PRODUCTION_SETUP.md").read_text(encoding="utf-8")

    assert "PostgreSQL" in production
    assert "worker-ingest" in production
    assert "旧版 Task/EGO worker 保持禁用" in production


def test_oss_import_scopes_are_database_owned_only():
    settings = config.Settings(_env_file=None)
    storage = config.resolve_deployment_storage(settings)
    compose = (ROOT / "deploy" / "docker-compose.prod.yml").read_text(encoding="utf-8")
    template = (ROOT / "deploy" / ".env.example").read_text(encoding="utf-8")

    assert not hasattr(settings, "oss_import_scopes_json")
    assert "oss_import_scopes" not in storage
    assert "OSS_IMPORT_SCOPES_JSON" not in compose
    assert "OSS_IMPORT_SCOPES_JSON" not in template


def test_production_compose_passes_source_multipart_copy_opt_ins_to_ingest_worker():
    compose = (ROOT / "deploy" / "docker-compose.prod.yml").read_text(encoding="utf-8")
    template = (ROOT / "deploy" / ".env.example").read_text(encoding="utf-8")

    assert (
        "EGO_SOURCE_OSS_MULTIPART_COPY_ENABLED: ${EGO_SOURCE_OSS_MULTIPART_COPY_ENABLED:-false}"
    ) in compose
    assert "EGO_SOURCE_OSS_MULTIPART_COPY_ENABLED=false" in template
    assert ("SOURCE_OSS_MULTIPART_COPY_ENABLED: ${SOURCE_OSS_MULTIPART_COPY_ENABLED:-}") in compose
    assert "SOURCE_OSS_MULTIPART_COPY_ENABLED=" in template
