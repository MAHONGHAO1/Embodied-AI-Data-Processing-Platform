"""Concurrency-safe state claim for legacy task audit acceptance."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from data.database import Task, TaskLog, TaskStatus
from data.services.state_machine import assert_legacy_task_transition_allowed

AUDIT_ACCEPTABLE_STATUSES = frozenset({"pending_audit", "annotate_done", "rejected"})


class AuditSubmissionConflict(ValueError):
    """The task was accepted or otherwise changed by another transaction."""


def claim_audit_acceptance(
    db: Session,
    task_id: int,
    *,
    operator: str,
    note: str = "",
) -> Task:
    task = db.scalar(
        select(Task)
        .where(Task.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is None:
        raise KeyError(f"task not found: {task_id}")
    assert_legacy_task_transition_allowed(db, task)
    expected_status = str(task.status)
    if expected_status not in AUDIT_ACCEPTABLE_STATUSES:
        raise AuditSubmissionConflict("task audit state changed concurrently; retry")

    now = datetime.utcnow()
    changed = db.execute(
        update(Task)
        .where(Task.id == task.id, Task.status == expected_status)
        .values(status=TaskStatus.AUDIT_PASSED.value, updated_at=now),
        execution_options={"synchronize_session": False},
    )
    if changed.rowcount != 1:
        raise AuditSubmissionConflict("task audit state changed concurrently; retry")
    db.add(
        TaskLog(
            task_id=task.id,
            action="audit_pass",
            from_status=expected_status,
            to_status=TaskStatus.AUDIT_PASSED.value,
            operator=operator,
            note=note,
        )
    )
    db.flush()
    db.expire(task)
    db.refresh(task)
    return task
