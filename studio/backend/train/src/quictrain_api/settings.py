from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="QUICTRAIN_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: str = "development"
    # Unverified examples are useful only in an explicitly opted-in local/test catalog.
    seed_example_datasets: bool = False
    database_url: str = Field(
        default_factory=lambda: (
            os.environ.get("QUICTRAIN_DATABASE_URL")
            or os.environ.get("DATABASE_URL")
            or "postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata"
        )
    )
    provider: str = "fake"
    embedded_scheduler: bool = True
    cors_origins: str = "http://localhost:3000"
    artifact_root: str = "./artifacts"
    cpfs_data_source_id: str | None = None
    # Prefer VPC dataset id for ECS/general quotas when RAM/workspace allows
    # PaiDataset:GetDataset. Falls back to mountTarget options when unset.
    cpfs_vpc_data_source_id: str | None = None
    cpfs_vpc_mount_target: str | None = None
    # JSON object for Quota4090 / ECS DLC jobs (VpcId, SwitchId, SecurityGroupId, …).
    # Required so OSS mounts and JobEnqueued work on general compute quotas.
    dlc_user_vpc_json: str | None = None
    cpfs_root_uri: str | None = None
    cpfs_mount_path: str = "/mnt/cpfs"
    cpfs_workspace_dir: str = "quictrain"
    cpfs_pi05_pretrained_dir: str = "models/pi05/lerobot-pi05-base"
    # OSS mirror prefix for Quota4090 (ECS) jobs that cannot mount BMCPFS.
    # CPFS path bmcpfs://…/<relative> maps to {oss_cpfs_mirror_prefix}/<relative>.
    # Example: oss://bucket/quictrain  ←→  bmcpfs://fs/quictrain/...
    oss_cpfs_mirror_prefix: str | None = None
    mlflow_tracking_uri: str = "http://localhost:5001"
    public_mlflow_url: str = "http://localhost:5001"
    scheduler_poll_seconds: float = 0.8
    # studio: validate QuicData JWT (production / integrated default).
    # open: development default (actor header optional).
    # local: email/password login → session Bearer (multi-user) — preferred for local+cloud.
    # bootstrap: require Bearer (bootstrap token and/or local session).
    # oidc: reserved; configure idp_* when wiring an external IdP.
    auth_mode: str = "studio"
    bootstrap_admin_token: str | None = None
    admin_email: str = "admin@quicrobot.com"
    admin_password: str | None = None
    session_ttl_hours: int = 168
    default_project_id: str = "prj_robot_arm"
    provider_disabled: bool = False
    default_max_concurrent_jobs: int = 16
    default_max_gpus: int = 16
    default_max_runtime_seconds: int = 86_400
    materialization_root: str | None = None
    export_root: str | None = None
    export_oss_prefix: str = "oss://quictrain-exports/exports"
    capacity_discovery_mode: str = "honest_unknown"
    capacity_gpu_model: str | None = "H20"
    capacity_total_gpus: int | None = None
    # pool_id:ResourceId pairs, e.g. cn-beijing-h20:quota1lnd98qodrh
    pool_resource_ids: str = ""
    # pool_id:capacity pairs, e.g. cn-beijing-h20:8
    pool_capacities: str = ""
    # pool_id:region pairs (optional overrides)
    pool_regions: str = ""
    # Optional fallback when pool map omits cn-beijing-h20
    default_dlc_resource_id: str | None = None
    # Dedicated DSW (ops messaging only — CreateJob still uses quota* ResourceId).
    dedicated_dsw_instance_id: str = "dsw-n1n8o1usk465z51gam"
    dedicated_dsw_name: str = "quic-train-5000"
    # Known DSW instances for ops list/verify (id:name:family, comma-separated).
    # family is 4090|h20 for UI grouping. Training still prefers aliyun_dlc.
    dsw_known_instances: str = (
        "dsw-fr1y76cr1n67t9vx7n:Nv4093:4090,"
        "dsw-if7gmtyy8qf0vep4jt:Nv4094--8card:4090,"
        "dsw-n1n8o1usk465z51gam:quic-train-5000:h20,"
        "dsw-c0me85cekg6m8prdf4:quic-post-train-4001:h20"
    )
    # Idle resource / resource-group stop policy: default NEVER auto-stop.
    # If operators later enable auto-stop, require idle ≥ this many minutes.
    auto_stop_idle_resources: bool = False
    idle_minutes_before_stop: int = 30
    auto_stop_resource_groups: bool = False
    # Hard protect H20 DSW instances + H20-5 resource group from casual Stop.
    # Ops/scripts must not stop these unless allow_h20_dsw_stop=true and force.
    protect_h20_dsw: bool = True
    protect_h20_resource_groups: bool = True
    allow_h20_dsw_stop: bool = False
    dsw_stop_protected_families: str = "h20"
    dsw_stop_protected_resource_ids: str = "quota1lnd98qodrh"
    alerts_webhook_url: str | None = None
    log_retention_days: int = 30
    sls_project: str | None = None
    sls_logstore: str | None = None
    oss_staging_root: str | None = None
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    # Dev-only: accept unsigned JWT payload JSON for oidc flow tests (never production).
    oidc_dev_unsigned: bool = False
    export_gc_enabled: bool = True
    break_glass_basic_auth_enabled: bool = False
    public_base_url: str | None = None
    stable_domain_ready: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def example_dataset_seed_enabled(self) -> bool:
        return self.seed_example_datasets and self.env in {"development", "test"}

    def resolved_materialization_root(self) -> str:
        if self.materialization_root:
            return self.materialization_root.rstrip("/")
        return f"{self.cpfs_mount_path.rstrip('/')}/{self.cpfs_workspace_dir}/datasets"

    def resolved_export_root(self) -> str:
        if self.export_root:
            return self.export_root.rstrip("/")
        return f"{self.artifact_root.rstrip('/')}/exports"

    def resolved_dlc_user_vpc(self) -> dict[str, object] | None:
        raw = (self.dlc_user_vpc_json or "").strip()
        if not raw:
            return None
        import ast
        import json
        import logging

        value: object | None = None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            # systemd Environment= often strips nested double quotes; accept
            # single-quoted / Python-literal forms written by operators.
            try:
                value = ast.literal_eval(raw)
            except (SyntaxError, ValueError):
                logging.getLogger(__name__).warning(
                    "QUICTRAIN_DLC_USER_VPC_JSON is not valid JSON; ignoring "
                    "(4090/ECS DLC submits need a real UserVpc). raw_prefix=%r",
                    raw[:48],
                )
                return None
        if not isinstance(value, dict) or not value.get("VpcId"):
            logging.getLogger(__name__).warning(
                "QUICTRAIN_DLC_USER_VPC_JSON must be an object with VpcId; ignoring"
            )
            return None
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
