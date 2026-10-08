import pytest

from data.database import Batch, TaskSet, User, Workspace, WorkspaceMember
from data.realtime.auth import RealtimeAuthenticationError, authenticate_socket
from data.realtime.subscriptions import SubscriptionDenied, resolve_resource_subscription
from data.utils.helpers import create_access_token, hash_password


def _socket_token(user: User) -> str:
    return create_access_token(
        user.id,
        user.email,
        user.role,
        realtime_session_epoch=user.realtime_session_epoch,
    )


def test_logout_invalidates_existing_realtime_session(client, db_session):
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@quicdata.com", "password": "admin123"},
    )
    assert login.status_code == 200
    token = login.json()["data"]["access_token"]
    user = db_session.query(User).filter(User.email == "admin@quicdata.com").one()
    previous_epoch = user.realtime_session_epoch

    response = client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    db_session.expire_all()
    assert db_session.get(User, user.id).realtime_session_epoch == previous_epoch + 1
    with pytest.raises(RealtimeAuthenticationError, match="revoked"):
        authenticate_socket(
            db_session,
            auth={"access_token": token},
            origin="http://127.0.0.1:8000",
        )


def test_role_change_invalidates_existing_realtime_session(client, admin_headers, db_session):
    target = User(
        email="realtime-role-target@example.com",
        password_hash=hash_password("role-target-password"),
        role="viewer",
    )
    db_session.add(target)
    db_session.commit()
    token = _socket_token(target)

    response = client.put(
        f"/api/v1/auth/users/{target.id}/role",
        headers=admin_headers,
        json={"role": "operator"},
    )

    assert response.status_code == 200
    db_session.expire_all()
    assert db_session.get(User, target.id).realtime_session_epoch == 1
    with pytest.raises(RealtimeAuthenticationError, match="revoked"):
        authenticate_socket(
            db_session,
            auth={"access_token": token},
            origin="http://127.0.0.1:8000",
        )


def test_workspace_membership_revoke_denies_future_realtime_subscriptions(
    client, admin_headers, db_session
):
    target = User(
        email="realtime-membership-target@example.com",
        password_hash=hash_password("membership-target-password"),
        role="viewer",
    )
    workspace = Workspace(name="realtime membership workspace", creator="test")
    db_session.add_all([target, workspace])
    db_session.flush()
    project = TaskSet(workspace_id=workspace.id, name="realtime membership project")
    db_session.add(project)
    db_session.flush()
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="realtime membership batch",
        batch_type="teleop",
    )
    db_session.add_all(
        [
            batch,
            WorkspaceMember(workspace_id=workspace.id, user_id=target.id),
        ]
    )
    db_session.commit()
    resolve_resource_subscription(
        db_session,
        actor_id=target.id,
        realtime_session_epoch=target.realtime_session_epoch,
        resource_type="batch",
        resource_id=str(batch.id),
    )

    response = client.delete(
        f"/api/v1/workspace/{workspace.id}/members/{target.id}",
        headers=admin_headers,
    )

    assert response.status_code == 200
    http_response = client.get(
        "/api/v1/workspace/task-set/list",
        headers={"Authorization": f"Bearer {_socket_token(target)}"},
        params={"workspace_id": workspace.id},
    )
    assert http_response.status_code == 403
    with pytest.raises(SubscriptionDenied, match="access denied"):
        resolve_resource_subscription(
            db_session,
            actor_id=target.id,
            realtime_session_epoch=target.realtime_session_epoch,
            resource_type="batch",
            resource_id=str(batch.id),
        )
