"""Global collector identities and workspace membership helpers."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from data.database import PersonnelProfile, WorkspacePersonnelProfile
from data.services.resource_names import is_constraint_conflict, normalized_name
from data.services.workspace_access import require_actor

PROFILE_KEY_PATTERN = r"^[1-9][0-9]*$"


def available_collector_profile(
    db: Session, *, workspace_id: int, profile_id: int
) -> PersonnelProfile | None:
    """Only active workspace members can be newly selected for attribution."""
    return db.scalar(
        select(PersonnelProfile)
        .join(
            WorkspacePersonnelProfile,
            WorkspacePersonnelProfile.personnel_profile_id == PersonnelProfile.id,
        )
        .where(
            PersonnelProfile.id == profile_id,
            PersonnelProfile.is_active.is_(True),
            WorkspacePersonnelProfile.workspace_id == workspace_id,
        )
    )


def require_collector_profile_admin(db: Session, *, actor_id: int | None) -> None:
    """Global identity changes affect all member workspaces, so require an admin."""
    actor = require_actor(db, actor_id=actor_id)
    if actor.role != "admin":
        raise PermissionError("only an admin can update global collector profiles")


class CollectorProfileKeyConflict(ValueError):
    """The requested collector number is already used in the workspace."""


class CollectorProfileKeySpaceExhausted(ValueError):
    """No collector number can be allocated."""


def collector_display_label(profile: PersonnelProfile | None) -> str:
    if profile is None:
        return ""
    return profile.name


def collector_profile_brief(profile: PersonnelProfile) -> dict[str, object]:
    return {
        "id": profile.id,
        "name": profile.name,
        "profile_key": profile.profile_key,
        "display_label": collector_display_label(profile),
    }


def collector_profile_item(profile: PersonnelProfile) -> dict[str, object]:
    return {
        **collector_profile_brief(profile),
        "is_active": bool(profile.is_active),
    }


def create_collector_profile(
    db: Session,
    *,
    workspace_id: int,
    name: str,
    profile_key: str | None,
) -> PersonnelProfile:
    name = normalized_name(name)
    if profile_key is not None:
        if not re.fullmatch(PROFILE_KEY_PATTERN, str(profile_key)):
            raise CollectorProfileKeyConflict("collector profile number must be a positive integer")
        profile_key = str(profile_key)
        profile = _insert_profile(
            db,
            workspace_id=None,
            name=name,
            profile_key=profile_key,
            conflict_is_terminal=True,
        )
        add_workspace_membership(db, workspace_id=workspace_id, profile_id=profile.id)
        return profile

    # Numeric allocation is intentionally represented as a decimal string so IDs
    # remain lossless in JSON and are not limited by a display width.
    keys = [key for (key,) in db.query(PersonnelProfile.profile_key).all()]
    number = max((int(key) for key in keys if str(key).isdigit()), default=0) + 1
    profile = _insert_profile(
        db, workspace_id=None, name=name, profile_key=str(number), conflict_is_terminal=False
    )
    add_workspace_membership(db, workspace_id=workspace_id, profile_id=profile.id)
    return profile


def _insert_profile(
    db: Session,
    *,
    workspace_id: int | None,
    name: str,
    profile_key: str,
    conflict_is_terminal: bool,
) -> PersonnelProfile:
    try:
        with db.begin_nested():
            profile = PersonnelProfile(
                workspace_id=workspace_id,
                name=name,
                profile_key=profile_key,
                is_active=True,
            )
            db.add(profile)
            db.flush()
        return profile
    except IntegrityError as exc:
        if not is_constraint_conflict(exc, "uq_personnel_profiles_profile_key"):
            raise
        if conflict_is_terminal:
            raise CollectorProfileKeyConflict("collector profile number already exists") from exc
        raise CollectorProfileKeyConflict(profile_key) from exc


def add_workspace_membership(db: Session, *, workspace_id: int, profile_id: int) -> None:
    """Attach a global collector identity to a workspace idempotently."""
    exists = (
        db.query(WorkspacePersonnelProfile)
        .filter_by(workspace_id=workspace_id, personnel_profile_id=profile_id)
        .first()
    )
    if exists is None:
        db.add(
            WorkspacePersonnelProfile(workspace_id=workspace_id, personnel_profile_id=profile_id)
        )


def remove_workspace_membership(db: Session, *, workspace_id: int, profile_id: int) -> bool:
    """Detach a global collector identity from a workspace."""
    row = (
        db.query(WorkspacePersonnelProfile)
        .filter_by(workspace_id=workspace_id, personnel_profile_id=profile_id)
        .first()
    )
    if row is not None:
        db.delete(row)
        return True
    return False


def list_available_collector_profiles(
    db: Session, *, workspace_id: int, query: str | None = None
) -> list[PersonnelProfile]:
    """List active global collectors that are not yet members of the given workspace."""
    subq = select(WorkspacePersonnelProfile.personnel_profile_id).where(
        WorkspacePersonnelProfile.workspace_id == workspace_id
    )
    stmt = select(PersonnelProfile).where(
        PersonnelProfile.is_active.is_(True),
        PersonnelProfile.id.not_in(subq),
    )
    if query:
        term = f"%{query.strip()}%"
        stmt = stmt.where(
            (PersonnelProfile.name.ilike(term)) | (PersonnelProfile.profile_key.ilike(term))
        )
    return list(
        db.scalars(
            stmt.order_by(PersonnelProfile.name.asc(), PersonnelProfile.profile_key.asc())
        ).all()
    )
