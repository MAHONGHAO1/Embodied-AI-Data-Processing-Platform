"""Collection management access control: admin only, and must belong to the workspace."""

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from data.database import User, Workspace, WorkspaceMember
from data.services.collection_access import require_collection_admin, require_collection_workspace


def _user(db: Session, *, email: str, role: str) -> User:
    user = User(email=email, password_hash="x", role=role, is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_require_collection_admin_rejects_annotator(db_session):
    user = _user(db_session, email=f"ann-{uuid4().hex}@t.com", role="annotator")
    with pytest.raises(PermissionError, match="admin"):
        require_collection_admin(db_session, actor_id=user.id)


def test_require_collection_workspace_rejects_outsider(db_session):
    admin = _user(db_session, email=f"adm-{uuid4().hex}@t.com", role="admin")
    workspace = Workspace(name=f"ws-{uuid4().hex}", creator=admin.email)
    db_session.add(workspace)
    db_session.commit()
    with pytest.raises(PermissionError):
        require_collection_workspace(db_session, actor_id=admin.id, workspace_id=workspace.id)


def test_require_collection_workspace_accepts_member_admin(db_session):
    admin = _user(db_session, email=f"adm-{uuid4().hex}@t.com", role="admin")
    workspace = Workspace(name=f"ws-{uuid4().hex}", creator=admin.email)
    db_session.add(workspace)
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=admin.id))
    db_session.commit()
    got = require_collection_workspace(db_session, actor_id=admin.id, workspace_id=workspace.id)
    assert got.id == workspace.id
