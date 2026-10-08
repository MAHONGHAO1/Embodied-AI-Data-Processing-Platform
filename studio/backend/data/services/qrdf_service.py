"""QRDF asset statistics (P1)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from data.database import QrdfData
from data.integrations.qrdf import list_episodes, resolve_dataset_path
from data.services.workspace_access import qrdf_access_filter


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
    except OSError:
        pass
    return total


def compute_qrdf_stats(
    db: Session,
    *,
    project_id: int | None = None,
    actor_id: int | None,
) -> dict:
    q = db.query(QrdfData).filter(qrdf_access_filter(db, actor_id=actor_id))
    if project_id:
        q = q.filter(QrdfData.project_id == project_id)
    records = q.all()

    total_episodes = 0
    valid_episodes = 0
    storage_bytes = 0

    for record in records:
        episodes = list_episodes(record.storage_path)
        total_episodes += len(episodes)
        if record.quality_level == "valid":
            valid_episodes += len(episodes)
        dataset_path = resolve_dataset_path(record.storage_path)
        if dataset_path and dataset_path.is_dir():
            storage_bytes += _dir_size(dataset_path)

    return {
        "qrdf_count": len(records),
        "episode_count": total_episodes,
        "valid_episode_count": valid_episodes,
        "storage_bytes": storage_bytes,
        "storage_mb": round(storage_bytes / (1024 * 1024), 2),
    }
