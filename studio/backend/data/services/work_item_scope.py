"""Workspace context for annotation and review work items.

Annotation and review are not scoped by workspace membership: admins see every
item and other users see the items assigned to them. The workspace is kept as a
list filter, so the console needs the workspaces those visible items belong to.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from data.database import User, Workspace


def work_item_workspaces(db: Session, model, *, actor: User) -> list[dict[str, object]]:
    """Distinct workspaces of the work items (``model``) visible to ``actor``."""
    query = db.query(Workspace.id, Workspace.name).join(model, model.workspace_id == Workspace.id)
    if actor.role != "admin":
        query = query.filter(model.assignee_user_id == actor.id)
    rows = query.distinct().order_by(Workspace.name.asc(), Workspace.id.asc()).all()
    return [{"id": workspace_id, "name": name} for workspace_id, name in rows]
