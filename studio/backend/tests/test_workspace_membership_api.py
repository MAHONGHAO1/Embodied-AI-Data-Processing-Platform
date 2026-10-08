from __future__ import annotations

from uuid import uuid4

from data.database import SecurityAuditEvent, User, Workspace, WorkspaceMember
from data.utils.helpers import hash_password


def _workspace(db_session, *, creator: str = "test") -> Workspace:
    workspace = Workspace(name=f"membership {uuid4().hex}", creator=creator)
    db_session.add(workspace)
    db_session.commit()
    return workspace


def _user(db_session, *, role: str = "viewer") -> User:
    user = User(
        email=f"membership-{uuid4().hex}@example.com",
        password_hash=hash_password("membership-test-password"),
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    return user


def test_admin_grant_accepts_legacy_field_without_returning_or_auditing_it(
    client, admin_headers, db_session
):
    workspace = _workspace(db_session)
    target = _user(db_session, role="operator")

    response = client.post(
        f"/api/v1/workspace/{workspace.id}/members",
        headers=admin_headers,
        json={"user_id": target.id, "access_level": "owner"},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "workspace_id": workspace.id,
        "user_id": target.id,
        "email": target.email,
        "role": "operator",
    }
    db_session.expire_all()
    event = (
        db_session.query(SecurityAuditEvent)
        .filter(
            SecurityAuditEvent.action == "workspace.member.grant",
            SecurityAuditEvent.workspace_id == workspace.id,
            SecurityAuditEvent.resource_id == str(workspace.id),
        )
        .order_by(SecurityAuditEvent.id.desc())
        .first()
    )
    assert event is not None
    assert event.detail_json == {"workspace_id": workspace.id, "user_id": target.id}


def test_admin_grant_is_idempotent_and_member_list_has_no_level(client, admin_headers, db_session):
    workspace = _workspace(db_session)
    target = _user(db_session)
    url = f"/api/v1/workspace/{workspace.id}/members"

    first = client.post(url, headers=admin_headers, json={"user_id": target.id})
    second = client.post(url, headers=admin_headers, json={"user_id": target.id})
    listed = client.get(url, headers=admin_headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["data"] == first.json()["data"]
    assert listed.status_code == 200
    assert listed.json()["data"]["list"] == [first.json()["data"]]
    assert (
        db_session.query(WorkspaceMember)
        .filter(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.user_id == target.id,
        )
        .count()
        == 1
    )


def test_non_admin_workspace_creator_cannot_manage_members(client, operator_headers, db_session):
    # Creation is no longer permitted for annotators; model an existing creator.
    creator = db_session.query(User).filter(User.email == "annotator@quicdata.com").one()
    workspace_id = _workspace(db_session, creator=creator.email).id
    target = _user(db_session)

    listed = client.get(
        f"/api/v1/workspace/{workspace_id}/members",
        headers=operator_headers,
    )
    granted = client.post(
        f"/api/v1/workspace/{workspace_id}/members",
        headers=operator_headers,
        json={"user_id": target.id},
    )

    assert listed.status_code == 403
    assert granted.status_code == 403


def test_admin_business_scope_requires_membership_but_can_manage_all_spaces(
    client, admin_headers, db_session
):
    workspace = _workspace(db_session)
    admin = db_session.query(User).filter(User.email == "admin@quicdata.com").one()
    options_url = "/api/v1/workspace/options"
    members_url = f"/api/v1/workspace/{workspace.id}/members"
    # Annotation and review are not membership-scoped, unlike the collection business scope.
    work_lists = [
        f"/api/v1/annotation-work-items?workspace_id={workspace.id}",
        f"/api/v1/review-work-items?workspace_id={workspace.id}",
    ]
    missing_workbenches = [
        f"/api/v1/annotation-work-items/999999/workbench?workspace_id={workspace.id}",
        f"/api/v1/review-work-items/999999/workbench?workspace_id={workspace.id}",
    ]

    def ids(scope="member"):
        response = client.get(options_url, params={"scope": scope}, headers=admin_headers)
        assert response.status_code == 200
        return {row["id"] for row in response.json()["data"]["list"]}

    def assert_work_access():
        for path in work_lists:
            assert client.get(path, headers=admin_headers).status_code == 200
        for path in missing_workbenches:
            assert client.get(path, headers=admin_headers).status_code == 404

    assert workspace.id not in ids()
    assert workspace.id in ids("management")
    assert_work_access()
    assert (
        client.post(members_url, headers=admin_headers, json={"user_id": admin.id}).status_code
        == 200
    )
    assert workspace.id in ids()
    assert_work_access()
    assert client.delete(f"{members_url}/{admin.id}", headers=admin_headers).status_code == 200
    assert workspace.id not in ids()
    assert_work_access()
    assert workspace.id in ids("management")


def test_non_admin_cannot_request_management_scope(client, db_session):
    from data.utils.helpers import create_access_token

    actor = _user(db_session)
    token = create_access_token(actor.id, actor.email, actor.role)
    response = client.get(
        "/api/v1/workspace/options?scope=management", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403
