"""Self-service token endpoints: any signed-in user manages only their own tokens."""


def test_any_user_can_issue_and_list_own_tokens(client, annotator_headers):
    created = client.post(
        "/api/v1/tokens",
        json={"name": "api-self-service"},
        headers=annotator_headers,
    )
    assert created.status_code == 200
    payload = created.json()["data"]
    assert payload["secret"].startswith("qs_")
    assert payload["name"] == "api-self-service"

    listed = client.get("/api/v1/tokens", headers=annotator_headers).json()["data"]["items"]
    names = [item["name"] for item in listed]
    assert "api-self-service" in names
    assert all("secret" not in item for item in listed)


def test_tokens_never_cross_users(client, annotator_headers, admin_headers):
    created = client.post(
        "/api/v1/tokens",
        json={"name": "annotator-only"},
        headers=annotator_headers,
    ).json()["data"]

    admin_items = client.get("/api/v1/tokens", headers=admin_headers).json()["data"]["items"]
    assert admin_items == []

    stolen = client.delete(f"/api/v1/tokens/{created['id']}", headers=admin_headers)
    assert stolen.status_code == 404


def test_rotate_and_revoke_own_token(client, annotator_headers):
    created = client.post(
        "/api/v1/tokens",
        json={"name": "rotate-me"},
        headers=annotator_headers,
    ).json()["data"]

    rotated = client.post(f"/api/v1/tokens/{created['id']}/rotate", headers=annotator_headers)
    assert rotated.status_code == 200
    assert rotated.json()["data"]["secret"] != created["secret"]

    revoked = client.delete(f"/api/v1/tokens/{created['id']}", headers=annotator_headers)
    assert revoked.status_code == 200
    listed = client.get("/api/v1/tokens", headers=annotator_headers).json()["data"]["items"]
    assert listed and listed[0]["revoked_at"]


def test_issue_rejects_client_supplied_user_id(client, annotator_headers):
    response = client.post(
        "/api/v1/tokens",
        json={"name": "forged", "user_id": 1},
        headers=annotator_headers,
    )
    assert response.status_code == 422


def test_token_rate_limit_returns_429(client, annotator_headers, monkeypatch):
    from data.config import settings

    issued = client.post(
        "/api/v1/tokens",
        json={"name": "rate-limited"},
        headers=annotator_headers,
    ).json()["data"]
    token_headers = {"Authorization": f"Bearer {issued['secret']}"}
    monkeypatch.setattr(settings, "api_token_rate_limit_per_minute", 3, raising=False)

    codes = [client.get("/api/v1/auth/me", headers=token_headers).status_code for _ in range(6)]

    assert 429 in codes
    assert 200 in codes


def test_token_usage_is_audited_once_per_window(client, annotator_headers, monkeypatch):
    import data.security.tokens as token_security

    events: list[tuple] = []
    monkeypatch.setattr(
        token_security,
        "emit_audit_event",
        lambda action, **kwargs: events.append((action, kwargs)),
        raising=False,
    )
    issued = client.post(
        "/api/v1/tokens",
        json={"name": "audited"},
        headers=annotator_headers,
    ).json()["data"]
    token_headers = {"Authorization": f"Bearer {issued['secret']}"}

    for _ in range(3):
        client.get("/api/v1/auth/me", headers=token_headers)

    assert [action for action, _ in events] == ["api_token.use"]
    assert events[0][1]["resource"] == f"api_token:{issued['id']}"
