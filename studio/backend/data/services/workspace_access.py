"""Default-deny authorization helpers for Batch / Episode resources."""

from __future__ import annotations

from sqlalchemy import false, true
from sqlalchemy.orm import Session

from data.database import (
    Batch,
    Episode,
    EpisodeArtifact,
    ImportSession,
    TaskSet,
    User,
    Workspace,
    WorkspaceMember,
)


def require_actor(db: Session, *, actor_id: int | None) -> User:
    actor = db.get(User, actor_id) if actor_id else None
    if actor is None or not actor.is_active:
        raise PermissionError("workspace actor is unavailable")
    return actor


def require_workspace_actor(
    db: Session, *, actor_id: int | None, workspace_id: int, require_membership: bool = False
) -> User:
    actor = require_actor(db, actor_id=actor_id)
    if db.get(Workspace, workspace_id) is None:
        raise ValueError("workspace does not exist")
    if actor.role == "admin" and not require_membership:
        return actor
    membership = (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == actor.id)
        .one_or_none()
    )
    if membership is None:
        raise PermissionError("workspace access denied")
    return actor


def accessible_workspace_ids(db: Session, *, actor_id: int | None) -> tuple[int, ...] | None:
    actor = require_actor(db, actor_id=actor_id)
    if actor.role == "admin":
        return None
    return tuple(
        workspace_id
        for (workspace_id,) in db.query(WorkspaceMember.workspace_id)
        .filter(WorkspaceMember.user_id == actor.id)
        .all()
    )


def workspace_access_filter(db: Session, *, actor_id: int | None, workspace_column):
    workspace_ids = accessible_workspace_ids(db, actor_id=actor_id)
    if workspace_ids is None:
        return true()
    if not workspace_ids:
        return false()
    return workspace_column.in_(workspace_ids)


def require_task_set_actor(db: Session, *, actor_id: int | None, task_set_id: int) -> User:
    task_set = db.get(TaskSet, task_set_id)
    if task_set is None:
        raise ValueError("task set does not exist")
    return require_workspace_actor(db, actor_id=actor_id, workspace_id=task_set.workspace_id)


def require_batch_actor(db: Session, *, actor_id: int | None, batch: Batch) -> User:
    return require_workspace_actor(db, actor_id=actor_id, workspace_id=batch.workspace_id)


def require_import_session_actor(
    db: Session, *, actor_id: int | None, import_session: ImportSession
) -> User:
    batch = db.get(Batch, import_session.batch_id)
    if batch is None:
        raise ValueError("import session batch is unavailable")
    return require_batch_actor(db, actor_id=actor_id, batch=batch)


def require_episode_actor(db: Session, *, actor_id: int | None, episode: Episode) -> User:
    return require_workspace_actor(db, actor_id=actor_id, workspace_id=episode.workspace_id)


def require_artifact_actor(db: Session, *, actor_id: int | None, artifact: EpisodeArtifact) -> User:
    if artifact.episode_id is not None:
        episode = db.get(Episode, artifact.episode_id)
        if episode is None:
            raise ValueError("artifact episode is unavailable")
        return require_episode_actor(db, actor_id=actor_id, episode=episode)
    if artifact.import_session_id:
        import_session = db.get(ImportSession, artifact.import_session_id)
        if import_session is None:
            raise ValueError("artifact import session is unavailable")
        return require_import_session_actor(db, actor_id=actor_id, import_session=import_session)
    raise ValueError("artifact owner is unavailable")
