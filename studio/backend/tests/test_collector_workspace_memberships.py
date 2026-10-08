"""Collector cross-workspace (factory) mobility and membership management tests."""

from collection_api_fixtures import make_workspace


def test_collector_workspace_flow(client, db_session, admin_headers):
    # 1. Prepare two independent workspaces (representing different factories)
    ws1 = make_workspace(db_session)
    ws2 = make_workspace(db_session)

    # 2. Create collector in workspace 1
    create_res = client.post(
        "/api/v1/collector-profiles",
        headers=admin_headers,
        json={"workspace_id": ws1.id, "name": "GlobalWorker", "profile_key": "8888"},
    )
    assert create_res.status_code == 200
    profile_id = create_res.json()["data"]["id"]
    profile_key = create_res.json()["data"]["profile_key"]
    assert profile_key == "8888"

    # 3. Verify workspace 1 can see this collector
    ws1_list = client.get(
        "/api/v1/collector-profiles",
        headers=admin_headers,
        params={"workspace_id": ws1.id},
    )
    assert ws1_list.status_code == 200
    assert profile_id in {row["id"] for row in ws1_list.json()["data"]["items"]}

    # 4. Verify workspace 2 currently cannot see this collector
    ws2_list = client.get(
        "/api/v1/collector-profiles",
        headers=admin_headers,
        params={"workspace_id": ws2.id},
    )
    assert ws2_list.status_code == 200
    assert profile_id not in {row["id"] for row in ws2_list.json()["data"]["items"]}

    # 5. Workspace 2 queries "available existing collectors", finding this collector
    available_res = client.get(
        "/api/v1/collector-profiles/available",
        headers=admin_headers,
        params={"workspace_id": ws2.id, "query": "GlobalWorker"},
    )
    assert available_res.status_code == 200
    available_items = available_res.json()["data"]["items"]
    assert any(item["id"] == profile_id for item in available_items)

    # 6. Workspace 2 calls memberships endpoint to add this collector
    grant_res = client.post(
        "/api/v1/collector-profiles/memberships",
        headers=admin_headers,
        json={"workspace_id": ws2.id, "personnel_profile_id": profile_id},
    )
    assert grant_res.status_code == 200
    assert grant_res.json()["data"]["id"] == profile_id
    assert grant_res.json()["data"]["profile_key"] == "8888"

    # 7. Workspace 2 collectors list now includes this collector, and available list no longer includes them
    ws2_list_after = client.get(
        "/api/v1/collector-profiles",
        headers=admin_headers,
        params={"workspace_id": ws2.id},
    )
    assert ws2_list_after.status_code == 200
    assert profile_id in {row["id"] for row in ws2_list_after.json()["data"]["items"]}

    available_res_after = client.get(
        "/api/v1/collector-profiles/available",
        headers=admin_headers,
        params={"workspace_id": ws2.id},
    )
    assert available_res_after.status_code == 200
    available_items = available_res_after.json()["data"]["items"]
    assert not any(item["id"] == profile_id for item in available_items)

    # 8. Workspace 2 removes this collector
    revoke_res = client.delete(
        f"/api/v1/collector-profiles/{profile_id}/memberships",
        headers=admin_headers,
        params={"workspace_id": ws2.id},
    )
    assert revoke_res.status_code == 200

    # 9. Verify workspace 2 no longer includes the collector, while workspace 1 still retains them
    ws2_list_final = client.get(
        "/api/v1/collector-profiles",
        headers=admin_headers,
        params={"workspace_id": ws2.id},
    )
    ws2_items = ws2_list_final.json()["data"]["items"]
    assert profile_id not in {row["id"] for row in ws2_items}

    ws1_list_final = client.get(
        "/api/v1/collector-profiles",
        headers=admin_headers,
        params={"workspace_id": ws1.id},
    )
    assert profile_id in {row["id"] for row in ws1_list_final.json()["data"]["items"]}
