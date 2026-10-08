"""Batch rows owned by the retained native LeRobot dataset registry."""

from __future__ import annotations

from sqlalchemy.orm import Session

from data.database import Batch, TaskSet, User

VALID_BATCH_TYPES = frozenset({"ego", "teleop", "lerobot"})


def _required_text(value: str, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field} must contain 1-{maximum} characters")
    return normalized


def create_batch(
    db: Session,
    *,
    workspace_id: int,
    task_set_id: int,
    name: str,
    batch_type: str,
    actor_id: int | None,
    metadata_json: dict | None = None,
) -> Batch:
    task_set = db.get(TaskSet, task_set_id)
    if task_set is None or task_set.workspace_id != workspace_id:
        raise ValueError("task set does not belong to workspace")
    if actor_id is not None and db.get(User, actor_id) is None:
        raise ValueError("batch actor does not exist")
    if batch_type not in VALID_BATCH_TYPES:
        raise ValueError("unsupported batch_type")
    batch = Batch(
        workspace_id=workspace_id,
        task_set_id=task_set_id,
        name=_required_text(name, "name", 256),
        batch_type=batch_type,
        created_by_user_id=actor_id,
        metadata_json=dict(metadata_json or {}),
    )
    db.add(batch)
    db.flush()
    return batch
