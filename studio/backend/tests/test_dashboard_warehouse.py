"""Unit and API coverage for the Episode dashboard warehouse."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from data.database import (
    JOB_STATUS_SUCCEEDED,
    AdsDailyKpi,
    AdsDashboardSnapshot,
    DwdEpisodeFact,
    DwsEpisode5m,
    Episode,
    TaskSet,
    User,
    Workspace,
    WorkspaceMember,
)
from data.services.batches import create_batch
from data.services.dashboard_scope import DashboardScope
from data.services.dashboard_warehouse import (
    align_bucket_start,
    asset_library_duration_s,
    build_dwd,
    build_live_overview_payload,
    duration_bucket_for,
    empty_dashboard_payload,
    enqueue_dashboard_etl,
    parse_episode_duration_s,
    queue_counts_from_asset_facts,
    refresh_ads,
    request_dashboard_refresh,
    resolve_pipeline_stage,
    resolve_today_queue_key,
    roll_dws,
    run_dashboard_etl,
    unique_countable_asset_facts,
)
from data.services.job_access import actor_can_access_job_resource
from data.services.job_runs import create_or_get_job


def test_dashboard_scope_keys_are_stable():
    assert DashboardScope.global_scope().key == "global"
    assert DashboardScope.for_workspace(7).key == "workspace:7"
    assert DashboardScope.for_task_set(workspace_id=7, task_set_id=11).key == "task-set:11"


def test_warehouse_includes_collection_package_episodes_without_legacy_batch_ids(db_session):
    from tests.collection_api_fixtures import make_assigned_package, make_project, make_workspace

    from data.services.dashboard_warehouse import collect_episode_fact_mappings

    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    episode = Episode(
        episode_uid=f"collection-{uuid4().hex}",
        workspace_id=workspace.id,
        data_package_id=package.id,
        task_set_id=None,
        batch_id=None,
        kind="source",
        modality="ego",
        workflow_status="discovered",
        metadata_json={"timing": {"duration_s": 37.5}},
    )
    db_session.add(episode)
    db_session.commit()
    derived = Episode(
        episode_uid=f"collection-derived-{uuid4().hex}",
        workspace_id=workspace.id,
        data_package_id=package.id,
        task_set_id=None,
        batch_id=None,
        kind="derived",
        parent_episode_id=episode.id,
        derivation_version=1,
        modality="ego",
        source_start_ns=0,
        source_end_ns=10_000_000_000,
        workflow_status="ready",
        quality_status="pending",
        annotation_status="pending",
        review_status="pending",
        metadata_json={},
    )
    db_session.add(derived)
    db_session.commit()
    rows = collect_episode_fact_mappings(
        db_session, etl_at=datetime.utcnow(), episode_ids=[episode.id]
    )
    assert len(rows) == 1
    assert rows[0]["episode_id"] == episode.id
    assert rows[0]["workspace_id"] == workspace.id
    assert rows[0]["task_set_id"] is None
    assert rows[0]["batch_id"] is None
    assert rows[0]["duration_s"] == 37.5
    build_dwd(db_session, etl_at=datetime.utcnow())
    fact = db_session.get(DwdEpisodeFact, episode.id)
    assert fact is not None
    assert fact.duration_s == 37.5
    derived_fact = db_session.get(DwdEpisodeFact, derived.id)
    assert derived_fact is not None
    assert derived_fact.duration_s == 10.0

    scope = DashboardScope.for_workspace(workspace.id)
    bucket = align_bucket_start(datetime.utcnow())
    roll_dws(db_session, scope=scope, bucket_start=bucket, etl_at=datetime.utcnow())
    payload = refresh_ads(
        db_session,
        scope=scope,
        bucket_start=bucket,
        etl_at=datetime.utcnow(),
        etl_job_id="collection-package-dashboard-job",
    )
    assert payload["kpis"]["total_episodes"] == 2
    assert payload["kpis"]["total_duration_s"] == 47.5
    assert payload["pipeline_funnel"]["avg_duration_s"] == 37.5

    # A package fact with no legacy task-set identity is visible at workspace
    # scope, but must not be attributed to an unrelated legacy task set.
    legacy_task_set = TaskSet(
        workspace_id=workspace.id,
        name=f"unrelated-legacy-{uuid4().hex[:8]}",
    )
    db_session.add(legacy_task_set)
    db_session.commit()
    task_scope = DashboardScope.for_task_set(
        workspace_id=workspace.id,
        task_set_id=legacy_task_set.id,
    )
    task_bucket = align_bucket_start(datetime.utcnow())
    roll_dws(
        db_session,
        scope=task_scope,
        bucket_start=task_bucket,
        etl_at=datetime.utcnow(),
    )
    task_payload = refresh_ads(
        db_session,
        scope=task_scope,
        bucket_start=task_bucket,
        etl_at=datetime.utcnow(),
        etl_job_id="collection-package-task-scope-job",
    )
    assert task_payload["kpis"]["total_episodes"] == 0


def test_dashboard_warehouse_models_carry_scope_columns():
    assert DwdEpisodeFact.__table__.columns["task_set_id"].nullable is True
    assert DwdEpisodeFact.__table__.columns["batch_id"].nullable is True
    for model in (DwsEpisode5m, AdsDashboardSnapshot, AdsDailyKpi):
        assert "scope_key" in model.__table__.columns
        assert "workspace_id" in model.__table__.columns
        assert "task_set_id" in model.__table__.columns

    unique_column_sets = {
        tuple(column.name for column in constraint.columns)
        for constraint in DwsEpisode5m.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert (
        "scope_key",
        "bucket_start",
        "grain",
        "dim_key",
        "dim_value",
    ) in unique_column_sets


def _scoped_project(db_session):
    actor = db_session.query(User).order_by(User.id).first()
    suffix = uuid4().hex
    workspace = Workspace(name=f"dashboard warehouse workspace {suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    project = TaskSet(workspace_id=workspace.id, name=f"dashboard warehouse project {suffix}")
    db_session.add(project)
    db_session.flush()
    return actor, workspace, project


def _add_source_episode(db_session, *, actor, workspace, task_set, uid: str, duration_s: float):
    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name=f"Dashboard batch {uid}",
        batch_type="ego",
        actor_id=actor.id,
    )
    episode = Episode(
        episode_uid=uid,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=batch.id,
        kind="source",
        modality="ego",
        workflow_status="ready",
        quality_status="passed",
        annotation_status="pending",
        review_status="pending",
        metadata_json={"metrics": {"duration_s": duration_s}},
    )
    db_session.add(episode)
    db_session.commit()
    return episode


def test_duration_and_stage_mapping():
    assert parse_episode_duration_s({"metrics": {"duration_s": 12}}) == 12.0
    assert parse_episode_duration_s({"timing": {"duration_s": 13.5}}) == 13.5
    assert parse_episode_duration_s({"duration_hours": 0.5}) == 1800.0
    assert parse_episode_duration_s({"metrics": {"duration_s": 0}}) is None
    assert duration_bucket_for(12) == "lt_30s"
    assert duration_bucket_for(45) == "bt_30_60s"
    assert duration_bucket_for(90) == "gt_60s"
    assert duration_bucket_for(None) == "unknown"

    assert (
        resolve_pipeline_stage(
            kind="source",
            workflow_status="published",
            quality_status="passed",
            annotation_status="accepted",
            review_status="accepted",
            is_published=True,
            has_derived_children=False,
        )
        == "stored"
    )
    assert (
        resolve_pipeline_stage(
            kind="derived",
            workflow_status="ready",
            quality_status="pending",
            annotation_status="accepted",
            review_status="pending",
            is_published=False,
            has_derived_children=False,
        )
        == "annotated"
    )
    assert (
        resolve_pipeline_stage(
            kind="derived",
            workflow_status="ready",
            quality_status="pending",
            annotation_status="pending",
            review_status="pending",
            is_published=False,
            has_derived_children=False,
        )
        == "separated"
    )
    assert (
        resolve_pipeline_stage(
            kind="source",
            workflow_status="ready",
            quality_status="passed",
            annotation_status="pending",
            review_status="pending",
            is_published=False,
            has_derived_children=False,
        )
        == "collected"
    )
    assert (
        resolve_pipeline_stage(
            kind="source",
            workflow_status="quality_pending",
            quality_status="pending",
            annotation_status="pending",
            review_status="pending",
            is_published=False,
            has_derived_children=False,
        )
        == "intake"
    )


def test_align_bucket_start():
    moment = datetime(2026, 8, 12, 10, 17, 42)
    assert align_bucket_start(moment) == datetime(2026, 8, 12, 10, 15, 0)


def test_empty_dashboard_payload_shape():
    payload = empty_dashboard_payload()
    assert payload["kpis"]["total_episodes"] == 0
    assert len(payload["pipeline_funnel"]["stages"]) == 5
    assert len(payload["today_queues"]) == 5


def test_unique_countable_asset_facts_dedupe_by_episode_id():
    from types import SimpleNamespace

    facts = [
        SimpleNamespace(episode_id=1, is_countable=True, duration_s=10.0),
        SimpleNamespace(episode_id=1, is_countable=True, duration_s=10.0),
        SimpleNamespace(episode_id=2, is_countable=True, duration_s=5.0),
        SimpleNamespace(episode_id=3, is_countable=False, duration_s=99.0),
    ]
    unique = unique_countable_asset_facts(facts)
    assert [fact.episode_id for fact in unique] == [1, 2]
    assert asset_library_duration_s(facts) == 15.0


def test_today_queue_keys_partition_countable_pipeline_stages():
    from types import SimpleNamespace

    assert resolve_today_queue_key(pipeline_stage="intake", annotation_status="pending") == "intake"
    assert (
        resolve_today_queue_key(pipeline_stage="collected", annotation_status="pending")
        == "collected"
    )
    assert (
        resolve_today_queue_key(pipeline_stage="separated", annotation_status="pending")
        == "separated"
    )
    assert (
        resolve_today_queue_key(pipeline_stage="separated", annotation_status="submitted")
        == "separated"
    )
    assert (
        resolve_today_queue_key(pipeline_stage="annotated", annotation_status="accepted")
        == "annotated"
    )
    assert (
        resolve_today_queue_key(pipeline_stage="stored", annotation_status="accepted") == "stored"
    )
    assert (
        resolve_today_queue_key(pipeline_stage="unknown", annotation_status="pending") == "intake"
    )

    facts = [
        SimpleNamespace(
            episode_id=1, is_countable=True, pipeline_stage="collected", annotation_status="pending"
        ),
        SimpleNamespace(
            episode_id=2, is_countable=True, pipeline_stage="collected", annotation_status="pending"
        ),
        SimpleNamespace(
            episode_id=3, is_countable=True, pipeline_stage="separated", annotation_status="pending"
        ),
        SimpleNamespace(
            episode_id=3, is_countable=True, pipeline_stage="separated", annotation_status="pending"
        ),
        SimpleNamespace(
            episode_id=4, is_countable=False, pipeline_stage="intake", annotation_status="pending"
        ),
    ]
    counts = queue_counts_from_asset_facts(facts)
    assert counts == {
        "intake": 0,
        "collected": 2,
        "separated": 1,
        "annotated": 0,
        "stored": 0,
    }
    assert sum(counts.values()) == 3


def test_workspace_etl_excludes_other_workspace(db_session):
    actor, own_workspace, own_task_set = _scoped_project(db_session)
    _, other_workspace, other_task_set = _scoped_project(db_session)
    _add_source_episode(
        db_session,
        actor=actor,
        workspace=own_workspace,
        task_set=own_task_set,
        uid="ep-dashboard-own-scope",
        duration_s=12,
    )
    _add_source_episode(
        db_session,
        actor=actor,
        workspace=other_workspace,
        task_set=other_task_set,
        uid="ep-dashboard-other-scope",
        duration_s=95,
    )
    scope = DashboardScope.for_workspace(own_workspace.id)
    bucket = align_bucket_start(datetime(2026, 8, 13, 10, 17))
    now = datetime(2026, 8, 13, 10, 18)

    build_dwd(db_session, etl_at=now)
    roll_dws(db_session, scope=scope, bucket_start=bucket, etl_at=now)
    payload = refresh_ads(
        db_session,
        scope=scope,
        bucket_start=bucket,
        etl_at=now,
        etl_job_id="workspace-job",
    )

    assert payload["scope"] == scope.as_dict()
    assert payload["kpis"]["total_episodes"] == 1
    assert payload["kpis"]["total_duration_s"] == 12
    queues = {row["key"]: row for row in payload["today_queues"]}
    assert sum(row["count"] for row in payload["today_queues"]) == 1
    assert queues["collected"]["count"] == 1
    assert queues["collected"]["share"] == 1.0


def test_derived_duration_prefers_authoritative_source_interval(db_session):
    actor, workspace, task_set = _scoped_project(db_session)
    source = _add_source_episode(
        db_session,
        actor=actor,
        workspace=workspace,
        task_set=task_set,
        uid="ep-dashboard-duration-source",
        duration_s=600,
    )
    derived = Episode(
        episode_uid="ep-dashboard-duration-derived",
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=source.batch_id,
        kind="derived",
        parent_episode_id=source.id,
        derivation_version=1,
        modality="ego",
        source_start_ns=10_000_000_000,
        source_end_ns=25_000_000_000,
        workflow_status="ready",
        quality_status="passed",
        annotation_status="pending",
        review_status="pending",
        metadata_json={},
    )
    db_session.add(derived)
    db_session.commit()

    build_dwd(db_session, etl_at=datetime.utcnow())
    fact = db_session.query(DwdEpisodeFact).filter_by(episode_id=derived.id).one()

    assert fact.duration_s == 15.0
    assert fact.duration_bucket == "lt_30s"


def test_build_dwd_does_not_rewrite_unchanged_episode_facts(db_session):
    actor, workspace, task_set = _scoped_project(db_session)
    episode = _add_source_episode(
        db_session,
        actor=actor,
        workspace=workspace,
        task_set=task_set,
        uid="ep-dashboard-incremental-dwd",
        duration_s=18,
    )
    first_etl = episode.updated_at + timedelta(minutes=1)
    second_etl = first_etl + timedelta(minutes=5)

    assert build_dwd(db_session, etl_at=first_etl) >= 1
    fact = db_session.get(DwdEpisodeFact, episode.id)
    assert fact is not None
    assert fact.etl_at == first_etl

    assert build_dwd(db_session, etl_at=second_etl) == 0
    db_session.refresh(fact)
    assert fact.etl_at == first_etl

    episode.annotation_status = "accepted"
    episode.updated_at = second_etl
    db_session.flush()
    assert build_dwd(db_session, etl_at=second_etl) == 1
    db_session.refresh(fact)
    assert fact.annotation_status == "accepted"
    assert fact.etl_at == second_etl


def test_dws_and_ads_aggregate_without_loading_scoped_fact_entities(db_session, monkeypatch):
    actor, workspace, task_set = _scoped_project(db_session)
    _add_source_episode(
        db_session,
        actor=actor,
        workspace=workspace,
        task_set=task_set,
        uid="ep-dashboard-sql-rollup",
        duration_s=36,
    )
    scope = DashboardScope.for_task_set(workspace_id=workspace.id, task_set_id=task_set.id)
    bucket = align_bucket_start(datetime(2026, 8, 20, 10, 7))
    now = datetime(2026, 8, 20, 10, 8)
    build_dwd(db_session, etl_at=now)

    monkeypatch.setattr(
        "data.services.dashboard_warehouse._scoped_dwd_query",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("full fact load")),
    )

    roll_dws(db_session, scope=scope, bucket_start=bucket, etl_at=now)
    payload = refresh_ads(
        db_session,
        scope=scope,
        bucket_start=bucket,
        etl_at=now,
        etl_job_id="sql-rollup-job",
    )

    assert payload["kpis"]["total_episodes"] == 1
    assert payload["kpis"]["total_duration_s"] == 36
    assert sum(row["count"] for row in payload["today_queues"]) == 1


def test_asset_library_kpis_count_unique_episodes_without_omitting_derived(db_session):
    actor, workspace, task_set = _scoped_project(db_session)
    source = _add_source_episode(
        db_session,
        actor=actor,
        workspace=workspace,
        task_set=task_set,
        uid="ep-dashboard-asset-source",
        duration_s=600,
    )
    derived = Episode(
        episode_uid="ep-dashboard-asset-derived",
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=source.batch_id,
        kind="derived",
        parent_episode_id=source.id,
        derivation_version=1,
        modality="ego",
        source_start_ns=10_000_000_000,
        source_end_ns=25_000_000_000,
        workflow_status="ready",
        quality_status="passed",
        annotation_status="pending",
        review_status="pending",
        metadata_json={},
    )
    db_session.add(derived)
    db_session.commit()

    scope = DashboardScope.for_workspace(workspace.id)
    bucket = align_bucket_start(datetime(2026, 8, 13, 10, 17))
    now = datetime(2026, 8, 13, 10, 18)
    build_dwd(db_session, etl_at=now)
    roll_dws(db_session, scope=scope, bucket_start=bucket, etl_at=now)
    payload = refresh_ads(
        db_session,
        scope=scope,
        bucket_start=bucket,
        etl_at=now,
        etl_job_id="asset-library-job",
    )

    assert payload["kpis"]["total_episodes"] == 2
    assert payload["kpis"]["total_duration_s"] == 615.0
    queues = {row["key"]: row for row in payload["today_queues"]}
    assert sum(row["count"] for row in payload["today_queues"]) == 2
    assert queues["collected"]["count"] == 1
    assert queues["separated"]["count"] == 1
    assert queues["collected"]["share"] + queues["separated"]["share"] == 1.0


def test_manual_refresh_reuses_active_job_in_same_scope_bucket(db_session, monkeypatch):
    _, workspace, _ = _scoped_project(db_session)
    scope = DashboardScope.for_workspace(workspace.id)
    bucket = datetime(2026, 8, 13, 10, 15)
    monkeypatch.setattr(
        "data.services.dashboard_warehouse.dispatch_media_job",
        lambda _job: "celery:test",
    )

    first = enqueue_dashboard_etl(db_session, scope=scope, bucket_start=bucket)
    second = enqueue_dashboard_etl(db_session, scope=scope, bucket_start=bucket)

    assert second.id == first.id
    assert first.idempotency_key == f"dashboard-etl:{scope.key}:{bucket.isoformat()}"
    assert first.detail_json["scope_key"] == scope.key


def test_live_overview_classifies_quality_passed_sources_with_same_grain(db_session):
    actor, workspace, task_set = _scoped_project(db_session)
    for index in range(5):
        _add_source_episode(
            db_session,
            actor=actor,
            workspace=workspace,
            task_set=task_set,
            uid=f"ep-dashboard-live-{index}",
            duration_s=40 + index,
        )
    scope = DashboardScope.for_task_set(workspace_id=workspace.id, task_set_id=task_set.id)
    payload = build_live_overview_payload(db_session, scope)

    assert payload["source"] == "live"
    assert payload["kpis"]["total_episodes"] == 5
    assert payload["kpis"]["collected_today"] == 5
    queues = {row["key"]: row["count"] for row in payload["today_queues"]}
    assert sum(queues.values()) == 5
    assert queues["collected"] == 5
    collected = next(
        stage for stage in payload["pipeline_funnel"]["stages"] if stage["key"] == "collected"
    )
    assert sum(collected["buckets"].values()) == queues["collected"]
    intake = next(
        stage for stage in payload["pipeline_funnel"]["stages"] if stage["key"] == "intake"
    )
    assert sum(intake["buckets"].values()) == 0


def test_request_dashboard_refresh_rebuilds_after_succeeded_job(db_session, monkeypatch):
    actor, workspace, task_set = _scoped_project(db_session)
    _add_source_episode(
        db_session,
        actor=actor,
        workspace=workspace,
        task_set=task_set,
        uid="ep-dashboard-refresh-1",
        duration_s=40,
    )
    scope = DashboardScope.for_workspace(workspace.id)
    bucket = datetime(2026, 8, 13, 10, 15)
    monkeypatch.setattr(
        "data.services.dashboard_warehouse.dispatch_media_job",
        lambda _job: "celery:test",
    )
    job = enqueue_dashboard_etl(db_session, scope=scope, bucket_start=bucket)
    job.status = JOB_STATUS_SUCCEEDED
    db_session.commit()

    _add_source_episode(
        db_session,
        actor=actor,
        workspace=workspace,
        task_set=task_set,
        uid="ep-dashboard-refresh-2",
        duration_s=41,
    )
    reused, payload = request_dashboard_refresh(
        db_session,
        scope=scope,
        actor_id=actor.id,
        bucket_start=bucket,
    )

    assert reused.id == job.id
    assert payload["kpis"]["total_episodes"] == 2
    assert sum(row["count"] for row in payload["today_queues"]) == 2
    snapshot = (
        db_session.query(AdsDashboardSnapshot)
        .filter(AdsDashboardSnapshot.scope_key == scope.key)
        .one()
    )
    assert snapshot.payload_json["kpis"]["total_episodes"] == 2


def test_dashboard_job_access_follows_dashboard_scope(db_session):
    actor, workspace, task_set = _scoped_project(db_session)
    member = User(
        email="dashboard-member@example.com", password_hash="test", role="annotator", is_active=True
    )
    viewer = User(
        email="dashboard-viewer@example.com", password_hash="test", role="auditor", is_active=True
    )
    outsider = User(
        email="dashboard-outsider@example.com",
        password_hash="test",
        role="annotator",
        is_active=True,
    )
    admin = db_session.query(User).filter(User.email == "admin@quicdata.com").one()
    db_session.add_all([member, viewer, outsider])
    db_session.flush()
    db_session.add_all(
        [
            WorkspaceMember(workspace_id=workspace.id, user_id=member.id),
            WorkspaceMember(workspace_id=workspace.id, user_id=viewer.id),
        ]
    )
    db_session.commit()

    # Dashboards are admin only, so a workspace member without the admin role
    # cannot read dashboard jobs even inside their own workspace.
    assert not actor_can_access_job_resource(
        db_session,
        actor_id=member.id,
        resource_type="platform",
        resource_id=f"workspace:{workspace.id}",
    )
    assert actor_can_access_job_resource(
        db_session,
        actor_id=admin.id,
        resource_type="platform",
        resource_id=f"workspace:{workspace.id}",
    )
    assert actor_can_access_job_resource(
        db_session,
        actor_id=admin.id,
        resource_type="platform",
        resource_id=f"task-set:{task_set.id}",
    )
    assert not actor_can_access_job_resource(
        db_session,
        actor_id=outsider.id,
        resource_type="platform",
        resource_id=f"workspace:{workspace.id}",
    )
    assert not actor_can_access_job_resource(
        db_session,
        actor_id=viewer.id,
        resource_type="platform",
        resource_id=f"workspace:{workspace.id}",
    )
    assert not actor_can_access_job_resource(
        db_session,
        actor_id=member.id,
        resource_type="platform",
        resource_id="global",
    )
    assert actor_can_access_job_resource(
        db_session,
        actor_id=admin.id,
        resource_type="platform",
        resource_id="global",
    )


def test_dashboard_worker_rejects_inconsistent_scope_detail(db_session):
    _, workspace, _ = _scoped_project(db_session)
    job = create_or_get_job(
        db_session,
        kind="dashboard_etl",
        resource_type="platform",
        resource_id=f"workspace:{workspace.id}",
        idempotency_key=f"dashboard-etl:forged:{workspace.id}",
        queue="analytics",
        workspace_id=workspace.id,
        detail={
            "scope_key": "global",
            "workspace_id": workspace.id,
            "task_set_id": None,
            "bucket_start": align_bucket_start().isoformat(),
        },
    )

    try:
        run_dashboard_etl(db_session, job)
    except ValueError as exc:
        assert "resource scope is inconsistent" in str(exc)
    else:
        raise AssertionError("inconsistent dashboard job scope must be rejected")


def test_dashboard_worker_rejects_inconsistent_persisted_scope(db_session):
    _, workspace, _ = _scoped_project(db_session)
    scope = DashboardScope.for_workspace(workspace.id)
    job = create_or_get_job(
        db_session,
        kind="dashboard_etl",
        resource_type="platform",
        resource_id=scope.key,
        idempotency_key=f"dashboard-etl:forged-persisted:{workspace.id}",
        queue="analytics",
        detail={
            **scope.as_dict(),
            "scope_key": scope.key,
            "bucket_start": align_bucket_start().isoformat(),
        },
    )

    try:
        run_dashboard_etl(db_session, job)
    except ValueError as exc:
        assert "resource scope is inconsistent" in str(exc)
    else:
        raise AssertionError("inconsistent persisted dashboard scope must be rejected")


def test_dashboard_job_api_enforces_scope_and_persisted_ownership(
    client,
    admin_headers,
    operator_headers,
    db_session,
):
    _, workspace, _ = _scoped_project(db_session)
    operator_id = client.get("/api/v1/auth/me", headers=operator_headers).json()["data"]["id"]
    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=operator_id))
    workspace_job = create_or_get_job(
        db_session,
        kind="dashboard_etl",
        resource_type="platform",
        resource_id=f"workspace:{workspace.id}",
        idempotency_key=f"dashboard-job-api-workspace:{workspace.id}",
        queue="analytics",
        workspace_id=workspace.id,
        detail={
            "scope_key": f"workspace:{workspace.id}",
            "workspace_id": workspace.id,
            "task_set_id": None,
        },
    )
    global_job = create_or_get_job(
        db_session,
        kind="dashboard_etl",
        resource_type="platform",
        resource_id="global",
        idempotency_key="dashboard-job-api-global",
        queue="analytics",
        detail={"scope_key": "global", "workspace_id": None, "task_set_id": None},
    )
    inconsistent_job = create_or_get_job(
        db_session,
        kind="dashboard_etl",
        resource_type="platform",
        resource_id="global",
        idempotency_key=f"dashboard-job-api-forged:{workspace.id}",
        queue="analytics",
        workspace_id=workspace.id,
        detail={"scope_key": "global", "workspace_id": None, "task_set_id": None},
    )
    inconsistent_detail_job = create_or_get_job(
        db_session,
        kind="dashboard_etl",
        resource_type="platform",
        resource_id=f"workspace:{workspace.id}",
        idempotency_key=f"dashboard-job-api-forged-detail:{workspace.id}",
        queue="analytics",
        workspace_id=workspace.id,
        detail={"scope_key": "global"},
    )
    db_session.commit()

    own_scope = client.get(f"/api/v1/jobs/{workspace_job.id}", headers=admin_headers)
    global_admin = client.get(f"/api/v1/jobs/{global_job.id}", headers=admin_headers)
    global_operator = client.get(f"/api/v1/jobs/{global_job.id}", headers=operator_headers)
    inconsistent_admin = client.get(f"/api/v1/jobs/{inconsistent_job.id}", headers=admin_headers)
    inconsistent_detail_admin = client.get(
        f"/api/v1/jobs/{inconsistent_detail_job.id}", headers=admin_headers
    )

    assert own_scope.status_code == 200
    assert own_scope.json()["data"]["job"]["id"] == workspace_job.id
    assert global_admin.status_code == 200
    # Dashboards are admin only: a non-admin member is rejected before the
    # scope check can hide the global job behind a 404.
    assert global_operator.status_code == 403
    assert inconsistent_admin.status_code == 404
    assert inconsistent_detail_admin.status_code == 404


def test_etl_builds_ads_snapshot(db_session):
    actor, workspace, project = _scoped_project(db_session)
    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="Dashboard ETL batch",
        batch_type="ego",
        actor_id=actor.id,
    )
    db_session.add(
        Episode(
            episode_uid="ep-dashboard-seed-1",
            workspace_id=workspace.id,
            task_set_id=project.id,
            batch_id=batch.id,
            kind="source",
            modality="ego",
            workflow_status="ready",
            quality_status="passed",
            annotation_status="pending",
            review_status="pending",
            metadata_json={"metrics": {"duration_s": 42.5}},
        )
    )
    db_session.commit()

    scope = DashboardScope.for_task_set(workspace_id=workspace.id, task_set_id=project.id)
    job = create_or_get_job(
        db_session,
        kind="dashboard_etl",
        resource_type="platform",
        resource_id=scope.key,
        idempotency_key="dashboard-etl:test:ep-dashboard-seed-1",
        queue="analytics",
        workspace_id=workspace.id,
        task_set_id=project.id,
        detail={
            **scope.as_dict(),
            "scope_key": scope.key,
            "bucket_start": align_bucket_start().isoformat(),
        },
    )
    result = run_dashboard_etl(db_session, job)
    assert result["total_episodes"] >= 1
    snapshot = (
        db_session.query(AdsDashboardSnapshot)
        .filter(AdsDashboardSnapshot.scope_key == scope.key)
        .one()
    )
    assert snapshot is not None
    assert isinstance(snapshot.payload_json, dict)
    assert snapshot.payload_json["kpis"]["total_episodes"] >= 1
    assert "pipeline_funnel" in snapshot.payload_json
    assert "today_queues" in snapshot.payload_json
    assert "collect_trend_7d" in snapshot.payload_json
    assert "device_distribution" in snapshot.payload_json


def test_dashboard_api_enforces_global_and_workspace_scope(
    client,
    admin_headers,
    operator_headers,
    viewer_headers,
    db_session,
):
    actor, workspace, project = _scoped_project(db_session)
    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="Dashboard API batch",
        batch_type="ego",
        actor_id=actor.id,
    )
    db_session.add(
        Episode(
            episode_uid="ep-dashboard-api-1",
            workspace_id=workspace.id,
            task_set_id=project.id,
            batch_id=batch.id,
            kind="source",
            modality="ego",
            workflow_status="ready",
            quality_status="passed",
            metadata_json={"metrics": {"duration_s": 33}},
        )
    )
    db_session.commit()

    operator_id = client.get("/api/v1/auth/me", headers=operator_headers).json()["data"]["id"]
    viewer_id = client.get("/api/v1/auth/me", headers=viewer_headers).json()["data"]["id"]
    db_session.add_all(
        [
            WorkspaceMember(workspace_id=workspace.id, user_id=operator_id),
            WorkspaceMember(workspace_id=workspace.id, user_id=viewer_id),
        ]
    )
    db_session.commit()

    bucket = align_bucket_start()
    now = datetime.utcnow()
    build_dwd(db_session, etl_at=now)
    global_scope = DashboardScope.global_scope()
    workspace_scope = DashboardScope.for_workspace(workspace.id)
    for scope in (global_scope, workspace_scope):
        roll_dws(db_session, scope=scope, bucket_start=bucket, etl_at=now)
        refresh_ads(
            db_session,
            scope=scope,
            bucket_start=bucket,
            etl_at=now,
            etl_job_id=f"test-job-{scope.key}",
        )
    db_session.commit()

    overview = client.get("/api/v1/dashboard/overview", headers=admin_headers)
    assert overview.status_code == 200
    body = overview.json()
    assert body["code"] == 200
    assert body["data"]["kpis"]["total_episodes"] >= 1

    operator_global = client.get("/api/v1/dashboard/overview", headers=operator_headers)
    assert operator_global.status_code == 403

    admin_overview = client.get(
        "/api/v1/dashboard/overview",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )
    assert admin_overview.status_code == 200
    assert admin_overview.json()["data"]["scope"]["key"] == f"workspace:{workspace.id}"
    member_overview = client.get(
        "/api/v1/dashboard/overview",
        headers=operator_headers,
        params={"workspace_id": workspace.id},
    )
    assert member_overview.status_code == 403

    denied = client.get(
        "/api/v1/dashboard/overview",
        headers=viewer_headers,
        params={"workspace_id": workspace.id},
    )
    assert denied.status_code == 403

    unauth = client.get("/api/v1/dashboard/overview")
    assert unauth.status_code == 401


def test_dashboard_api_rejects_non_member_and_cross_workspace_task_set(
    client,
    admin_headers,
    operator_headers,
    db_session,
):
    _, first_workspace, first_task_set = _scoped_project(db_session)
    _, second_workspace, second_task_set = _scoped_project(db_session)
    operator_id = client.get("/api/v1/auth/me", headers=operator_headers).json()["data"]["id"]
    db_session.add(WorkspaceMember(workspace_id=first_workspace.id, user_id=operator_id))
    db_session.commit()

    non_member = client.get(
        "/api/v1/dashboard/overview",
        headers=operator_headers,
        params={"workspace_id": second_workspace.id},
    )
    assert non_member.status_code == 403

    cross_workspace = client.get(
        "/api/v1/dashboard/overview",
        headers=admin_headers,
        params={
            "workspace_id": first_workspace.id,
            "task_set_id": second_task_set.id,
        },
    )
    assert cross_workspace.status_code == 403

    own_scope = client.get(
        "/api/v1/dashboard/overview",
        headers=admin_headers,
        params={
            "workspace_id": first_workspace.id,
            "task_set_id": first_task_set.id,
        },
    )
    assert own_scope.status_code == 200
    assert own_scope.json()["data"]["scope"]["key"] == f"task-set:{first_task_set.id}"

    missing_workspace = client.get(
        "/api/v1/dashboard/overview",
        headers=operator_headers,
        params={"task_set_id": first_task_set.id},
    )
    assert missing_workspace.status_code == 422


def test_dashboard_overview_get_uses_live_oltp_without_ads(client, admin_headers, db_session):
    actor, workspace, task_set = _scoped_project(db_session)
    _add_source_episode(
        db_session,
        actor=actor,
        workspace=workspace,
        task_set=task_set,
        uid="ep-dashboard-live-get",
        duration_s=44,
    )

    overview = client.get(
        "/api/v1/dashboard/overview",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "task_set_id": task_set.id},
    )
    assert overview.status_code == 200
    data = overview.json()["data"]
    assert data["source"] == "live"
    assert data["kpis"]["total_episodes"] == 1
    queues = {row["key"]: row["count"] for row in data["today_queues"]}
    assert sum(queues.values()) == 1
    assert queues["collected"] == 1
