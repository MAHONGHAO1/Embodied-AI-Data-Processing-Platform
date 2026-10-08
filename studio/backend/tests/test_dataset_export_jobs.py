from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event

from data.database import (
    Batch,
    Episode,
    EpisodeArtifact,
    PublishedEpisode,
    TaskSet,
    User,
    Workspace,
    WorkspaceMember,
)
from data.services.dataset_revisions import (
    DatasetItemInput,
    create_dataset_revision,
    create_dataset_revision_for_dataset,
    enqueue_dataset_export,
    enqueue_dataset_export_attempt,
    serialize_revision_summary,
)


def _revision(db_session):
    actor = db_session.query(User).order_by(User.id).first()
    workspace = Workspace(name=f"export workspace {uuid4().hex}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=actor.id))
    project = TaskSet(workspace_id=workspace.id, name=f"export project {uuid4().hex}")
    db_session.add(project)
    db_session.flush()
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="export batch",
        batch_type="ego",
    )
    db_session.add(batch)
    db_session.flush()
    episode = Episode(
        workspace_id=workspace.id,
        task_set_id=project.id,
        batch_id=batch.id,
        episode_uid=f"source-{uuid4().hex}",
        kind="source",
        modality="ego",
        source_fingerprint="e" * 64,
        task_language="close the box",
        metadata_json={"timing": {"start_timestamp_ns": 0, "end_timestamp_ns": 1000}},
    )
    db_session.add(episode)
    db_session.flush()
    artifact = EpisodeArtifact(
        episode_id=episode.id,
        artifact_type="official_qrdf",
        storage_role="official",
        storage_uri=f"nas://official/{uuid4().hex}",
        checksum_sha256="a" * 64,
        size_bytes=10,
    )
    db_session.add(artifact)
    db_session.flush()
    published = PublishedEpisode(
        episode_id=episode.id,
        official_artifact_id=artifact.id,
        publisher_user_id=actor.id,
        output_profile="qrdf",
        manifest_hash="b" * 64,
    )
    db_session.add(published)
    db_session.commit()
    revision = create_dataset_revision(
        db_session,
        workspace_id=workspace.id,
        name=f"export-{uuid4().hex}",
        actor_id=actor.id,
        items=[
            DatasetItemInput(
                episode_id=episode.id,
                sample_id="sample-1",
                core_start_ns=100,
                core_end_ns=500,
                effective_start_ns=0,
                effective_end_ns=800,
                task="close the box",
            )
        ],
    )
    return actor, revision


def test_existing_dataset_revision_creation_locks_its_dataset(db_session):
    """Version allocation must serialize concurrent writes to one Dataset."""
    actor, first_revision = _revision(db_session)
    published = db_session.get(PublishedEpisode, first_revision.items[0].published_episode_id)
    assert published is not None
    statements = []

    def capture_dataset_query(_conn, _cursor, statement, _parameters, _context, _executemany):
        if "FROM datasets" in statement:
            statements.append(statement)

    event.listen(db_session.bind, "before_cursor_execute", capture_dataset_query)
    try:
        second_revision = create_dataset_revision_for_dataset(
            db_session,
            dataset_id=first_revision.dataset_id,
            actor_id=actor.id,
            items=[
                DatasetItemInput(
                    episode_id=published.episode_id,
                    sample_id="sample-2",
                    core_start_ns=100,
                    core_end_ns=500,
                    effective_start_ns=0,
                    effective_end_ns=800,
                    task="close the box",
                )
            ],
        )
    finally:
        event.remove(db_session.bind, "before_cursor_execute", capture_dataset_query)

    assert second_revision.version == first_revision.version + 1
    assert any("FOR UPDATE" in statement.upper() for statement in statements)


def test_existing_dataset_revision_creation_is_idempotent_for_one_request_id(db_session):
    actor, first_revision = _revision(db_session)
    published = db_session.get(PublishedEpisode, first_revision.items[0].published_episode_id)
    request_id = str(uuid4())
    items = [DatasetItemInput(episode_id=published.episode_id, task="idempotent payload")]

    first = create_dataset_revision_for_dataset(
        db_session,
        dataset_id=first_revision.dataset_id,
        actor_id=actor.id,
        request_id=request_id,
        items=items,
    )
    second = create_dataset_revision_for_dataset(
        db_session,
        dataset_id=first_revision.dataset_id,
        actor_id=actor.id,
        request_id=request_id,
        items=items,
    )

    assert second.id == first.id
    assert second.version == first.version


def test_existing_dataset_revision_rejects_reused_request_id_with_different_items(db_session):
    from data.services.dataset_revisions import IdempotencyKeyReused

    actor, first_revision = _revision(db_session)
    published = db_session.get(PublishedEpisode, first_revision.items[0].published_episode_id)
    request_id = str(uuid4())
    create_dataset_revision_for_dataset(
        db_session,
        dataset_id=first_revision.dataset_id,
        actor_id=actor.id,
        request_id=request_id,
        items=[DatasetItemInput(episode_id=published.episode_id, task="first payload")],
    )

    with pytest.raises(IdempotencyKeyReused):
        create_dataset_revision_for_dataset(
            db_session,
            dataset_id=first_revision.dataset_id,
            actor_id=actor.id,
            request_id=request_id,
            items=[DatasetItemInput(episode_id=published.episode_id, task="changed payload")],
        )


def test_existing_dataset_revision_rejects_duplicate_episode_ids_before_writing(db_session):
    from data.services.workflow_conflict import WorkflowConflict

    actor, first_revision = _revision(db_session)
    published = db_session.get(PublishedEpisode, first_revision.items[0].published_episode_id)
    before = len(first_revision.dataset.revisions)

    with pytest.raises(WorkflowConflict, match="duplicate episode"):
        create_dataset_revision_for_dataset(
            db_session,
            dataset_id=first_revision.dataset_id,
            actor_id=actor.id,
            request_id=str(uuid4()),
            items=[
                DatasetItemInput(episode_id=published.episode_id),
                DatasetItemInput(episode_id=published.episode_id),
            ],
        )

    db_session.expire_all()
    assert (
        len(db_session.get(type(first_revision.dataset), first_revision.dataset_id).revisions)
        == before
    )


def test_dataset_revision_api_reports_changed_candidates_without_writing(
    client, admin_headers, db_session
):
    actor, first_revision = _revision(db_session)
    before = len(first_revision.dataset.revisions)
    missing_episode_id = 2_000_000_000

    response = client.post(
        f"/api/v1/datasets/{first_revision.dataset_id}/revisions",
        headers=admin_headers,
        json={
            "request_id": str(uuid4()),
            "items": [{"episode_id": missing_episode_id}],
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "dataset_revision_candidates_changed",
        "invalid_items": [
            {"episode_id": missing_episode_id, "reason": "candidate_unavailable"},
        ],
    }
    db_session.expire_all()
    assert (
        len(db_session.get(type(first_revision.dataset), first_revision.dataset_id).revisions)
        == before
    )


def test_dataset_export_job_uses_frozen_revision_manifest(db_session, tmp_storage, monkeypatch):
    from data.services import dataset_export_jobs

    actor, revision = _revision(db_session)
    job = enqueue_dataset_export(db_session, revision.id, actor_id=actor.id)
    captured = {}

    def fake_export(manifest, work_dir, *, options):
        captured["manifest"] = manifest
        captured["options"] = options
        archive = work_dir / "published_sample_lerobot_v3.zip"
        archive.write_bytes(b"zip")
        return SimpleNamespace(
            archive_path=archive,
            report=SimpleNamespace(
                lerobot_version="v3.0",
                items=(object(),),
                input_manifest_sha256="c" * 64,
                artifact_manifest_sha256="d" * 64,
            ),
        )

    def fake_publish(path, **kwargs):
        assert path.is_file()
        captured["publish"] = kwargs
        return "nas://exports/published-sample.zip"

    monkeypatch.setattr(dataset_export_jobs, "export_published_sample_revision", fake_export)
    monkeypatch.setattr(dataset_export_jobs, "publish_export_output", fake_publish)

    result = dataset_export_jobs.run_dataset_revision_export(job.id)

    assert result["dataset_revision_id"] == revision.id
    assert result["export_uri"] == "nas://exports/published-sample.zip"
    assert captured["manifest"] == job.detail_json["manifest"]
    assert captured["manifest"]["items"][0]["item_kind"] == "sample"
    assert captured["options"].export_mode == "ego_rgb"
    assert captured["publish"]["workspace_id"] == revision.workspace_id


def test_dataset_export_profiles_are_independent_and_idempotent(db_session):
    actor, revision = _revision(db_session)

    lerobot = enqueue_dataset_export_attempt(
        db_session,
        revision.id,
        actor_id=actor.id,
        export_profile="lerobot",
    )
    qrdf = enqueue_dataset_export_attempt(
        db_session,
        revision.id,
        actor_id=actor.id,
        export_profile="qrdf",
    )
    repeated_qrdf = enqueue_dataset_export_attempt(
        db_session,
        revision.id,
        actor_id=actor.id,
        export_profile="qrdf",
    )

    assert lerobot.created is True
    assert qrdf.created is True
    assert lerobot.job.id != qrdf.job.id
    assert lerobot.job.detail_json["export_profile"] == "lerobot"
    assert qrdf.job.detail_json["export_profile"] == "qrdf"
    assert repeated_qrdf.created is False
    assert repeated_qrdf.job.id == qrdf.job.id
    summary = serialize_revision_summary(db_session, revision.id)
    assert summary["exports"]["lerobot"]["id"] == lerobot.job.id
    assert summary["exports"]["qrdf"]["id"] == qrdf.job.id


def test_qrdf_dataset_export_job_uses_frozen_revision_manifest(
    db_session, tmp_storage, monkeypatch
):
    from data.services import dataset_export_jobs

    actor, revision = _revision(db_session)
    job = enqueue_dataset_export(
        db_session,
        revision.id,
        actor_id=actor.id,
        export_profile="qrdf",
    )
    captured = {}

    def fake_export(manifest, work_dir):
        captured["manifest"] = manifest
        archive = work_dir / "dataset_revision_qrdf.zip"
        archive.write_bytes(b"zip")
        return SimpleNamespace(
            archive_path=archive,
            item_count=1,
            dataset_manifest_sha256="c" * 64,
            source_manifest_sha256="d" * 64,
        )

    def fake_publish(path, **kwargs):
        assert path.is_file()
        captured["publish"] = kwargs
        return "nas://exports/dataset-revision-qrdf.zip"

    monkeypatch.setattr(dataset_export_jobs, "export_qrdf_revision", fake_export)
    monkeypatch.setattr(dataset_export_jobs, "publish_export_output", fake_publish)

    result = dataset_export_jobs.run_dataset_revision_export(job.id)

    assert captured["manifest"] == job.detail_json["manifest"]
    assert captured["publish"]["template"] == "qrdf"
    assert captured["publish"]["lerobot_version"] is None
    assert result["export_profile"] == "qrdf"
    assert result["item_count"] == 1
    assert result["dataset_manifest_sha256"] == "c" * 64


def test_export_staging_cleanup_rejects_a_globbed_job_id(tmp_storage):
    from data.services.cloud_storage import cleanup_export_staging

    unrelated_staging = tmp_storage / "exports" / "export_unrelated_deadbeef"
    unrelated_staging.mkdir(parents=True)
    (unrelated_staging / "keep.txt").write_text("keep", encoding="utf-8")

    assert cleanup_export_staging("*") == []
    assert unrelated_staging.is_dir()


def test_dataset_export_job_refuses_tampered_job_manifest(db_session, tmp_storage, monkeypatch):
    from data.database import JobRun
    from data.services import dataset_export_jobs
    from data.services.workflow_conflict import WorkflowConflict

    actor, revision = _revision(db_session)
    job = enqueue_dataset_export(db_session, revision.id, actor_id=actor.id)
    stored = db_session.get(JobRun, job.id)
    detail = dict(stored.detail_json)
    manifest = dict(detail["manifest"])
    items = [dict(item) for item in manifest["items"]]
    items[0]["package_uri"] = "nas://official/tampered"
    manifest["items"] = items
    detail["manifest"] = manifest
    stored.detail_json = detail
    db_session.commit()

    monkeypatch.setattr(
        dataset_export_jobs,
        "export_published_sample_revision",
        lambda *_args, **_kwargs: pytest.fail("tampered manifest must not reach exporter"),
    )

    with pytest.raises(WorkflowConflict, match="integrity"):
        dataset_export_jobs.run_dataset_revision_export(job.id)


def test_dataset_export_api_uses_new_route_and_realtime_job_event(
    client, admin_headers, db_session, monkeypatch
):
    from data.database import RealtimeEvent
    from data.routers import datasets

    _actor, revision = _revision(db_session)
    monkeypatch.setattr(datasets, "require_celery_worker", lambda _queue: None)
    monkeypatch.setattr(datasets, "dispatch_media_job", lambda job, **_kwargs: f"celery:{job.id}")

    response = client.post(
        f"/api/v1/dataset-revisions/{revision.id}/exports",
        headers=admin_headers,
        json={"export_profile": "lerobot"},
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["job"]["status"] == "queued"
    assert "storage_uri" not in response.text
    event = (
        db_session.query(RealtimeEvent)
        .filter(
            RealtimeEvent.resource_type == "job_run",
            RealtimeEvent.resource_id == payload["job"]["id"],
        )
        .one()
    )
    assert event.event_name == "job_run.updated"

    revision_response = client.get(
        f"/api/v1/dataset-revisions/{revision.id}", headers=admin_headers
    )
    assert revision_response.status_code == 200
    latest_export = revision_response.json()["data"]["latest_export"]
    assert latest_export["id"] == payload["job"]["id"]
    assert latest_export["status"] == "queued"
    assert "storage_uri" not in revision_response.text


def test_dataset_export_api_accepts_qrdf_profile(client, admin_headers, db_session, monkeypatch):
    from data.routers import datasets

    _actor, revision = _revision(db_session)
    monkeypatch.setattr(datasets, "require_celery_worker", lambda _queue: None)
    monkeypatch.setattr(datasets, "dispatch_media_job", lambda job, **_kwargs: f"celery:{job.id}")

    response = client.post(
        f"/api/v1/dataset-revisions/{revision.id}/exports",
        headers=admin_headers,
        json={"export_profile": "qrdf"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["job"]["export_profile"] == "qrdf"


def test_dataset_export_initial_request_is_idempotent(
    client, admin_headers, db_session, monkeypatch
):
    from data.routers import datasets

    _actor, revision = _revision(db_session)
    dispatched = []
    monkeypatch.setattr(datasets, "require_celery_worker", lambda _queue: None)
    monkeypatch.setattr(
        datasets,
        "dispatch_media_job",
        lambda job, **_kwargs: dispatched.append(job.id) or f"celery:{job.id}",
    )

    first = client.post(
        f"/api/v1/dataset-revisions/{revision.id}/exports",
        headers=admin_headers,
        json={"export_profile": "lerobot"},
    )
    second = client.post(
        f"/api/v1/dataset-revisions/{revision.id}/exports",
        headers=admin_headers,
        json={"export_profile": "lerobot"},
    )

    assert first.status_code == second.status_code == 200
    assert first.json()["data"]["job"]["id"] == second.json()["data"]["job"]["id"]
    assert second.json()["data"]["dispatch"] == "already_exists"
    assert dispatched == [first.json()["data"]["job"]["id"]]


def test_dataset_export_retry_uses_canonical_route_and_preserves_workspace_scope(
    client, admin_headers, db_session, monkeypatch
):
    from data.database import JobRun
    from data.routers import datasets

    _actor, revision = _revision(db_session)
    monkeypatch.setattr(datasets, "require_celery_worker", lambda _queue: None)
    monkeypatch.setattr(datasets, "dispatch_media_job", lambda job, **_kwargs: f"celery:{job.id}")

    created = client.post(
        f"/api/v1/dataset-revisions/{revision.id}/exports",
        headers=admin_headers,
        json={"export_profile": "lerobot"},
    )
    assert created.status_code == 200
    job_id = created.json()["data"]["job"]["id"]

    failed = db_session.get(JobRun, job_id)
    failed.status = "failed"
    failed.phase = "failed"
    failed.error_code = "export_failed"
    db_session.commit()

    retried = client.post(f"/api/v1/exports/{job_id}/retry", headers=admin_headers)
    assert retried.status_code == 200
    retry_job = retried.json()["data"]["job"]
    assert retry_job["id"] != job_id
    assert retry_job["status"] == "queued"
    assert db_session.get(JobRun, retry_job["id"]).workspace_id == revision.workspace_id

    status = client.get(f"/api/v1/exports/{retry_job['id']}", headers=admin_headers)
    assert status.status_code == 200
    assert status.json()["data"]["job"]["id"] == retry_job["id"]
    assert "storage_uri" not in status.text


def test_dataset_export_retry_does_not_redispatch_an_active_manual_attempt(
    client, admin_headers, db_session, monkeypatch
):
    from data.database import JobRun
    from data.routers import datasets

    _actor, revision = _revision(db_session)
    dispatched = []
    monkeypatch.setattr(datasets, "require_celery_worker", lambda _queue: None)
    monkeypatch.setattr(
        datasets,
        "dispatch_media_job",
        lambda job, **_kwargs: dispatched.append(job.id) or f"celery:{job.id}",
    )

    created = client.post(
        f"/api/v1/dataset-revisions/{revision.id}/exports",
        headers=admin_headers,
        json={"export_profile": "lerobot"},
    )
    source_job_id = created.json()["data"]["job"]["id"]
    source = db_session.get(JobRun, source_job_id)
    source.status = "failed"
    source.phase = "failed"
    db_session.commit()

    first_retry = client.post(f"/api/v1/exports/{source_job_id}/retry", headers=admin_headers)
    second_retry = client.post(f"/api/v1/exports/{source_job_id}/retry", headers=admin_headers)

    assert first_retry.status_code == second_retry.status_code == 200
    assert first_retry.json()["data"]["job"]["id"] == second_retry.json()["data"]["job"]["id"]
    assert len(dispatched) == 2


def test_dataset_export_download_url_is_direct_only_after_authorized_lookup(
    client, admin_headers, db_session, monkeypatch
):
    from data.database import JobRun
    from data.routers import datasets

    actor, revision = _revision(db_session)
    job = enqueue_dataset_export(db_session, revision.id, actor_id=actor.id)
    stored = db_session.get(JobRun, job.id)
    stored.result_json = {
        "export_uri": "oss://export-bucket/exports/ws1/proj0/ds1/r1/v3.0/lerobot/archive.zip"
    }
    db_session.commit()

    monkeypatch.setattr(
        datasets,
        "issue_browser_download_url",
        lambda *_args, **_kwargs: type(
            "Access",
            (),
            {
                "as_payload": lambda self: {
                    "direct": True,
                    "url": "https://browser.example/revision.zip?signature=redacted",
                    "expires_at": "2099-01-01T00:00:00+00:00",
                    "media_type": "application/zip",
                }
            },
        )(),
    )

    response = client.get(f"/api/v1/exports/{job.id}/download-url", headers=admin_headers)

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["direct"] is True
    assert payload["url"].startswith("https://browser.example/")
    assert "oss://" not in response.text


def test_dataset_export_delivery_descriptor_returns_only_authorized_oss_uri(
    client, admin_headers, db_session
):
    from data.database import JobRun

    actor, revision = _revision(db_session)
    job = enqueue_dataset_export(db_session, revision.id, actor_id=actor.id)
    stored = db_session.get(JobRun, job.id)
    stored.result_json = {
        "export_uri": "oss://export-bucket/exports/ws1/proj0/ds1/r1/lerobot/archive.zip"
    }
    db_session.commit()

    delivered = client.get(f"/api/v1/exports/{job.id}/delivery", headers=admin_headers)

    assert delivered.status_code == 200
    assert delivered.json()["data"] == {
        "available": True,
        "oss_uri": "oss://export-bucket/exports/ws1/proj0/ds1/r1/lerobot/archive.zip",
    }

    stored.result_json = {"export_uri": "nas://exports/private/archive.zip"}
    db_session.commit()

    unavailable = client.get(f"/api/v1/exports/{job.id}/delivery", headers=admin_headers)

    assert unavailable.status_code == 200
    assert unavailable.json()["data"] == {"available": False, "oss_uri": ""}


def test_dataset_export_routes_reject_a_job_outside_its_revision_workspace(
    client, admin_headers, db_session
):
    from data.database import JobRun

    actor, revision = _revision(db_session)
    job = enqueue_dataset_export(db_session, revision.id, actor_id=actor.id)
    stored = db_session.get(JobRun, job.id)
    stored.workspace_id = None
    db_session.commit()

    response = client.get(f"/api/v1/exports/{job.id}", headers=admin_headers)

    assert response.status_code == 404


def test_dataset_routes_return_batch_episode_safe_summaries(client, admin_headers, db_session):
    from data.database import PublishedEpisode

    _actor, revision = _revision(db_session)
    published = db_session.get(PublishedEpisode, revision.items[0].published_episode_id)

    listing = client.get(
        f"/api/v1/datasets?workspace_id={revision.workspace_id}&limit=10&offset=0",
        headers=admin_headers,
    )
    assert listing.status_code == 200
    listing_data = listing.json()["data"]
    assert listing_data["total"] == 1
    assert listing_data["items"][0]["id"] == revision.dataset_id
    assert "storage_uri" not in listing.text

    detail = client.get(f"/api/v1/datasets/{revision.dataset_id}", headers=admin_headers)
    assert detail.status_code == 200
    assert detail.json()["data"]["id"] == revision.dataset_id

    revisions = client.get(
        f"/api/v1/datasets/{revision.dataset_id}/revisions", headers=admin_headers
    )
    assert revisions.status_code == 200
    item = revisions.json()["data"]["items"][0]["items"][0]
    assert item["episode_id"] == published.episode_id


def test_dataset_container_routes_create_empty_dataset_then_immutable_revision(
    client, admin_headers, db_session
):
    from data.database import PublishedEpisode

    _actor, existing_revision = _revision(db_session)
    published = db_session.get(PublishedEpisode, existing_revision.items[0].published_episode_id)

    created = client.post(
        "/api/v1/datasets",
        headers=admin_headers,
        json={
            "workspace_id": existing_revision.workspace_id,
            "name": f"container-{uuid4().hex}",
            "description": "immutable sample collection",
        },
    )
    assert created.status_code == 200
    dataset = created.json()["data"]
    assert dataset["revision_count"] == 0
    assert "storage_uri" not in created.text

    revision = client.post(
        f"/api/v1/datasets/{dataset['id']}/revisions",
        headers=admin_headers,
        json={
            "items": [
                {
                    "episode_id": published.episode_id,
                    "sample_id": "box-close-1",
                    "core_start_ns": 100,
                    "core_end_ns": 500,
                    "effective_start_ns": 0,
                    "effective_end_ns": 800,
                    "task": "close the box",
                }
            ]
        },
    )
    assert revision.status_code == 200
    assert revision.json()["data"]["dataset_id"] == dataset["id"]
    assert revision.json()["data"]["items"][0]["episode_id"] == published.episode_id
    assert "split" not in revision.json()["data"]["items"][0]


def test_dataset_container_duplicate_name_returns_conflict_not_missing_workspace(
    client, admin_headers, db_session
):
    _actor, existing_revision = _revision(db_session)
    payload = {
        "workspace_id": existing_revision.workspace_id,
        "name": f"duplicate-container-{uuid4().hex}",
        "description": "duplicate name regression",
    }

    first = client.post("/api/v1/datasets", headers=admin_headers, json=payload)
    assert first.status_code == 200

    duplicate = client.post("/api/v1/datasets", headers=admin_headers, json=payload)
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == {
        "code": "dataset_name_exists",
        "message": "相同数采工作空间下数据集名称不可重复",
    }
