"""Real API pagination applies role/scope filters before SQL counts and limits."""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import event
from tests.collection_api_fixtures import make_workspace
from tests.test_annotation_work_items_api import (
    _create_annotated_batch,
    _headers_for,
    _make_reviewer,
)

from data.database import User
from data.models.annotation_work import ReviewWorkItem
from data.models.catalog_dataset import CatalogDataset, CatalogDatasetVersion
from data.models.data_asset import DataAsset
from data.models.data_batch import DataBatch
from data.services.annotation_work_items import assign_work_items_for_batch


def _get(client, path, headers, **params):
    response = client.get(f"/api/v1/{path}", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_work_pages_count_only_authorized_workspace_and_assignee(client, db_session, admin_headers):
    ws, batch, packages, actors, reviewer = _create_annotated_batch(
        db_session, durations=("1.00",) * 5
    )
    items = assign_work_items_for_batch(db_session, batch_id=batch.id)
    other_ws, other_batch, _, _, _ = _create_annotated_batch(db_session, durations=("1.00",) * 2)
    assign_work_items_for_batch(db_session, batch_id=other_batch.id)
    replacement = _make_reviewer(db_session, ws)
    review = db_session.query(ReviewWorkItem).filter_by(annotation_work_item_id=items[0].id).one()
    review.assignee_user_id = replacement.id
    db_session.commit()
    mine = [item.id for item in items if item.assignee_user_id == actors[0].id]
    headers, workspace_id = _headers_for(actors[0]), ws.id

    captured = []

    def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            captured.append(statement)

    event.listen(db_session.bind, "before_cursor_execute", observe)
    try:
        page = _get(
            client,
            "annotation-work-items",
            headers,
            workspace_id=workspace_id,
            limit=1,
            offset=1,
        )
    finally:
        event.remove(db_session.bind, "before_cursor_execute", observe)
    assert [row["id"] for row in page["items"]] == mine[1:2]
    assert (page["total"], page["limit"], page["offset"]) == (len(mine), 1, 1)
    assert page["items"][0]["data_batch_name"] == batch.name
    assert page["items"][0]["assignee_email"] == actors[0].email
    assert page["items"][0]["data_package_uid"] in {package.package_uid for package in packages}
    row_queries = [
        sql for sql in captured if "FROM annotation_work_items" in sql and "count(" not in sql
    ]
    assert len(row_queries) == 1 and "LIMIT" in row_queries[0] and "OFFSET" in row_queries[0]
    # Context fields come from the bounded join, rather than queries per row.
    assert not any("FROM data_packages \nWHERE" in sql for sql in captured)
    assert not any("FROM data_batches \nWHERE" in sql for sql in captured)

    full = _get(client, "annotation-work-items", admin_headers, workspace_id=ws.id)
    assert full["total"] == len(full["items"]) == 5
    assert full["limit"] is None and full["offset"] == 0
    beyond = _get(
        client,
        "annotation-work-items",
        _headers_for(actors[0]),
        workspace_id=ws.id,
        limit=2,
        offset=99,
    )
    assert beyond["items"] == [] and beyond["total"] == len(mine)
    # Work lists are not membership-scoped; assignment still limits what a worker sees.
    other = _get(
        client, "annotation-work-items", _headers_for(actors[0]), workspace_id=other_ws.id, limit=1
    )
    assert other["items"] == [] and other["total"] == 0

    reviews = _get(
        client, "review-work-items", _headers_for(reviewer), workspace_id=ws.id, limit=2, offset=1
    )
    assert reviews["total"] == 4 and len(reviews["items"]) == 2
    assert all(row["assignee_email"] == reviewer.email for row in reviews["items"])
    assert all(row["data_batch_name"] == batch.name for row in reviews["items"])
    admin_reviews = _get(client, "review-work-items", admin_headers, workspace_id=ws.id, limit=1)
    assert admin_reviews["total"] == 5 and len(admin_reviews["items"]) == 1
    beyond = _get(
        client, "review-work-items", _headers_for(reviewer), workspace_id=ws.id, limit=2, offset=99
    )
    assert beyond["items"] == [] and beyond["total"] == 4
    offset_only = _get(
        client, "review-work-items", _headers_for(reviewer), workspace_id=ws.id, offset=0
    )
    assert offset_only["limit"] == 20


def _seed_asset(db, workspace):
    batch = DataBatch(name=f"Paging-{uuid4().hex}", workspace_id=workspace.id)
    db.add(batch)
    db.flush()
    asset = DataAsset(
        data_batch_id=batch.id,
        workspace_id=workspace.id,
        governed_valid_duration_hours=Decimal("1"),
    )
    db.add(asset)
    db.flush()
    return asset


def test_asset_pages_preserve_global_scope_and_optional_workspace_filter(
    client, db_session, admin_headers
):
    ws, other_ws = make_workspace(db_session), make_workspace(db_session)
    mine = [_seed_asset(db_session, ws) for _ in range(3)]
    other = _seed_asset(db_session, other_ws)
    db_session.commit()
    page = _get(client, "data-assets", admin_headers, source_workspace_id=ws.id, limit=1, offset=1)
    assert [row["id"] for row in page["items"]] == [mine[1].id]
    assert page["total"] == 3
    beyond = _get(
        client, "data-assets", admin_headers, source_workspace_id=ws.id, limit=2, offset=3
    )
    assert beyond["items"] == [] and beyond["total"] == 3
    global_page = _get(client, "data-assets", admin_headers, limit=100)
    assert global_page["total"] == db_session.query(DataAsset).count()
    assert other.id in {row["id"] for row in global_page["items"]}
    legacy = _get(client, "data-assets", admin_headers, source_workspace_id=ws.id)
    assert len(legacy["items"]) == legacy["total"] == 3


def test_catalog_and_version_pages_count_before_windowing(client, db_session, admin_headers):
    datasets = [CatalogDataset(name=f"Paging-{uuid4().hex}") for _ in range(3)]
    db_session.add_all(datasets)
    db_session.flush()
    versions = [
        CatalogDatasetVersion(dataset_id=datasets[0].id, version=index) for index in range(1, 5)
    ]
    db_session.add_all(versions)
    db_session.add(CatalogDatasetVersion(dataset_id=datasets[1].id, version=1))
    db_session.commit()
    page = _get(client, "catalog-datasets", admin_headers, limit=1, offset=1)
    expected = (
        db_session.query(CatalogDataset)
        .order_by(CatalogDataset.created_at.desc(), CatalogDataset.id.desc())
        .offset(1)
        .first()
    )
    assert [row["id"] for row in page["items"]] == [expected.id]
    assert page["total"] == db_session.query(CatalogDataset).count()
    path = f"catalog-datasets/{datasets[0].id}/versions"
    page = _get(client, path, admin_headers, limit=2, offset=1)
    assert [row["version"] for row in page["items"]] == [2, 3]
    assert page["total"] == 4
    beyond = _get(client, path, admin_headers, limit=2, offset=4)
    assert beyond["items"] == [] and beyond["total"] == 4
    legacy = _get(client, path, admin_headers)
    assert len(legacy["items"]) == 4 and legacy["limit"] is None
    missing = client.get(
        "/api/v1/catalog-datasets/2147483647/versions", headers=admin_headers, params={"limit": 1}
    )
    assert missing.status_code == 404


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
def test_list_page_bounds_are_validated(client, admin_headers, params):
    for path in [
        "annotation-work-items?workspace_id=1",
        "review-work-items?workspace_id=1",
        "data-assets",
        "catalog-datasets",
        "catalog-datasets/1/versions",
    ]:
        response = client.get(f"/api/v1/{path}", headers=admin_headers, params=params)
        assert response.status_code == 422, (path, response.text)


def test_catalog_read_permission_is_checked_before_count(client, db_session):
    user = User(email=f"blocked-{uuid4().hex}@test.com", password_hash="unused", role="no_access")
    db_session.add(user)
    db_session.commit()
    for path in [
        "data-assets",
        "catalog-datasets",
        "catalog-datasets/1/versions",
        "catalog-datasets/versions/1",
        "catalog-datasets/exports/1/download",
    ]:
        response = client.get(f"/api/v1/{path}", headers=_headers_for(user), params={"limit": 1})
        assert response.status_code == 403, response.text
