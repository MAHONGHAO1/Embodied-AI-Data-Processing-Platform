from __future__ import annotations

from data.config import settings
from data.database import User
from data.infra.redis_client import RedisUnavailableError
from data.utils.helpers import hash_password


def test_login_sets_http_only_session_cookie_without_exposing_refresh_token(client):
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@quicdata.com", "password": "admin123"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["access_token"]
    assert "refresh_token" not in data
    cookie = response.headers["set-cookie"]
    assert "quicdata_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/api/v1/auth" in cookie
    assert "Domain=" not in cookie


def test_production_login_cookie_is_secure(client, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "session_cookie_secure", None)

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@quicdata.com", "password": "admin123"},
    )

    assert "Secure" in response.headers["set-cookie"]


def test_development_login_cookie_is_not_secure(client, monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "session_cookie_secure", None)

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@quicdata.com", "password": "admin123"},
    )

    assert "Secure" not in response.headers["set-cookie"]


def test_session_cookie_secure_override(client, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "session_cookie_secure", False)

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@quicdata.com", "password": "admin123"},
    )

    assert "Secure" not in response.headers["set-cookie"]


def test_cookie_refresh_restores_a_new_page_session(client):
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@quicdata.com", "password": "admin123"},
    )
    old_access = login.json()["data"]["access_token"]

    refreshed = client.post("/api/v1/auth/refresh")

    assert refreshed.status_code == 200
    data = refreshed.json()["data"]
    assert data["access_token"] != old_access
    assert data["userInfo"]["email"] == "admin@quicdata.com"
    assert "refresh_token" not in data


def test_cookie_refresh_rejects_cross_site_requests(client):
    client.post(
        "/api/v1/auth/login",
        json={"email": "admin@quicdata.com", "password": "admin123"},
    )

    response = client.post(
        "/api/v1/auth/refresh",
        headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 403


def test_cookie_refresh_rejects_untrusted_origin_without_fetch_metadata(client):
    client.post(
        "/api/v1/auth/login",
        json={"email": "admin@quicdata.com", "password": "admin123"},
    )

    response = client.post(
        "/api/v1/auth/refresh",
        headers={"Origin": "https://attacker.example"},
    )

    assert response.status_code == 403


def test_cookie_refresh_accepts_same_origin(client):
    client.post(
        "/api/v1/auth/login",
        json={"email": "admin@quicdata.com", "password": "admin123"},
    )

    response = client.post(
        "/api/v1/auth/refresh",
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 200
    assert response.json()["code"] == 200


def test_auth_failures_use_real_http_status_codes(client, monkeypatch):
    invalid_login = client.post(
        "/api/v1/auth/login",
        json={"email": "missing@example.com", "password": "invalid-password"},
    )
    invalid_refresh = client.post("/api/v1/auth/refresh")

    monkeypatch.setattr("data.security.rate_limit.is_rate_limited", lambda *_args, **_kwargs: True)
    limited = client.post(
        "/api/v1/auth/login",
        json={"email": "limited@example.com", "password": "invalid-password"},
    )

    assert invalid_login.status_code == 401
    assert invalid_refresh.status_code == 401
    assert limited.status_code == 429


def test_logout_without_bearer_revokes_session_and_clears_cookie(client):
    client.post(
        "/api/v1/auth/login",
        json={"email": "admin@quicdata.com", "password": "admin123"},
    )

    logout = client.post("/api/v1/auth/logout")
    refreshed = client.post("/api/v1/auth/refresh")

    assert logout.status_code == 200
    assert "quicdata_session=" in logout.headers["set-cookie"]
    assert "Max-Age=0" in logout.headers["set-cookie"]
    assert refreshed.json()["code"] == 401


def test_stale_bearer_logout_cannot_revoke_a_new_session(client, db_session):
    first_login = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@quicdata.com", "password": "admin123"},
    )
    stale_access = first_login.json()["data"]["access_token"]
    first_logout = client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {stale_access}"},
    )
    assert first_logout.status_code == 200

    second_login = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@quicdata.com", "password": "admin123"},
    )
    current_access = second_login.json()["data"]["access_token"]
    user = db_session.query(User).filter(User.email == "admin@quicdata.com").one()
    epoch_after_current_login = user.realtime_session_epoch

    client.cookies.clear()
    repeated = client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {stale_access}"},
    )

    assert repeated.status_code == 200
    db_session.expire_all()
    assert db_session.get(User, user.id).realtime_session_epoch == epoch_after_current_login
    current = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {current_access}"},
    )
    assert current.status_code == 200


def test_password_change_remains_authoritative_when_redis_cleanup_fails(
    client, db_session, monkeypatch
):
    user = User(
        email="password-redis-failure@example.com",
        password_hash=hash_password("old-password-value"),
        role="viewer",
    )
    db_session.add(user)
    db_session.commit()
    login = client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "old-password-value"},
    )
    access = login.json()["data"]["access_token"]
    session_cookie = client.cookies.get("quicdata_session")
    previous_epoch = user.browser_session_epoch

    def unavailable(_user_id: int) -> int:
        raise RedisUnavailableError()

    monkeypatch.setattr("data.routers.auth.revoke_all_for_user", unavailable)
    changed = client.post(
        "/api/v1/auth/change-password",
        headers={"Authorization": f"Bearer {access}"},
        json={"old_password": "old-password-value", "new_password": "new-password-value"},
    )

    assert changed.status_code == 200
    assert "Max-Age=0" in changed.headers["set-cookie"]
    db_session.expire_all()
    assert db_session.get(User, user.id).browser_session_epoch == previous_epoch + 1
    client.cookies.set("quicdata_session", session_cookie, path="/api/v1/auth")
    refreshed = client.post("/api/v1/auth/refresh")
    assert refreshed.status_code == 401


def test_cookie_only_logout_fails_when_redis_cannot_confirm_revocation(client, monkeypatch):
    client.cookies.set(
        "quicdata_session",
        "unavailable-browser-session",
        path="/api/v1/auth",
    )

    def unavailable(_session_id: str):
        raise RedisUnavailableError()

    monkeypatch.setattr("data.routers.auth.get_browser_session", unavailable)
    logout = client.post("/api/v1/auth/logout")

    assert logout.status_code == 503
    assert logout.json()["code"] == 503
    assert "quicdata_session=" in logout.headers["set-cookie"]
    assert "Max-Age=0" in logout.headers["set-cookie"]
