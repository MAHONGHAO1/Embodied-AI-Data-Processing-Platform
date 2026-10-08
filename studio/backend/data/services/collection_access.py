"""Unified authentication and authorization for collection management API."""

from __future__ import annotations

from sqlalchemy.orm import Session

from data.database import User, Workspace, WorkspaceMember
from data.services.workspace_access import require_actor, require_workspace_actor


def require_collection_admin(db: Session, *, actor_id: int | None) -> User:
    """Phase 1 spec: collection project management/assignment is admin-only."""
    actor = require_actor(db, actor_id=actor_id)
    if actor.role != "admin":
        raise PermissionError("only an admin can manage collection resources")
    return actor


def require_collection_workspace(
    db: Session, *, actor_id: int | None, workspace_id: int
) -> Workspace:
    """Verify admin first, then verify workspace membership."""
    actor = require_collection_admin(db, actor_id=actor_id)
    membership = (
        db.query(WorkspaceMember)
        .filter(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == actor.id,
        )
        .one_or_none()
    )
    if membership is None:
        raise PermissionError("workspace access denied")
    require_workspace_actor(db, actor_id=actor_id, workspace_id=workspace_id)
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise ValueError("workspace does not exist")
    return workspace
