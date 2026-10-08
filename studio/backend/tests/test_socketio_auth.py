import pytest

from data.config import settings
from data.database import User
from data.realtime.auth import RealtimeAuthenticationError, authenticate_socket
from data.utils.helpers import create_access_token


def _access_token(user: User) -> str:
    return create_access_token(
        user.id,
        user.email,
        user.role,
        realtime_session_epoch=user.realtime_session_epoch,
    )


def test_socket_auth_requires_a_valid_access_token(db_session):
    with pytest.raises(RealtimeAuthenticationError, match="access token"):
        authenticate_socket(
            db_session,
            auth={},
            origin="http://127.0.0.1:8000",
        )


def test_socket_auth_rejects_unconfigured_origin(db_session, monkeypatch):
    user = db_session.query(User).order_by(User.id).first()
    monkeypatch.setattr(settings, "cors_origins", "http://trusted.example")

    with pytest.raises(RealtimeAuthenticationError, match="origin"):
        authenticate_socket(
            db_session,
            auth={"access_token": _access_token(user)},
            origin="http://untrusted.example",
        )


def test_socket_auth_uses_current_user_not_token_role(db_session):
    user = db_session.query(User).order_by(User.id).first()
    token = create_access_token(
        user.id,
        user.email,
        "viewer",
        realtime_session_epoch=user.realtime_session_epoch,
    )

    identity = authenticate_socket(
        db_session,
        auth={"access_token": token},
        origin="http://127.0.0.1:8000",
    )

    assert identity.user_id == user.id
    assert identity.role == user.role
    assert identity.expires_at > 0


def test_socket_auth_rejects_a_token_from_a_revoked_realtime_session(db_session):
    user = db_session.query(User).order_by(User.id).first()
    token = _access_token(user)
    user.realtime_session_epoch += 1
    db_session.commit()

    with pytest.raises(RealtimeAuthenticationError, match="revoked"):
        authenticate_socket(
            db_session,
            auth={"access_token": token},
            origin="http://127.0.0.1:8000",
        )
