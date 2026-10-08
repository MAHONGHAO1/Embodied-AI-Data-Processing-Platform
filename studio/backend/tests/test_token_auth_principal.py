"""API tokens authenticate like the owning user's JWT."""


def _issue(client, headers, name="duance-运维机"):
    created = client.post("/api/v1/tokens", json={"name": name}, headers=headers)
    assert created.status_code == 200
    return created.json()["data"]


def test_token_can_call_existing_apis_like_the_owning_user(client, annotator_headers):
    issued = _issue(client, annotator_headers)
    token_headers = {"Authorization": f"Bearer {issued['secret']}"}

    me = client.get("/api/v1/auth/me", headers=token_headers)
    assert me.status_code == 200
    assert me.json()["data"]["email"] == "annotator@quicdata.com"

    labels = client.get("/api/v1/task-labels", params={"workspace_id": 1}, headers=token_headers)
    assert labels.status_code in (200, 404)  # 404 only when the fixture has no workspace


def test_revoked_token_is_rejected_with_a_reason(client, annotator_headers):
    issued = _issue(client, annotator_headers, name="revoke-me")
    token_headers = {"Authorization": f"Bearer {issued['secret']}"}
    assert client.get("/api/v1/auth/me", headers=token_headers).status_code == 200

    client.delete(f"/api/v1/tokens/{issued['id']}", headers=annotator_headers)
    response = client.get("/api/v1/auth/me", headers=token_headers)

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "revoked"


def test_tampered_token_is_rejected(client, annotator_headers):
    issued = _issue(client, annotator_headers, name="tamper-me")
    forged = f"{issued['secret']}x"

    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {forged}"})

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_token"


def test_token_inherits_the_role_for_permission_checks(client, annotator_headers):
    issued = _issue(client, annotator_headers, name="role-bound")
    token_headers = {"Authorization": f"Bearer {issued['secret']}"}

    # Annotators cannot mint users; the token must be rejected exactly like the JWT.
    assert client.get("/api/v1/auth/users", headers=token_headers).status_code == 403
