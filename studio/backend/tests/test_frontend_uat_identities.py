"""Acceptance identities follow the three fixed roles, with legacy cleanup intact."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from data.config import DEFAULT_RBAC, settings
from data.database import User, WorkspaceMember
from data.utils.helpers import create_access_token, hash_password
from scripts import seed_frontend_uat_users as command


@pytest.fixture
def uat_seed_args(monkeypatch, tmp_path):
    for role in ("raw", "process", "export"):
        monkeypatch.setattr(settings, f"oss_bucket_{role}", f"quicstudio-uat-{role}")
    namespace = f"fe-uat-rbac-{uuid4().hex[:10]}"
    password_file = tmp_path / "password"
    password_file.write_text("isolated-acceptance-password")
    return namespace, password_file


def _seed(namespace, password_file):
    command.main(["--namespace", namespace, "--password-file", str(password_file)])


def _identities(db, namespace):
    return db.query(User).filter(User.email.like(f"{namespace}.%@uat.quicrobot.xyz")).all()


def test_seed_creates_only_configured_roles_and_keeps_existing_identities(
    db_session, uat_seed_args, capsys
):
    namespace, password_file = uat_seed_args
    _seed(namespace, password_file)
    first = json.loads(capsys.readouterr().out)
    users = _identities(db_session, namespace)
    assert {user.role for user in users} == {"admin", "annotator", "auditor"}
    assert {user["role"] for user in first["users"]} == set(DEFAULT_RBAC["roles"])
    password_hashes = {user.id: user.password_hash for user in users}

    legacy = User(
        email=f"{namespace}.operator@uat.quicrobot.xyz",
        role="operator",
        password_hash=hash_password("legacy-acceptance-password"),
    )
    db_session.add(legacy)
    db_session.commit()
    password_file.write_text("different-acceptance-password")
    _seed(namespace, password_file)
    second = json.loads(capsys.readouterr().out)
    db_session.expire_all()

    assert all(not user["created"] for user in second["users"])
    assert {user["id"] for user in second["users"]} == set(password_hashes)
    assert all(
        db_session.get(User, user_id).password_hash == value
        for user_id, value in password_hashes.items()
    )
    assert legacy.role == "operator" and legacy.is_active
    assert "password" not in json.dumps(first) + json.dumps(second)


def test_seed_rejects_missing_runtime_roles_before_creating_any_user(
    db_session, uat_seed_args, monkeypatch
):
    namespace, password_file = uat_seed_args
    monkeypatch.setattr(
        command, "get_runtime_config", lambda: {"rbac": {"roles": {"admin": {}, "annotator": {}}}}
    )
    with pytest.raises(SystemExit, match="not configured: auditor"):
        _seed(namespace, password_file)
    assert _identities(db_session, namespace) == []


@pytest.mark.parametrize("deactivate", [False, True])
def test_identity_command_rejects_non_uat_buckets(
    db_session, uat_seed_args, monkeypatch, deactivate
):
    namespace, password_file = uat_seed_args
    monkeypatch.setattr(settings, "oss_bucket_raw", "quicstudio-production-raw")
    args = ["--namespace", namespace, "--password-file", str(password_file)]
    if deactivate:
        args.extend(["--deactivate", "--actor-user-id", "1"])
    with pytest.raises(SystemExit, match="outside the dedicated Studio UAT"):
        command.main(args)
    assert _identities(db_session, namespace) == []


def test_cleanup_disables_all_five_exact_emails_and_revokes_sessions(
    client, db_session, uat_seed_args, capsys
):
    namespace, password_file = uat_seed_args
    _seed(namespace, password_file)
    capsys.readouterr()
    for role in ("operator", "viewer"):
        db_session.add(
            User(
                email=f"{namespace}.{role}@uat.quicrobot.xyz",
                role=role,
                password_hash=hash_password("legacy-acceptance-password"),
            )
        )
    other = User(
        email=f"{namespace}.unrelated@uat.quicrobot.xyz",
        role="annotator",
        password_hash=hash_password("unrelated-acceptance-password"),
    )
    db_session.add(other)
    db_session.commit()
    users = [user for user in _identities(db_session, namespace) if user.id != other.id]
    headers = {
        user.id: {"Authorization": f"Bearer {create_access_token(user.id, user.email, user.role)}"}
        for user in users
    }
    assert all(
        client.get("/api/v1/auth/me", headers=value).status_code == 200
        for value in headers.values()
    )
    admin = db_session.query(User).filter_by(email="admin@quicdata.com").one()
    args = ["--namespace", namespace, "--deactivate", "--actor-user-id", str(admin.id)]

    command.main(args)
    output = json.loads(capsys.readouterr().out)
    db_session.expire_all()
    assert {user["role"] for user in output["users"]} == set(command.CLEANUP_ROLES)
    assert all(not user.is_active for user in users)
    assert all(
        user.browser_session_epoch == 1 and user.realtime_session_epoch == 1 for user in users
    )
    assert admin.is_active and other.is_active
    assert all(
        client.get("/api/v1/auth/me", headers=value).status_code == 401
        for value in headers.values()
    )

    command.main(args)
    capsys.readouterr()
    db_session.expire_all()
    assert all(user.browser_session_epoch == 1 for user in users)


@pytest.mark.parametrize("actor_role", ["admin", "annotator"])
def test_cleanup_cannot_use_an_acceptance_identity_as_actor(
    db_session, uat_seed_args, capsys, actor_role
):
    namespace, password_file = uat_seed_args
    _seed(namespace, password_file)
    capsys.readouterr()
    users = _identities(db_session, namespace)
    actor = next(user for user in users if user.role == actor_role)
    with pytest.raises(SystemExit, match="active admin outside the acceptance namespace"):
        command.main(["--namespace", namespace, "--deactivate", "--actor-user-id", str(actor.id)])
    db_session.expire_all()
    assert all(user.is_active for user in users)


def test_registration_example_only_names_existing_roles():
    path = Path(__file__).resolve().parents[2] / "deploy" / "registration.example.json"
    example = json.loads(path.read_text())["registration"]
    roles = set(DEFAULT_RBAC["roles"])
    assert roles == {"admin", "annotator", "auditor"}
    assert set(example["creatable_roles"]["admin"]) == roles
    assert example["default_role"] in roles
    assert example["self_register_role"] in roles


@pytest.mark.parametrize("role", ["operator", "viewer", "unknown-acceptance-role"])
def test_unconfigured_real_identity_has_no_data_permissions_even_with_membership(
    client, db_session, role
):
    from tests.collection_api_fixtures import make_workspace

    workspace = make_workspace(db_session)
    user = User(
        email=f"unconfigured-{uuid4().hex}@example.test",
        role=role,
        password_hash=hash_password("unconfigured-acceptance-password"),
    )
    db_session.add(user)
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id))
    db_session.commit()
    login = client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "unconfigured-acceptance-password"},
    )
    assert login.status_code == 200
    payload = login.json()["data"]
    assert payload["userInfo"]["role"] == role
    assert payload["userInfo"]["permissions"] == []
    headers = {"Authorization": f"Bearer {payload['access_token']}"}
    for path in ("/workspace/options", "/data-assets", "/catalog-datasets"):
        response = client.get(f"/api/v1{path}", headers=headers)
        assert response.status_code == 403, (role, path, response.text)
    for path in ("/annotation-work-items", "/review-work-items"):
        response = client.get(
            f"/api/v1{path}", params={"workspace_id": workspace.id}, headers=headers
        )
        assert response.status_code == 403, (role, path, response.text)
