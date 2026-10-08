# ruff: noqa: E701, E702
from tests.collection_api_fixtures import make_label, make_project, make_workspace


def _get(client, headers, **params):
    return client.get("/api/v1/collection-dashboard/data", headers=headers, params=params)


def test_returns_data_board_payload(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    make_project(db_session, workspace)
    response = _get(client, admin_headers, workspace_id=workspace.id)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["workspace_id"] == workspace.id
    assert data["filters"]["granularity"] == "day"
    assert len(data["duration"]["trend"]) == 30


def test_rejects_invalid_scope_and_format(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    other = make_workspace(db_session)
    foreign = make_project(db_session, other)
    purpose = make_label(db_session, category="purpose")
    assert (
        _get(client, admin_headers, workspace_id=workspace.id, project_ids=[foreign.id]).status_code
        == 422
    )
    assert (
        _get(
            client, admin_headers, workspace_id=workspace.id, scene_label_ids=[purpose.id]
        ).status_code
        == 422
    )
    assert _get(client, admin_headers, workspace_id=workspace.id, format="xml").status_code == 422


def test_csv_download_and_unknown_workspace(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    response = _get(
        client,
        admin_headers,
        workspace_id=workspace.id,
        start_date="2026-09-28",
        end_date="2026-09-29",
        format="csv",
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.content.decode("utf-8").startswith("\ufeff时间,")
    assert _get(client, admin_headers, workspace_id=999999999).status_code == 404
