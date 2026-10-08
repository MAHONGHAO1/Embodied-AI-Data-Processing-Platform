"""Workspace scope resolution (tasks/datasets associate with workspace, project_id still retained internally)."""

from sqlalchemy.orm import Session

from data.database import Project, Task


def project_ids_for_workspace(db: Session, workspace_id: int) -> list[int]:
    return [p.id for p in db.query(Project).filter(Project.workspace_id == workspace_id).all()]


def ensure_default_project(db: Session, workspace_id: int) -> Project:
    project = (
        db.query(Project).filter(Project.workspace_id == workspace_id).order_by(Project.id).first()
    )
    if project:
        return project
    project = Project(
        workspace_id=workspace_id, name="默认", description="工作空间默认数据容器", scene=""
    )
    db.add(project)
    db.flush()
    return project


def resolve_task_workspace_id(db: Session, task: Task) -> int | None:
    if getattr(task, "workspace_id", None):
        return task.workspace_id
    project = db.get(Project, task.project_id)
    return project.workspace_id if project else None


def task_workspace_filter(db: Session, workspace_id: int):
    """Filter Task query by workspace_id (prefers column, falls back to project association)."""
    from sqlalchemy import or_

    pids = project_ids_for_workspace(db, workspace_id)
    clauses = [Task.workspace_id == workspace_id]
    if pids:
        clauses.append(Task.project_id.in_(pids))
    return or_(*clauses)


def offline_candidate_workspace_filter(workspace_id: int):
    """Restrict offline candidates to records whose workspace is resolved."""
    from data.database import EgoSourceCandidate

    return EgoSourceCandidate.workspace_id == workspace_id
