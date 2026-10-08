"""Preprocessing service: long-running execution is always delegated to media Celery worker."""

from data.services.preprocess_jobs import run_preprocess
from data.services.state_machine import transit_task
from data.services.task_dispatcher import (
    cancel_preprocess_job,
    dispatch_preprocess,
    dispatch_preprocess_batch,
)

# Compatibility with legacy import
start_preprocess = dispatch_preprocess


def terminate_preprocess(db, task, operator: str):
    cancel_preprocess_job(task.id)
    meta = dict(task.metadata_json or {})
    meta["preprocess"] = {
        **meta.get("preprocess", {}),
        "status": "terminated",
        "message": "用户终止预处理",
    }
    task.metadata_json = meta
    db.commit()
    return transit_task(db, task, "fail", operator, "预处理被终止")


__all__ = [
    "start_preprocess",
    "terminate_preprocess",
    "run_preprocess",
    "dispatch_preprocess",
    "dispatch_preprocess_batch",
]
