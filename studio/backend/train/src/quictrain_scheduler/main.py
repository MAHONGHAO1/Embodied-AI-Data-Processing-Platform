from __future__ import annotations

import logging
import os
import time

from quictrain_api.db import SessionLocal, init_database
from quictrain_api.runtime import get_artifact_client, get_provider
from quictrain_api.service import seed_catalog
from quictrain_api.settings import get_settings

from .engine import Scheduler
from .tracking import MLflowTrackingClient


def _default_oss_cpfs_mirror_prefix(settings) -> str | None:
    if settings.oss_cpfs_mirror_prefix:
        return settings.oss_cpfs_mirror_prefix.rstrip("/")
    bucket = os.environ.get("ALIYUN_OSS_BUCKET", "").strip()
    if bucket:
        return f"oss://{bucket}/quictrain"
    if settings.artifact_root.startswith("oss://"):
        root = settings.artifact_root.rstrip("/")
        if root.endswith("/runs"):
            return root[: -len("/runs")]
    return None


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    init_database()
    with SessionLocal() as session:
        seed_catalog(session)
    scheduler = Scheduler(
        SessionLocal,
        get_provider(),
        artifact_root=settings.artifact_root,
        artifact_client=get_artifact_client(),
        tracking_client=MLflowTrackingClient(settings.mlflow_tracking_uri),
        cpfs_data_source_id=settings.cpfs_data_source_id,
        cpfs_vpc_data_source_id=settings.cpfs_vpc_data_source_id,
        cpfs_vpc_mount_target=settings.cpfs_vpc_mount_target,
        cpfs_root_uri=settings.cpfs_root_uri,
        cpfs_mount_path=settings.cpfs_mount_path,
        cpfs_workspace_dir=settings.cpfs_workspace_dir,
        cpfs_pi05_pretrained_dir=settings.cpfs_pi05_pretrained_dir,
        dlc_user_vpc=settings.resolved_dlc_user_vpc(),
        oss_cpfs_mirror_prefix=_default_oss_cpfs_mirror_prefix(settings),
    )
    while True:
        scheduler.run_once()
        time.sleep(settings.scheduler_poll_seconds)


if __name__ == "__main__":
    main()
