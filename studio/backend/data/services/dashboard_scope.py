"""Stable tenant scope values for dashboard warehouse reads and writes."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from data.database import TaskSet
from data.services.workspace_access import require_workspace_actor
from data.utils.helpers import require_permission


@dataclass(frozen=True, slots=True)
class DashboardScope:
    key: str
    workspace_id: int | None
    task_set_id: int | None

    @classmethod
    def global_scope(cls) -> DashboardScope:
        return cls(key="global", workspace_id=None, task_set_id=None)

    @classmethod
    def for_workspace(cls, workspace_id: int) -> DashboardScope:
        workspace_id = _positive_id(workspace_id, "workspace_id")
        return cls(
            key=f"workspace:{workspace_id}",
            workspace_id=workspace_id,
            task_set_id=None,
        )

    @classmethod
    def for_task_set(cls, *, workspace_id: int, task_set_id: int) -> DashboardScope:
        workspace_id = _positive_id(workspace_id, "workspace_id")
        task_set_id = _positive_id(task_set_id, "task_set_id")
        return cls(
            key=f"task-set:{task_set_id}",
            workspace_id=workspace_id,
            task_set_id=task_set_id,
        )

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "key": self.key,
            "workspace_id": self.workspace_id,
            "task_set_id": self.task_set_id,
        }

    @classmethod
    def from_job_detail(cls, detail: object) -> DashboardScope:
        if not isinstance(detail, dict):
            raise ValueError("dashboard job scope is missing")
        raw_workspace_id = detail.get("workspace_id")
        raw_task_set_id = detail.get("task_set_id")
        if raw_workspace_id is None and raw_task_set_id is None:
            scope = cls.global_scope()
        elif raw_workspace_id is None:
            raise ValueError("dashboard job workspace scope is missing")
        elif raw_task_set_id is None:
            scope = cls.for_workspace(raw_workspace_id)
        else:
            scope = cls.for_task_set(
                workspace_id=raw_workspace_id,
                task_set_id=raw_task_set_id,
            )
        if detail.get("scope_key") != scope.key:
            raise ValueError("dashboard job scope key is inconsistent")
        return scope


def resolve_dashboard_scope(
    db: Session,
    *,
    user: dict,
    permission: str,
    workspace_id: int | None,
    task_set_id: int | None,
) -> DashboardScope:
    """Resolve an authorized request scope before any warehouse read or write."""
    require_permission(user, permission)
    if workspace_id is None:
        if task_set_id is not None:
            raise ValueError("workspace_id is required when task_set_id is provided")
        if str(user.get("role") or "") != "admin":
            raise PermissionError("global dashboard is admin only")
        return DashboardScope.global_scope()

    actor_id = _actor_id(user)
    require_workspace_actor(db, actor_id=actor_id, workspace_id=workspace_id)
    if task_set_id is None:
        return DashboardScope.for_workspace(workspace_id)

    task_set = db.get(TaskSet, task_set_id)
    if task_set is None or int(task_set.workspace_id) != int(workspace_id):
        raise PermissionError("task set is outside the requested workspace")
    return DashboardScope.for_task_set(
        workspace_id=workspace_id,
        task_set_id=task_set_id,
    )


def _positive_id(value: int, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return parsed


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub"))
    except (TypeError, ValueError):
        return None
