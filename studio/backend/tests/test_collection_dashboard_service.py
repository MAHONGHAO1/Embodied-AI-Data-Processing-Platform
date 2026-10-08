# ruff: noqa: E701, E702
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from tests.collection_api_fixtures import (
    make_collector,
    make_label,
    make_project,
    make_task,
    make_workspace,
)

from data.database import Workspace
from data.models.collection_core import CollectionTaskLabel
from data.models.data_package import DataPackage
from data.services.collection_dashboard import (
    DashboardFilterError,
    capacity_board,
    data_board,
    data_board_csv,
    efficiency_board,
    resolve_filters,
)

NOW = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)


def _package(
    db,
    task,
    *,
    status="pending_assignment",
    captured_at=None,
    captured_s=None,
    valid_s=None,
    size=None,
    valid_size=None,
    uploaded=False,
):
    collector = (
        make_collector(db, db.get(Workspace, task.workspace_id))
        if status != "pending_assignment"
        else None
    )
    package = DataPackage(
        package_uid=f"pkg-{task.id}-{id(task)}-{status}-{captured_s}",
        workspace_id=task.workspace_id,
        collection_project_id=task.collection_project_id,
        collection_task_id=task.id,
        status=status,
        target_duration_hours=Decimal("2.00"),
        responsible_collector_id=collector.id if collector else None,
        operator_collector_id=collector.id if collector else None,
        created_at=datetime(2026, 9, 20, 2, 0),
        captured_started_at=captured_at,
        captured_duration_s=captured_s,
        intake_valid_duration_s=valid_s,
        captured_size_bytes=size,
        intake_valid_size_bytes=valid_size,
        upload_completed_at=datetime(2026, 9, 28) if (uploaded or captured_s is not None) else None,
    )
    db.add(package)
    db.commit()
    return package


def _filters(db, workspace, **overrides):
    params = {"start_date": date(2026, 9, 1), "end_date": date(2026, 9, 29), "now": NOW}
    params.update(overrides)
    return resolve_filters(db, workspace_id=workspace.id, **params)


def test_totals_and_shanghai_trend(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    task = make_task(db_session, project)
    _package(db_session, task)
    _package(
        db_session,
        task,
        status="pending_intake_review",
        captured_at=datetime(2026, 9, 28, 1),
        captured_s=Decimal("1800.000"),
        size=100,
    )
    _package(
        db_session,
        task,
        status="intake_approved",
        captured_at=datetime(2026, 9, 29, 1),
        captured_s=Decimal("3600.000"),
        valid_s=Decimal("3000.000"),
        size=200,
        valid_size=150,
    )
    _package(
        db_session,
        task,
        status="voided",
        captured_at=datetime(2026, 9, 29, 2),
        captured_s=Decimal("600.000"),
        valid_s=Decimal("0.000"),
        size=50,
        valid_size=0,
    )
    payload = data_board(db_session, _filters(db_session, workspace), now=NOW)
    assert payload["packages"]["total"] == 4
    assert payload["packages"]["today"] == 2
    assert payload["duration"]["total_s"] == 3000.0
    assert payload["duration"]["pending_review_duration_s"] == 1800.0
    assert payload["size"]["total_bytes"] == 150


def test_trend_buckets_use_shanghai_boundary(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    task = make_task(db_session, project)
    _package(
        db_session,
        task,
        status="intake_approved",
        captured_at=datetime(2026, 9, 27, 16, 30),
        captured_s=Decimal("60.000"),
        valid_s=Decimal("60.000"),
        size=1,
        valid_size=1,
    )
    payload = data_board(
        db_session,
        _filters(db_session, workspace, start_date=date(2026, 9, 27), end_date=date(2026, 9, 28)),
        now=NOW,
    )
    assert payload["duration"]["trend"] == [
        {"bucket": "2026-09-27", "value": 0.0},
        {"bucket": "2026-09-28", "value": 60.0},
    ]


def test_incomplete_packages_are_counted(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    task = make_task(db_session, project)
    _package(db_session, task, status="parsing", uploaded=True)
    payload = data_board(db_session, _filters(db_session, workspace, basis="all"), now=NOW)
    assert payload["incomplete_packages"] == 1
    assert payload["duration"]["total_s"] == 0.0


def test_label_filter_and_scope_validation(db_session):
    workspace = make_workspace(db_session)
    other = make_workspace(db_session)
    foreign = make_project(db_session, other)
    project = make_project(db_session, workspace)
    task = make_task(db_session, project)
    scene = make_label(db_session, category="scene")
    purpose = make_label(db_session, category="purpose")
    db_session.add(CollectionTaskLabel(collection_task_id=task.id, collection_label_id=scene.id))
    db_session.commit()
    _package(db_session, task)
    assert (
        data_board(
            db_session, _filters(db_session, workspace, scene_label_ids=[scene.id]), now=NOW
        )["packages"]["total"]
        == 1
    )
    with pytest.raises(DashboardFilterError, match="project"):
        _filters(db_session, workspace, project_ids=[foreign.id])
    with pytest.raises(DashboardFilterError, match="scene"):
        _filters(db_session, workspace, scene_label_ids=[purpose.id])


def test_filter_limits_and_csv(db_session):
    workspace = make_workspace(db_session)
    with pytest.raises(DashboardFilterError, match="7"):
        _filters(db_session, workspace, granularity="hour")
    payload = data_board(
        db_session,
        _filters(db_session, workspace, start_date=date(2026, 9, 28), end_date=date(2026, 9, 29)),
        now=NOW,
    )
    text = data_board_csv(payload)
    assert text.startswith("\ufeff时间,")


def test_capacity_uses_existing_package_facts_for_distribution_and_completion(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace, name="Project A")
    task = make_task(db_session, project, target_hours="2.00")
    collector = make_collector(db_session, workspace)
    package = _package(
        db_session,
        task,
        status="intake_approved",
        captured_at=datetime(2026, 9, 28, 1),
        captured_s=Decimal("3600.000"),
        valid_s=Decimal("1800.000"),
        size=10,
        valid_size=5,
    )
    package.operator_collector_id = collector.id
    package.responsible_collector_id = collector.id
    db_session.commit()
    payload = capacity_board(db_session, _filters(db_session, workspace), now=NOW)
    assert payload["projects"][0]["packages"] == 1
    assert payload["tasks"][0]["share"] == 1
    assert payload["completion"]["tasks"][0]["valid_s"] == 1800
    assert payload["completion"]["tasks"][0]["completion"] == 0.25
    assert payload["duration"]["valid_total_s"] == 1800
    assert payload["personnel"][0]["duration_s"] == 1800


def test_efficiency_ranks_personnel_by_valid_duration(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    task = make_task(db_session, project)
    first = make_collector(db_session, workspace, name="A")
    second = make_collector(db_session, workspace, name="B")
    p1 = _package(
        db_session,
        task,
        status="intake_approved",
        captured_at=datetime(2026, 9, 28, 1),
        captured_s=Decimal("3600.000"),
        valid_s=Decimal("1200.000"),
        size=1,
        valid_size=1,
    )
    p1.operator_collector_id = first.id
    p1.responsible_collector_id = first.id
    p2 = _package(
        db_session,
        task,
        status="intake_approved",
        captured_at=datetime(2026, 9, 29, 1),
        captured_s=Decimal("1800.000"),
        valid_s=Decimal("600.000"),
        size=1,
        valid_size=1,
    )
    p2.operator_collector_id = second.id
    p2.responsible_collector_id = second.id
    db_session.commit()
    payload = efficiency_board(db_session, _filters(db_session, workspace), now=NOW)
    assert payload["top5"][0]["name"] == "A"
    assert payload["top5"][0]["valid_duration_s"] == 1200
    assert payload["bottom5"][0]["name"] == "B"
