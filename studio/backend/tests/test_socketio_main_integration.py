import socketio
from fastapi.testclient import TestClient

from data.database import Batch, TaskSet, User, Workspace
from data.main import app, socket_server
from data.utils.helpers import create_access_token, hash_password


def test_main_exposes_socketio_asgi_wrapper_and_preserves_health_endpoint(client):
    assert isinstance(app, socketio.ASGIApp)
    assert socket_server.async_mode == "asgi"

    response = client.get("/health")

    assert response.status_code == 200


def test_socketio_handshake_and_subscription_use_same_origin_websocket(db_session):
    user = db_session.query(User).filter(User.role == "admin").first()
    workspace = Workspace(name="socket handshake workspace", creator="test")
    db_session.add(workspace)
    db_session.flush()
    project = TaskSet(workspace_id=workspace.id, name="socket handshake project")
    db_session.add(project)
    db_session.flush()
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="socket handshake batch",
        batch_type="teleop",
    )
    db_session.add(batch)
    db_session.commit()
    token = create_access_token(
        user.id,
        user.email,
        user.role,
        realtime_session_epoch=user.realtime_session_epoch,
    )

    with TestClient(app) as runtime_client:
        with runtime_client.websocket_connect(
            "/socket.io/?EIO=4&transport=websocket",
            headers={
                "origin": "http://127.0.0.1:8000",
                "connection": "upgrade",
                "upgrade": "websocket",
            },
        ) as websocket:
            assert websocket.receive_text().startswith("0")
            websocket.send_text(f'40{{"access_token":"{token}"}}')
            assert websocket.receive_text().startswith("40")
            websocket.send_text(
                f'421["subscribe",{{"resource_type":"batch","resource_id":"{batch.id}"}}]'
            )
            assert (
                websocket.receive_text()
                == f'431[{{"resource_type":"batch","resource_id":"{batch.id}"}}]'
            )


def test_logout_immediately_disconnects_the_authenticated_socket(db_session):
    user = User(
        email="socket-logout@example.com",
        password_hash=hash_password("socket-logout-password"),
        role="viewer",
    )
    db_session.add(user)
    db_session.commit()

    with TestClient(app) as runtime_client:
        login = runtime_client.post(
            "/api/v1/auth/login",
            json={"email": user.email, "password": "socket-logout-password"},
        )
        token = login.json()["data"]["access_token"]
        with runtime_client.websocket_connect(
            "/socket.io/?EIO=4&transport=websocket",
            headers={
                "origin": "http://127.0.0.1:8000",
                "connection": "upgrade",
                "upgrade": "websocket",
            },
        ) as websocket:
            assert websocket.receive_text().startswith("0")
            websocket.send_text(f'40{{"access_token":"{token}"}}')
            assert websocket.receive_text().startswith("40")

            logout = runtime_client.post(
                "/api/v1/auth/logout",
                headers={"Authorization": f"Bearer {token}"},
            )

            assert logout.status_code == 200
            assert websocket.receive_text() == "41"
