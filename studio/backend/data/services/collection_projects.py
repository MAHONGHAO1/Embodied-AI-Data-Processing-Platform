"""Collection project operations."""

from __future__ import annotations

from sqlalchemy.orm import Session

from data.database import User
from data.models.collection_core import CollectionProject
from data.services.resource_names import normalized_name


class ArchivedCollectionProjectError(ValueError):
    """Raised when an archived project is modified."""


def _require_active_owner(db: Session, owner_user_id: int | None) -> None:
    if owner_user_id is None:
        return
    owner = db.get(User, owner_user_id)
    if owner is None or not owner.is_active:
        raise LookupError("active project owner does not exist")


def create_collection_project(
    db: Session,
    *,
    workspace_id: int,
    name: str,
    description: str = "",
    owner_user_id: int | None = None,
) -> CollectionProject:
    _require_active_owner(db, owner_user_id)
    project = CollectionProject(
        workspace_id=workspace_id,
        name=normalized_name(name),
        description=description.strip(),
        owner_user_id=owner_user_id,
        status="enabled",
    )
    db.add(project)
    db.flush()
    return project


def update_collection_project(
    db: Session,
    *,
    project_id: int,
    workspace_id: int,
    changes: dict[str, object],
) -> CollectionProject:
    project = (
        db.query(CollectionProject)
        .filter(
            CollectionProject.id == project_id,
            CollectionProject.workspace_id == workspace_id,
        )
        .with_for_update()
        .one_or_none()
    )
    if project is None:
        raise LookupError("collection project does not exist")
    if project.status != "enabled":
        raise ArchivedCollectionProjectError("archived collection project cannot be modified")
    if "name" in changes:
        project.name = normalized_name(str(changes["name"]))
    if "description" in changes:
        project.description = str(changes["description"] or "").strip()
    if "owner_user_id" in changes:
        _require_active_owner(db, changes["owner_user_id"])
        project.owner_user_id = changes["owner_user_id"]
    db.flush()
    return project


def archive_collection_project(
    db: Session,
    *,
    project_id: int,
    workspace_id: int,
) -> CollectionProject:
    project = (
        db.query(CollectionProject)
        .filter(
            CollectionProject.id == project_id,
            CollectionProject.workspace_id == workspace_id,
        )
        .one_or_none()
    )
    if project is None:
        raise LookupError("collection project does not exist")
    if project.status != "archived":
        project.status = "archived"
        db.flush()
    return project
