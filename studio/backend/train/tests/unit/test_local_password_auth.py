"""Local email/password sessions and multi-user membership provisioning."""

from fastapi.testclient import TestClient
from tests.db_helpers import rebind_database

import quictrain_api.db as db
from quictrain_api.auth import hash_password, verify_password
from quictrain_api.db import init_database
from quictrain_api.service import seed_catalog


def test_password_hash_roundtrip():
    encoded = hash_password("correct-horse-battery")
    assert verify_password("correct-horse-battery", encoded)
    assert not verify_password("wrong-password", encoded)


def test_local_login_session_and_member_password(tmp_path, monkeypatch):
    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/local-auth.db")
    monkeypatch.setenv("QUICTRAIN_AUTH_MODE", "local")
    monkeypatch.setenv("QUICTRAIN_ADMIN_EMAIL", "admin@quicrobot.com")
    monkeypatch.setenv("QUICTRAIN_ADMIN_PASSWORD", "AdminPass123!")
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    rebind_database()
    init_database()
    with db.SessionLocal() as session:
        seed_catalog(session)

    from quictrain_api.main import app

    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["identity"]["auth_mode"] == "local"
        assert health.json()["identity"]["login_required"] is True

        denied = client.get("/api/v1/models")
        assert denied.status_code == 401

        bad = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@quicrobot.com", "password": "nope"},
        )
        assert bad.status_code == 401

        login = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@quicrobot.com", "password": "AdminPass123!"},
        )
        assert login.status_code == 200
        token = login.json()["access_token"]
        assert login.json()["user"]["email"] == "admin@quicrobot.com"

        me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        assert me.json()["user"]["display_name"] == "Admin"

        models = client.get("/api/v1/models", headers={"Authorization": f"Bearer {token}"})
        assert models.status_code == 200

        created = client.put(
            "/api/v1/projects/prj_robot_arm/members",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "email": "ops@quicrobot.com",
                "display_name": "Ops",
                "role": "operator",
                "password": "OpsPass123!",
            },
        )
        assert created.status_code == 200
        assert created.json()["created"] is True

        ops_login = client.post(
            "/api/v1/auth/login",
            json={"email": "ops@quicrobot.com", "password": "OpsPass123!"},
        )
        assert ops_login.status_code == 200
        ops_token = ops_login.json()["access_token"]

        ops_jobs = client.get(
            "/api/v1/jobs",
            headers={"Authorization": f"Bearer {ops_token}"},
        )
        assert ops_jobs.status_code == 200

        logout = client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {ops_token}"},
        )
        assert logout.status_code == 200
        revoked = client.get(
            "/api/v1/jobs",
            headers={"Authorization": f"Bearer {ops_token}"},
        )
        assert revoked.status_code == 401
