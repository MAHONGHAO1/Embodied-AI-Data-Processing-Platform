"""Workstream C — bootstrap identity and project RBAC."""

from fastapi.testclient import TestClient
from quictrain_core import new_id
from tests.db_helpers import rebind_database

import quictrain_api.db as db
from quictrain_api.auth import hash_bootstrap_token, resolve_actor
from quictrain_api.db import ProjectMembershipRecord, UserRecord, init_database
from quictrain_api.errors import ServiceError
from quictrain_api.service import seed_catalog


def test_cross_project_denied_and_audit_actor(tmp_path, monkeypatch):
    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/auth.db")
    monkeypatch.setenv("QUICTRAIN_AUTH_MODE", "bootstrap")
    monkeypatch.setenv("QUICTRAIN_BOOTSTRAP_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    rebind_database()
    init_database()
    with db.SessionLocal() as session:
        seed_catalog(session)
        user = session.get(UserRecord, "usr_demo")
        assert user is not None
        user.bootstrap_token_hash = hash_bootstrap_token("test-admin-token")
        session.add(
            UserRecord(
                id="usr_viewer",
                display_name="Viewer",
                email="viewer@quicrobot.local",
                role="user",
                bootstrap_token_hash=hash_bootstrap_token("viewer-token"),
            )
        )
        session.add(
            ProjectMembershipRecord(
                id=new_id("pjm"),
                project_id="prj_robot_arm",
                user_id="usr_viewer",
                role="viewer",
            )
        )
        session.commit()

        admin = resolve_actor(
            session,
            authorization="Bearer test-admin-token",
            project_id="prj_robot_arm",
        )
        assert admin.user_id == "usr_demo"
        admin.require_project_role("admin")

        try:
            resolve_actor(
                session,
                authorization="Bearer viewer-token",
                project_id="prj_other",
            ).require_project_role("viewer")
            raise AssertionError("expected cross-project denial")
        except ServiceError as exc:
            assert exc.code == "PROJECT_ACCESS_DENIED"

    from quictrain_api.main import app

    with TestClient(app) as client:
        unauth = client.post(
            "/api/v1/jobs",
            json={
                "project_id": "prj_robot_arm",
                "dataset_version_id": "dsv_kitchen_v17",
                "model_version_id": "mv_act_20260716",
                "recipe_id": "fine_tune",
                "config_overrides": {},
                "resource_selection": {"mode": "AUTO", "profile": "act-h20-standard"},
                "client_request_id": "auth-denied",
            },
            headers={"Idempotency-Key": "auth-denied"},
        )
        assert unauth.status_code == 401

        forbidden = client.post(
            "/api/v1/jobs",
            json={
                "project_id": "prj_robot_arm",
                "dataset_version_id": "dsv_kitchen_v17",
                "model_version_id": "mv_act_20260716",
                "recipe_id": "fine_tune",
                "config_overrides": {},
                "resource_selection": {"mode": "AUTO", "profile": "act-h20-standard"},
                "client_request_id": "viewer-create",
            },
            headers={
                "Idempotency-Key": "viewer-create",
                "Authorization": "Bearer viewer-token",
            },
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["code"] == "INSUFFICIENT_ROLE"

        members = client.get(
            "/api/v1/projects/prj_robot_arm/members",
            headers={"Authorization": "Bearer viewer-token"},
        )
        assert members.status_code == 200
        assert any(item["user_id"] == "usr_viewer" for item in members.json()["items"])

        denied_member = client.put(
            "/api/v1/projects/prj_robot_arm/members",
            headers={"Authorization": "Bearer viewer-token"},
            json={
                "email": "ops@quicrobot.local",
                "display_name": "Ops",
                "role": "operator",
            },
        )
        assert denied_member.status_code == 403

        created = client.put(
            "/api/v1/projects/prj_robot_arm/members",
            headers={"Authorization": "Bearer test-admin-token"},
            json={
                "email": "ops@quicrobot.local",
                "display_name": "Ops",
                "role": "operator",
            },
        )
        assert created.status_code == 200
        assert created.json()["role"] == "operator"
        assert created.json()["created"] is True

        audit = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": "Bearer viewer-token"},
        )
        assert audit.status_code == 403

        retry_denied = client.post(
            "/api/v1/jobs/job_missing/retry",
            headers={"Authorization": "Bearer viewer-token"},
        )
        assert retry_denied.status_code in {403, 404}
