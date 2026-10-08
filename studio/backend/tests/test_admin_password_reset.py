"""Administrator password reset endpoint."""

from __future__ import annotations

from data.database import User
from data.utils.helpers import hash_password


def _create_user(db_session, *, email: str, role: str = "viewer") -> User:
    user = User(email=email, password_hash=hash_password("activation-password"), role=role)
    db_session.add(user)
    db_session.commit()
    return user


def test_admin_resets_password_and_old_login_stops_working(client, admin_headers, db_session):
    target = _create_user(db_session, email="reset-target@example.com")

    reset = client.post(
        f"/api/v1/auth/users/{target.id}/reset-password",
        headers=admin_headers,
    )
    assert reset.status_code == 200
    data = reset.json()["data"]
    assert len(data["temporary_password"]) >= 16
    assert data["must_change_password"] is True
    assert data["email"] == target.email

    old_login = client.post(
        "/api/v1/auth/login",
        json={"email": target.email, "password": "activation-password"},
    )
    assert old_login.status_code == 401

    new_login = client.post(
        "/api/v1/auth/login",
        json={"email": target.email, "password": data["temporary_password"]},
    )
    assert new_login.status_code == 200
    assert new_login.json()["data"]["userInfo"]["must_change_password"] is True


def test_reset_password_requires_admin(client, operator_headers, db_session):
    target = _create_user(db_session, email="reset-operator@example.com")

    response = client.post(
        f"/api/v1/auth/users/{target.id}/reset-password",
        headers=operator_headers,
    )
    assert response.status_code == 403


def test_reset_password_rejects_inactive_user(client, admin_headers, db_session):
    target = _create_user(db_session, email="reset-inactive@example.com")
    target.is_active = False
    db_session.commit()

    response = client.post(
        f"/api/v1/auth/users/{target.id}/reset-password",
        headers=admin_headers,
    )
    assert response.status_code == 409


def test_reset_password_missing_user_returns_404(client, admin_headers):
    response = client.post("/api/v1/auth/users/999999/reset-password", headers=admin_headers)
    assert response.status_code == 404
