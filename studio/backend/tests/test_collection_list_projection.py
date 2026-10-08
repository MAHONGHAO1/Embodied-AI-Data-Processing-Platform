"""Real, paginated package/batch lists with bounded summary queries."""

from decimal import Decimal

from sqlalchemy import event
from tests.collection_api_fixtures import (
    make_project,
    make_workspace,
    seed_intake_approved_package,
    seed_package_pending_intake_review,
)

from data.database import User, Workspace, WorkspaceMember
from data.models.data_batch import DataBatch, DataBatchPackage
from data.models.data_package import DataPackage, PackageIntakeReview
from data.services.collection_intake_review import review_data_package_intake
from data.services.package_list_projection import package_list_extras


def test_workspace_options_expose_collection_membership_without_expanding_it(
    client, db_session, admin_headers
):
    member_workspace = make_workspace(db_session)
    outside = Workspace(name="Admin-visible but not collection-authorized", creator="fixture")
    db_session.add(outside)
    db_session.commit()
    response = client.get("/api/v1/workspace/options", headers=admin_headers)
    assert response.status_code == 200
    options = {row["id"]: row for row in response.json()["data"]["list"]}
    assert options[member_workspace.id]["collection_access"] is True
    assert options[outside.id]["collection_access"] is False
    denied = client.get(
        "/api/v1/data-packages", headers=admin_headers, params={"workspace_id": outside.id}
    )
    assert denied.status_code == 403
    actor = db_session.query(User).filter_by(email="admin@quicdata.com").one()
    assert (
        db_session.query(WorkspaceMember)
        .filter_by(workspace_id=outside.id, user_id=actor.id)
        .count()
        == 0
    )


def test_package_pages_include_pending_data_and_real_metadata(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace, name="Real project")
    pending, _ = seed_package_pending_intake_review(db_session, workspace, project)
    approved = seed_intake_approved_package(db_session, workspace, project)
    other_workspace = make_workspace(db_session)
    seed_intake_approved_package(
        db_session, other_workspace, make_project(db_session, other_workspace)
    )
    rows = []
    for page in (1, 2):
        response = client.get(
            "/api/v1/data-packages",
            headers=admin_headers,
            params={"workspace_id": workspace.id, "page": page, "size": 1},
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["total"] == 2
        assert data["page"] == page
        assert len(data["items"]) == 1
        rows.extend(data["items"])
    assert {row["id"] for row in rows} == {pending.id, approved.id}
    assert all(row["project_name"] == "Real project" for row in rows)
    assert all(row["task_name"] and row["collector_name"] for row in rows)
    pending_row = next(row for row in rows if row["id"] == pending.id)
    approved_row = next(row for row in rows if row["id"] == approved.id)
    assert pending_row["captured_duration_s"] == 7200.0
    assert pending_row["intake_valid_duration_s"] is None
    assert pending_row["can_batch"] is False
    assert approved_row["captured_duration_s"] == 7200.0
    assert approved_row["intake_valid_duration_s"] == 7200.0
    assert approved_row["can_batch"] is True
    filtered = client.get(
        "/api/v1/data-packages",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "page": 1, "size": 20, "status": "intake_approved"},
    ).json()["data"]
    assert filtered["total"] == 1
    assert filtered["items"][0]["id"] == approved.id


def test_summary_query_count_does_not_grow_with_package_count(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    for _ in range(4):
        seed_intake_approved_package(db_session, workspace, project)

    def count_queries(limit):
        db_session.expire_all()
        packages = (
            db_session.query(DataPackage).filter_by(workspace_id=workspace.id).limit(limit).all()
        )
        statements = []

        def capture(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement)

        engine = db_session.get_bind()
        event.listen(engine, "before_cursor_execute", capture)
        try:
            summaries = package_list_extras(db_session, packages)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert len(summaries) == limit
        return len(statements)

    assert count_queries(4) == count_queries(1)


def test_package_duration_projection_uses_episode_seconds_and_review_gate(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    pending, episodes = seed_package_pending_intake_review(
        db_session,
        workspace,
        project,
        episode_hours=(20 / 3600,),
    )
    # The package column rounds 20 seconds to 0.01 hours (36 seconds).  The
    # list projection must use the persisted Episode timing instead.
    assert pending.captured_duration_hours == Decimal("0.01")
    before = package_list_extras(db_session, [pending])[pending.id]
    assert before["captured_duration_s"] == 20.0
    assert before["intake_valid_duration_s"] is None

    review_data_package_intake(
        db_session,
        workspace_id=workspace.id,
        data_package_id=pending.id,
        reviewer_user_id=None,
        verdict="approved",
        rejected_episode_ids=[],
        reason="",
    )
    db_session.commit()
    after = package_list_extras(db_session, [pending])[pending.id]
    assert after["captured_duration_s"] == 20.0
    assert after["intake_valid_duration_s"] == 20.0

    review = (
        db_session.query(PackageIntakeReview)
        .filter(PackageIntakeReview.data_package_id == pending.id)
        .one()
    )
    review.fact_attempts_json = {str(episodes[0].id): 999}
    db_session.commit()
    stale = package_list_extras(db_session, [pending])[pending.id]
    assert stale["intake_valid_duration_s"] is None


def test_package_duration_projection_keeps_unknown_null_and_rejected_zero(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    pending, episodes = seed_package_pending_intake_review(
        db_session,
        workspace,
        project,
        episode_hours=(20 / 3600,),
    )
    episodes[0].metadata_json = {}
    db_session.commit()
    unknown = package_list_extras(db_session, [pending])[pending.id]
    assert unknown["captured_duration_s"] is None
    assert unknown["intake_valid_duration_s"] is None

    rejected, rejected_episodes = seed_package_pending_intake_review(
        db_session,
        workspace,
        project,
        episode_hours=(20 / 3600,),
    )
    review_data_package_intake(
        db_session,
        workspace_id=workspace.id,
        data_package_id=rejected.id,
        reviewer_user_id=None,
        verdict="rejected",
        rejected_episode_ids=[episode.id for episode in rejected_episodes],
        reason="bad source",
    )
    db_session.commit()
    rejected_summary = package_list_extras(db_session, [rejected])[rejected.id]
    assert rejected_summary["captured_duration_s"] == 20.0
    assert rejected_summary["intake_valid_duration_s"] == 0.0


def test_batch_pages_expose_only_summary_and_detail_loads_packages(
    client, db_session, admin_headers
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = seed_intake_approved_package(db_session, workspace, project)
    batch = DataBatch(
        workspace_id=workspace.id, name="Actual batch", status="open", episode_count=1
    )
    db_session.add(batch)
    db_session.flush()
    db_session.add(DataBatchPackage(data_batch_id=batch.id, data_package_id=package.id))
    db_session.commit()
    response = client.get(
        "/api/v1/data-batches",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "page": 1, "size": 20},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["package_count"] == 1
    assert "assignment_snapshot" not in data["items"][0]
    assert "episode_ids" not in data["items"][0]
    detail = client.get(
        f"/api/v1/data-batches/{batch.id}",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["packages"][0]["package_uid"] == package.package_uid
