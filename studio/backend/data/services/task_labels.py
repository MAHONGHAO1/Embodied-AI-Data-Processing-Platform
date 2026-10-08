"""Controlled task-label vocabulary used by Batch / Episode imports."""

from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from data.database import TaskLabel

_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")


def task_label_summary(label: TaskLabel) -> dict[str, object]:
    return {
        "id": label.id,
        "key": label.key,
        "name": label.name,
        "description": label.description,
    }


def task_label_brief(label: TaskLabel | None) -> dict[str, object] | None:
    """Return the controlled label identity safe for Batch/Episode projections."""
    if label is None:
        return None
    return {
        "id": label.id,
        "key": label.key,
        "name": label.name,
    }


def list_task_labels(db: Session, *, limit: int, offset: int) -> tuple[list[TaskLabel], int]:
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError("task label pagination is invalid")
    total = int(db.scalar(select(func.count(TaskLabel.id))) or 0)
    labels = list(
        db.scalars(
            select(TaskLabel)
            .order_by(TaskLabel.name.asc(), TaskLabel.key.asc(), TaskLabel.id.asc())
            .offset(offset)
            .limit(limit)
        )
    )
    return labels, total


def create_task_label(
    db: Session,
    *,
    key: str,
    name: str,
    description: str = "",
) -> TaskLabel:
    normalized_key = str(key or "").strip().lower()
    normalized_name = str(name or "").strip()
    normalized_description = str(description or "").strip()
    if not _KEY.fullmatch(normalized_key):
        raise ValueError("task label key is invalid")
    if not normalized_name or len(normalized_name) > 128:
        raise ValueError("task label name is invalid")
    if len(normalized_description) > 4_000:
        raise ValueError("task label description is invalid")
    if db.scalar(select(TaskLabel.id).where(TaskLabel.key == normalized_key)) is not None:
        raise ValueError("task label key already exists")
    try:
        with db.begin_nested():
            label = TaskLabel(
                key=normalized_key,
                name=normalized_name,
                description=normalized_description,
            )
            db.add(label)
            db.flush()
    except IntegrityError as exc:
        db.expire_all()
        raise ValueError("task label key already exists") from exc
    return label
