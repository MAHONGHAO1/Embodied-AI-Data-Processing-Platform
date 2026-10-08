"""Global workbench / data overview API."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from data.database import PIPELINE_FLOW_STAGES, Dataset, Project, QrdfData, Task, Workspace, get_db
from data.services.workspace_access import (
    accessible_workspace_ids,
    project_access_filter,
    qrdf_access_filter,
    task_access_filter,
)
from data.services.workspace_scope import resolve_task_workspace_id
from data.utils.formatting import format_api_datetime
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/overview", tags=["全局总览"])


def _actor_id(user: dict) -> int | None:
    try:
        raw = user.get("sub") or user.get("id")
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    require_permission(user, "workspace:read")

    actor_id = _actor_id(user)
    task_query = db.query(Task).filter(task_access_filter(db, actor_id=actor_id))
    total_tasks = task_query.count()
    completed = task_query.filter(Task.status == "storage_ready").count()
    pending_audit = task_query.filter(Task.status == "pending_audit").count()
    pending_annotate = task_query.filter(Task.status == "pending_annotate").count()
    pending_preprocess = task_query.filter(Task.status == "pending_preprocess").count()
    pending_collect = task_query.filter(Task.status == "pending_collect").count()
    rejected = task_query.filter(Task.status == "rejected").count()
    failed = task_query.filter(Task.status == "failed").count()
    qrdf_count = db.query(QrdfData).filter(qrdf_access_filter(db, actor_id=actor_id)).count()
    dataset_count = (
        db.query(Dataset)
        .filter(project_access_filter(db, actor_id=actor_id, project_column=Dataset.project_id))
        .count()
    )
    workspace_ids = accessible_workspace_ids(db, actor_id=actor_id)
    workspace_query = db.query(Workspace)
    if workspace_ids is not None:
        workspace_query = workspace_query.filter(Workspace.id.in_(workspace_ids))
    workspace_count = workspace_query.count()
    project_count = (
        db.query(Project)
        .filter(project_access_filter(db, actor_id=actor_id, project_column=Project.id))
        .count()
    )

    funnel = []
    for stage in PIPELINE_FLOW_STAGES:
        count = sum(task_query.filter(Task.status == s).count() for s in stage["statuses"])
        funnel.append({"stage": stage["key"], "label": stage["label"], "count": count})

    recent_tasks = task_query.order_by(Task.updated_at.desc()).limit(8).all()
    queues = [
        {
            "label": "待收集",
            "count": pending_collect,
            "route": "/data-import",
            "status": "pending_collect",
        },
        {
            "label": "待预处理",
            "count": pending_preprocess,
            "route": "/data-cleaning",
            "status": "pending_preprocess",
        },
        {
            "label": "待标注",
            "count": pending_annotate,
            "route": "/annotate",
            "status": "pending_annotate",
        },
        {"label": "待审核", "count": pending_audit, "route": "/review", "status": "pending_audit"},
        {"label": "已驳回", "count": rejected, "route": "/review", "status": "rejected"},
        {
            "label": "入库就绪",
            "count": completed,
            "route": "/data-query",
            "status": "storage_ready",
        },
    ]

    return success(
        {
            "stats": {
                "total_tasks": total_tasks,
                "completed": completed,
                "pending_audit": pending_audit,
                "pending_annotate": pending_annotate,
                "pending_preprocess": pending_preprocess,
                "pending_collect": pending_collect,
                "rejected": rejected,
                "failed": failed,
                "qrdf_count": qrdf_count,
                "dataset_count": dataset_count,
                "workspace_count": workspace_count,
                "project_count": project_count,
            },
            "funnel": funnel,
            "queues": queues,
            "recent_tasks": [
                {
                    "id": t.id,
                    "project_id": t.project_id,
                    "workspace_id": resolve_task_workspace_id(db, t),
                    "name": t.name,
                    "status": t.status,
                    "data_source": t.data_source,
                    "updated_at": format_api_datetime(t.updated_at),
                }
                for t in recent_tasks
            ],
        }
    )


@router.get("/pipeline")
def pipeline(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    require_permission(user, "task:read")
    task_query = db.query(Task).filter(task_access_filter(db, actor_id=_actor_id(user)))
    stages = []
    for stage in PIPELINE_FLOW_STAGES:
        count = sum(task_query.filter(Task.status == s).count() for s in stage["statuses"])
        stages.append({"key": stage["key"], "label": stage["label"], "count": count})
    return success({"stages": stages})
