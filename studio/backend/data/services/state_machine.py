"""Task state machine service."""

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from data.database import TASK_TRANSITIONS, Task, TaskLog, TaskStatus, WorkItem

logger = logging.getLogger(__name__)


ACTION_MAP = {
    "collect_done": TaskStatus.COLLECT_DONE,
    "collect_reject": TaskStatus.PENDING_COLLECT,
    "start_preprocess": TaskStatus.PENDING_PREPROCESS,
    "preprocess_done": TaskStatus.PREPROCESS_DONE,
    "submit_annotate": TaskStatus.PENDING_ANNOTATE,
    "annotate_done": TaskStatus.ANNOTATE_DONE,
    "submit_audit": TaskStatus.PENDING_AUDIT,
    "audit_pass": TaskStatus.AUDIT_PASSED,
    "audit_reject": TaskStatus.REJECTED,
    "storage_ready": TaskStatus.STORAGE_READY,
    "retry": TaskStatus.PENDING_COLLECT,
    "fail": TaskStatus.FAILED,
    # 4.2 doc action alias
    "APPROVE": TaskStatus.AUDIT_PASSED,
    "REJECT": TaskStatus.REJECTED,
    "SUBMIT_ANNOTATE": TaskStatus.PENDING_ANNOTATE,
    "ANNOTATE_DONE": TaskStatus.ANNOTATE_DONE,
    "SUBMIT_AUDIT": TaskStatus.PENDING_AUDIT,
    "COMPLETE": TaskStatus.STORAGE_READY,
}

WORK_ITEM_TRANSITION_ERROR = "WorkItem-managed tasks reject legacy status transitions"


class WorkItemManagedTaskError(ValueError):
    """Raised when a legacy transition targets a WorkItem-managed task."""


def assert_legacy_task_transition_allowed(db: Session, task: Task) -> None:
    """Keep WorkItem-managed task status under the aggregate projection only."""
    if task.task_type == "ego":
        raise WorkItemManagedTaskError(WORK_ITEM_TRANSITION_ERROR)
    has_work_items = db.query(WorkItem.id).filter(WorkItem.task_id == task.id).first() is not None
    if has_work_items:
        raise WorkItemManagedTaskError(WORK_ITEM_TRANSITION_ERROR)


def normalize_action(action: str) -> str:
    """Map 4.2 doc action to internal action."""
    alias = {
        "APPROVE": "audit_pass",
        "REJECT": "audit_reject",
        "SUBMIT_ANNOTATE": "submit_annotate",
        "ANNOTATE_DONE": "annotate_done",
        "SUBMIT_AUDIT": "submit_audit",
        "COMPLETE": "storage_ready",
    }
    return alias.get(action.upper(), action)


def transit_task(
    db: Session,
    task: Task,
    action: str,
    operator: str = "system",
    note: str = "",
    *,
    commit: bool = True,
) -> Task:
    assert_legacy_task_transition_allowed(db, task)
    internal_action = normalize_action(action)
    current = TaskStatus(task.status)
    target = ACTION_MAP.get(action) or ACTION_MAP.get(internal_action)
    if not target:
        raise ValueError(f"未知操作: {action}")

    allowed = TASK_TRANSITIONS.get(current, [])
    if target not in allowed:
        raise ValueError(f"不允许从 {current.value} 流转到 {target.value}")

    log = TaskLog(
        task_id=task.id,
        action=internal_action,
        from_status=current.value,
        to_status=target.value,
        operator=operator,
        note=note,
    )
    task.status = target.value
    task.updated_at = datetime.utcnow()
    if internal_action in ("audit_reject", "REJECT", "collect_reject") and note:
        task.reject_reason = note

    db.add(log)
    if commit:
        db.commit()
        db.refresh(task)
    else:
        db.flush()

    # Terminal cleanup: release cloud mirror and preview cache on reject/fail/terminate/intake reject
    if commit and (
        target in (TaskStatus.REJECTED, TaskStatus.FAILED) or internal_action == "collect_reject"
    ):
        try:
            from data.services.cloud_storage import cleanup_task_terminal_storage

            cleanup_task_terminal_storage(task)
        except Exception:
            logger.exception("任务终态本地缓存清理失败 task=%s", task.id)

    return task


def auto_advance_after_collect(db: Session, task: Task, operator: str = "system") -> Task:
    task = transit_task(db, task, "collect_done", operator)
    task = transit_task(db, task, "start_preprocess", operator)
    return task


def auto_advance_after_preprocess(db: Session, task: Task, operator: str = "system") -> Task:
    task = transit_task(db, task, "preprocess_done", operator)
    task = transit_task(db, task, "submit_annotate", operator)
    return task


def ensure_ready_for_preprocess(db: Session, task: Task, operator: str = "system") -> Task:
    """Ensure task is in pending_preprocess, automatically completing missing transition steps."""
    initial = TaskStatus(task.status)
    if (
        initial in (TaskStatus.PENDING_COLLECT, TaskStatus.REJECTED, TaskStatus.FAILED)
        and not task.storage_path
    ):
        raise ValueError("任务无数据文件，请先在数据接入页上传文件")
    if task.task_type != "ego":
        from data.services.provenance import assert_collection_attribution_complete

        assert_collection_attribution_complete(db, task)
    task = recover_task_state(db, task, operator)
    current = TaskStatus(task.status)
    if current == TaskStatus.PENDING_PREPROCESS:
        return task
    if current == TaskStatus.PENDING_COLLECT and task.storage_path:
        return auto_advance_after_collect(db, task, operator)
    if current == TaskStatus.COLLECT_DONE:
        return transit_task(db, task, "start_preprocess", operator)
    if current in (TaskStatus.REJECTED, TaskStatus.FAILED):
        if not task.storage_path:
            raise ValueError("任务无数据文件，请先在数据接入页上传文件")
        return transit_task(db, task, "start_preprocess", operator, "重试预处理")
    raise ValueError(f"任务状态 {current.value} 不可执行预处理")


def recover_task_state(db: Session, task: Task, operator: str = "system") -> Task:
    """Fix inconsistent tasks where preprocessing completed but state did not advance."""
    meta = task.metadata_json or {}
    preprocess = meta.get("preprocess", {})
    if preprocess.get("status") != "done":
        return task

    current = TaskStatus(task.status)
    if current in (TaskStatus.PENDING_ANNOTATE, TaskStatus.ANNOTATE_DONE, TaskStatus.PENDING_AUDIT):
        return task

    if current == TaskStatus.PENDING_COLLECT and task.storage_path:
        task = auto_advance_after_collect(db, task, operator)
        current = TaskStatus(task.status)

    if current == TaskStatus.COLLECT_DONE:
        task = transit_task(db, task, "start_preprocess", operator, "状态修复")
        current = TaskStatus(task.status)

    if current == TaskStatus.PENDING_PREPROCESS:
        task = auto_advance_after_preprocess(db, task, operator)

    return task
