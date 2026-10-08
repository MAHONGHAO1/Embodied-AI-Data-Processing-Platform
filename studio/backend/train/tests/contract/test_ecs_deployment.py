from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]
COMPOSE = (ROOT / "infra/ecs/docker-compose.yml").read_text()
CADDYFILE = (ROOT / "infra/ecs/Caddyfile").read_text()
CADDY_SHARED = (ROOT / "infra/ecs/Caddyfile.shared-host.example").read_text()
ENV_EXAMPLE = (ROOT / "infra/ecs/.env.example").read_text()
WEB_DOCKERFILE = (ROOT / "apps/web/Dockerfile").read_text()
PUBLISH_SCRIPT = (ROOT / "scripts/publish_control_plane_images.sh").read_text()
PREFLIGHT = (ROOT / "scripts/ecs_preflight.sh").read_text()


def test_ecs_stack_uses_shared_edge_loopback_ports() -> None:
    # Shared /opt/caddy owns 80/443; QuicTrain must not publish them.
    assert '"80:80"' not in COMPOSE
    assert '"443:443"' not in COMPOSE
    assert "gateway:" not in COMPOSE
    assert '"127.0.0.1:8001:8000"' in COMPOSE
    assert '"127.0.0.1:3000:3000"' in COMPOSE
    assert '"127.0.0.1:5001:5000"' in COMPOSE
    # Data platform keeps host 5432/6379/8000 — train PG stays internal.
    assert '"5432:5432"' not in COMPOSE
    assert '"6379:6379"' not in COMPOSE
    assert '"8000:8000"' not in COMPOSE
    assert "QUICTRAIN_AUTH_MODE: ${QUICTRAIN_AUTH_MODE:-local}" in COMPOSE
    assert "admin_password:" in COMPOSE
    assert "127.0.0.1:8001" in CADDYFILE
    assert "127.0.0.1:5001" in CADDYFILE
    # Training site must not force edge Basic Auth (app /login owns multi-user).
    assert "basic_auth" not in CADDYFILE
    assert "data.quicrobot.xyz" in CADDY_SHARED
    assert "127.0.0.1:8000" in CADDY_SHARED


def test_ecs_stack_uses_production_provider_postgres_and_secrets() -> None:
    assert "QUICTRAIN_PROVIDER: aliyun_dlc" in COMPOSE
    assert "postgresql+psycopg://" in COMPOSE
    assert "POSTGRES_HOST_AUTH_METHOD: trust" not in COMPOSE
    assert "postgres_password" in COMPOSE
    assert "bootstrap_admin_token" in COMPOSE
    assert "ALIBABA_CLOUD_ACCESS_KEY_ID" not in COMPOSE
    assert "ALIBABA_CLOUD_ACCESS_KEY_SECRET" not in COMPOSE
    assert "ALIYUN_DLC_ECS_SPEC" not in COMPOSE
    for name in (
        "QUICTRAIN_CPFS_DATA_SOURCE_ID",
        "QUICTRAIN_CPFS_ROOT_URI",
        "ALIYUN_DLC_WORKSPACE_ID",
        "ALIYUN_DLC_RESOURCE_ID",
    ):
        assert f"{name}: ${{{name}}}" in COMPOSE


def test_ecs_stack_mounts_cpfs_and_local_export_roots() -> None:
    assert "/mnt/cpfs:/mnt/cpfs" in COMPOSE
    assert "QUICTRAIN_MATERIALIZATION_ROOT: /mnt/cpfs/quictrain/datasets" in COMPOSE
    assert "QUICTRAIN_EXPORT_ROOT: /mnt/cpfs/quictrain/exports" in COMPOSE
    assert "QUICTRAIN_OSS_CPFS_MIRROR_PREFIX:" in COMPOSE
    assert "initdb:/docker-entrypoint-initdb.d:ro" in COMPOSE


def test_ecs_environment_example_contains_no_live_deployment_values() -> None:
    values = dict(
        line.split("=", 1)
        for line in ENV_EXAMPLE.splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    for name in (
        "PUBLIC_HOSTNAME",
        "ALIYUN_OSS_BUCKET",
        "QUICTRAIN_CPFS_DATA_SOURCE_ID",
        "QUICTRAIN_CPFS_ROOT_URI",
        "ALIYUN_DLC_WORKSPACE_ID",
        "ALIYUN_DLC_RESOURCE_ID",
        "NEXT_PUBLIC_AUTH_TOKEN",
        "QUICTRAIN_ALERTS_WEBHOOK_URL",
    ):
        assert values[name] == ""


def test_production_web_disables_demo_and_uses_pnpm() -> None:
    assert 'NEXT_PUBLIC_ALLOW_DEMO_MODE: "false"' in COMPOSE
    assert "pnpm@11.7.0" in WEB_DOCKERFILE
    assert "package-lock.json" not in WEB_DOCKERFILE
    assert "npm ci" not in WEB_DOCKERFILE
    assert "pnpm --dir apps/web install --frozen-lockfile" in WEB_DOCKERFILE
    source = (ROOT / "apps/web/app/components/QuicTrainApp.tsx").read_text()
    assert "生产控制面当前不可用，任务未提交" in source


def test_control_plane_publish_script_keeps_runtime_repo_separate() -> None:
    assert "quictrain-api" in PUBLISH_SCRIPT
    assert "quictrain-scheduler" in PUBLISH_SCRIPT
    assert "quictrain-web" in PUBLISH_SCRIPT
    assert "quictrain-mlflow" in PUBLISH_SCRIPT
    assert "quictrain-lerobot-runtime" not in PUBLISH_SCRIPT


def test_ecs_preflight_script_exists() -> None:
    assert "infra/ecs/.env" in PREFLIGHT or "ECS_DIR" in PREFLIGHT
    assert "bootstrap_admin_token" in PREFLIGHT
    assert "admin_password" in PREFLIGHT
    assert "docker compose" in PREFLIGHT
    assert "8001" in PREFLIGHT
    assert "shared" in PREFLIGHT.lower() or "/opt/caddy" in PREFLIGHT
