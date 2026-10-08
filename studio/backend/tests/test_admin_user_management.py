def test_admin_created_user_must_change_the_temporary_password(client, admin_headers):
    created = client.post(
        "/api/v1/auth/users",
        headers=admin_headers,
        json={
            "email": "temporary-user@example.com",
            "password": "temporary-password",
            "role": "annotator",
        },
    )

    assert created.status_code == 200
    assert created.json()["data"]["must_change_password"] is True

    login = client.post(
        "/api/v1/auth/login",
        json={"email": "temporary-user@example.com", "password": "temporary-password"},
    )
    assert login.status_code == 200
    payload = login.json()["data"]
    assert payload["userInfo"]["must_change_password"] is True

    blocked = client.get(
        "/api/v1/workspace/options",
        headers={"Authorization": f"Bearer {payload['access_token']}"},
    )
    assert blocked.status_code == 403

    changed = client.post(
        "/api/v1/auth/change-password",
        headers={"Authorization": f"Bearer {payload['access_token']}"},
        json={"old_password": "temporary-password", "new_password": "private-password"},
    )
    assert changed.status_code == 200

    renewed = client.post(
        "/api/v1/auth/login",
        json={"email": "temporary-user@example.com", "password": "private-password"},
    )
    assert renewed.status_code == 200
    assert renewed.json()["data"]["userInfo"]["must_change_password"] is False


def test_operator_cannot_list_or_create_platform_users(client, operator_headers):
    listed = client.get("/api/v1/auth/users", headers=operator_headers)
    created = client.post(
        "/api/v1/auth/users",
        headers=operator_headers,
        json={
            "email": "operator-created@example.com",
            "password": "temporary-password",
            "role": "annotator",
        },
    )

    assert listed.status_code == 403
    assert created.status_code == 403
