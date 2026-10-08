from datetime import datetime, timedelta
from uuid import uuid4

from collector_fixtures import make_collector

from data.database import (
    Batch,
    CollectionDevice,
    Episode,
    EpisodeArtifact,
    EpisodeCollectorAttribution,
    EpisodeDeviceAttribution,
    JobRun,
    TaskLabel,
    TaskSet,
    User,
    WorkItem,
    Workspace,
)
from data.services.import_sessions import create_import_session


def create_batch(
    db_session,
    *,
    workspace_id: int,
    task_set_id: int,
    name: str,
    batch_type: str,
    actor_id: int | None,
    metadata_json: dict | None = None,
) -> Batch:
    """Insert a collection batch row; the public batch API is retired."""

    batch = Batch(
        workspace_id=workspace_id,
        task_set_id=task_set_id,
        name=name,
        batch_type=batch_type,
        created_by_user_id=actor_id,
        metadata_json=dict(metadata_json or {}),
    )
    db_session.add(batch)
    db_session.flush()
    return batch


def _scoped_project(db_session):
    actor = db_session.query(User).order_by(User.id).first()
    suffix = uuid4().hex
    workspace = Workspace(name=f"service scope workspace {suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    project = TaskSet(workspace_id=workspace.id, name=f"service scope project {suffix}")
    db_session.add(project)
    db_session.flush()
    return actor, workspace, project


def _task_label(db_session, *, workspace_id: int, suffix: str) -> TaskLabel:
    label = TaskLabel(key=f"batch-api-{workspace_id}-{suffix}", name="close the box")
    db_session.add(label)
    db_session.flush()
    return label


def test_episode_list_is_server_paginated_with_stable_scope_order(
    client, admin_headers, db_session
):
    actor, workspace, task_set = _scoped_project(db_session)
    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name="episode page batch",
        batch_type="ego",
        actor_id=actor.id,
    )
    created = []
    base_time = datetime(2026, 1, 1)
    for index in range(55):
        episode = Episode(
            episode_uid=f"episode-page-{workspace.id}-{index}",
            workspace_id=workspace.id,
            task_set_id=task_set.id,
            batch_id=batch.id,
            kind="source",
            modality="ego",
            created_at=base_time + timedelta(seconds=index),
        )
        db_session.add(episode)
        created.append(episode)

    other_actor, other_workspace, other_task_set = _scoped_project(db_session)
    other_batch = create_batch(
        db_session,
        workspace_id=other_workspace.id,
        task_set_id=other_task_set.id,
        name="foreign episode page batch",
        batch_type="ego",
        actor_id=other_actor.id,
    )
    db_session.add(
        Episode(
            episode_uid=f"episode-page-foreign-{other_workspace.id}",
            workspace_id=other_workspace.id,
            task_set_id=other_task_set.id,
            batch_id=other_batch.id,
            kind="source",
            modality="ego",
        )
    )
    db_session.commit()

    first = client.get(
        "/api/v1/episodes",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "task_set_id": task_set.id},
    )
    second = client.get(
        "/api/v1/episodes",
        headers=admin_headers,
        params={
            "workspace_id": workspace.id,
            "task_set_id": task_set.id,
            "limit": 50,
            "offset": 50,
        },
    )

    assert first.status_code == 200
    assert first.json()["data"] == {
        "items": first.json()["data"]["items"],
        "total": 55,
        "limit": 50,
        "offset": 0,
    }
    assert len(first.json()["data"]["items"]) == 50
    assert [item["id"] for item in first.json()["data"]["items"]] == [
        episode.id for episode in reversed(created[5:])
    ]
    assert second.status_code == 200
    assert second.json()["data"]["total"] == 55
    assert [item["id"] for item in second.json()["data"]["items"]] == [
        episode.id for episode in reversed(created[:5])
    ]


def test_episode_list_bounds_page_size_and_rejects_cross_workspace_task_set(
    client, admin_headers, db_session
):
    _actor, workspace, _task_set = _scoped_project(db_session)
    _other_actor, _other_workspace, other_task_set = _scoped_project(db_session)
    db_session.commit()

    too_large = client.get(
        "/api/v1/episodes",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "limit": 201},
    )
    cross_scope = client.get(
        "/api/v1/episodes",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "task_set_id": other_task_set.id},
    )
    maximum = client.get(
        "/api/v1/episodes",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "limit": 200},
    )

    assert too_large.status_code == 422
    assert cross_scope.status_code == 403
    assert maximum.status_code == 200
    assert maximum.json()["data"]["limit"] == 200


def test_create_batch_and_import_session(db_session):
    actor, workspace, project = _scoped_project(db_session)
    task_label = _task_label(db_session, workspace_id=workspace.id, suffix="service")

    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="Batch A",
        batch_type="ego",
        actor_id=actor.id,
    )
    import_session = create_import_session(
        db_session,
        batch_id=batch.id,
        import_type="chunked_upload",
        actor_id=actor.id,
        task_label_id=task_label.id,
    )
    db_session.commit()

    assert batch.status == "created"
    assert import_session.batch_id == batch.id
    assert import_session.status == "init"
def test_episode_list_excludes_import_placeholders_by_default(client, admin_headers, db_session):
    actor, workspace, project = _scoped_project(db_session)
    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="Import placeholder visibility",
        batch_type="ego",
        actor_id=actor.id,
    )
    db_session.add_all(
        [
            Episode(
                episode_uid="visible-quality-pending",
                workspace_id=workspace.id,
                task_set_id=project.id,
                batch_id=batch.id,
                kind="source",
                modality="ego",
                workflow_status="quality_pending",
                quality_status="pending",
            ),
            Episode(
                episode_uid="hidden-importing",
                workspace_id=workspace.id,
                task_set_id=project.id,
                batch_id=batch.id,
                kind="source",
                modality="ego",
                workflow_status="importing",
                quality_status="not_started",
            ),
            Episode(
                episode_uid="hidden-import-failed",
                workspace_id=workspace.id,
                task_set_id=project.id,
                batch_id=batch.id,
                kind="source",
                modality="ego",
                workflow_status="import_failed",
                quality_status="not_started",
            ),
        ]
    )
    db_session.commit()

    visible = client.get(
        "/api/v1/episodes",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "task_set_id": project.id},
    )
    failed = client.get(
        "/api/v1/episodes",
        headers=admin_headers,
        params={
            "workspace_id": workspace.id,
            "task_set_id": project.id,
            "workflow_status": "import_failed",
        },
    )

    assert visible.status_code == 200
    assert [item["episode_uid"] for item in visible.json()["data"]["items"]] == [
        "visible-quality-pending"
    ]
    assert failed.status_code == 200
    assert [item["episode_uid"] for item in failed.json()["data"]["items"]] == [
        "hidden-import-failed"
    ]


def test_episode_read_does_not_expose_storage_uri(client, admin_headers, db_session):
    actor, workspace, project = _scoped_project(db_session)
    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="API batch",
        batch_type="ego",
        actor_id=actor.id,
    )
    episode = Episode(
        workspace_id=workspace.id,
        task_set_id=project.id,
        batch_id=batch.id,
        episode_uid="api-source-episode",
        kind="source",
        modality="ego",
    )
    db_session.add(episode)
    db_session.flush()
    db_session.add(
        EpisodeArtifact(
            episode_id=episode.id,
            artifact_type="raw_source",
            storage_role="raw",
            storage_uri="oss://raw/private/object",
        )
    )
    db_session.commit()

    detail = client.get(f"/api/v1/episodes/{episode.id}", headers=admin_headers)
    assert detail.status_code == 200
    artifact = detail.json()["data"]["artifacts"][0]
    assert artifact["artifact_type"] == "raw_source"
    assert "storage_uri" not in artifact
    assert "checksum_sha256" not in artifact
    assert "manifest_hash" not in artifact


def test_episode_list_filters_lineage_review_and_projects_safe_asset_states(
    client, admin_headers, db_session
):
    actor, workspace, project = _scoped_project(db_session)
    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="Lineage assets",
        batch_type="ego",
        actor_id=actor.id,
    )
    collector = make_collector(db_session, workspace_id=workspace.id, name="Ava")
    device = CollectionDevice(
        workspace_id=workspace.id,
        name="Phone 42",
        device_type="iphone",
        model="iPhone",
        serial_number="SN-42",
    )
    source = Episode(
        episode_uid="lineage-source",
        workspace_id=workspace.id,
        task_set_id=project.id,
        batch_id=batch.id,
        kind="source",
        modality="ego",
        quality_status="passed",
        review_status="accepted",
        metadata_json={"metrics": {"duration_s": 30.0}},
    )
    db_session.add_all([collector, device, source])
    db_session.flush()
    accepted = Episode(
        episode_uid="lineage-derived-accepted",
        workspace_id=workspace.id,
        task_set_id=project.id,
        batch_id=batch.id,
        kind="derived",
        parent_episode_id=source.id,
        derivation_version=1,
        modality="ego",
        quality_status="passed",
        annotation_status="accepted",
        review_status="accepted",
        workflow_status="published",
        source_start_ns=0,
        source_end_ns=10_000_000_000,
        metadata_json={"metrics": {"duration_s": 10.0}},
    )
    pending = Episode(
        episode_uid="lineage-derived-pending",
        workspace_id=workspace.id,
        task_set_id=project.id,
        batch_id=batch.id,
        kind="derived",
        parent_episode_id=source.id,
        derivation_version=1,
        modality="ego",
        quality_status="passed",
        annotation_status="submitted",
        review_status="pending",
        source_start_ns=10_000_000_000,
        source_end_ns=20_000_000_000,
        metadata_json={"metrics": {"duration_s": 10.0}},
    )
    db_session.add_all([accepted, pending])
    db_session.flush()
    annotation = WorkItem(
        workspace_id=workspace.id,
        episode_id=accepted.id,
        kind="annotation",
        status="accepted",
        created_by_user_id=actor.id,
        generation=1,
        version=1,
    )
    db_session.add_all(
        [
            annotation,
            EpisodeCollectorAttribution(
                root_source_episode_id=source.id,
                collector_profile_id=collector.id,
                source="offline_declared",
                created_by_user_id=actor.id,
            ),
            EpisodeDeviceAttribution(
                root_source_episode_id=source.id,
                collection_device_id=device.id,
                source="offline_declared",
                created_by_user_id=actor.id,
            ),
            JobRun(
                id=uuid4().hex,
                kind="episode_publish",
                resource_type="episode",
                resource_id=str(accepted.id),
                workspace_id=workspace.id,
                task_set_id=project.id,
                idempotency_key=f"lineage-publication-{accepted.id}",
                queue="publish",
                status="succeeded",
                phase="published",
                detail_json={"storage_uri": "oss://private/publication"},
                result_json={"checksum_sha256": "a" * 64},
            ),
        ]
    )
    db_session.commit()

    response = client.get(
        "/api/v1/episodes",
        headers=admin_headers,
        params={
            "workspace_id": workspace.id,
            "task_set_id": project.id,
            "kind": "derived",
            "review_status": "accepted",
            "publication_status": "published",
        },
    )

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert [item["episode_uid"] for item in items] == ["lineage-derived-accepted"]
    assert items[0]["parent_episode_id"] == source.id
    assert items[0]["source_start_ns"] == "0"
    assert items[0]["source_end_ns"] == "10000000000"
    assert items[0]["human_work"]["primary"] == {
        "id": annotation.id,
        "kind": "annotation",
        "status": "accepted",
    }
    assert items[0]["publication"]["category"] == "published"
    assert items[0]["attribution"]["collector"]["collector"]["name"] == "Ava"
    assert items[0]["attribution"]["collector"]["collector"]["profile_key"] == collector.profile_key
    assert items[0]["attribution"]["device"]["device"]["serial_number"] == "SN-42"
    assert "storage_uri" not in response.text
    assert "checksum" not in response.text


def test_episode_list_rejects_unknown_asset_filter_values(client, admin_headers, db_session):
    _actor, workspace, project = _scoped_project(db_session)
    db_session.commit()

    response = client.get(
        "/api/v1/episodes",
        headers=admin_headers,
        params={
            "workspace_id": workspace.id,
            "task_set_id": project.id,
            "kind": "archive",
        },
    )

    assert response.status_code == 422


def test_episode_list_does_not_inherit_attribution_across_project_boundaries(
    client, admin_headers, db_session
):
    actor, workspace, project = _scoped_project(db_session)
    other_project = TaskSet(workspace_id=workspace.id, name="Other lineage project")
    collector = make_collector(db_session, workspace_id=workspace.id, name="Private Collector")
    db_session.add_all([other_project, collector])
    db_session.flush()
    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="Visible lineage batch",
        batch_type="ego",
        actor_id=actor.id,
    )
    other_batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=other_project.id,
        name="Private lineage batch",
        batch_type="ego",
        actor_id=actor.id,
    )
    foreign_source = Episode(
        episode_uid="foreign-lineage-source",
        workspace_id=workspace.id,
        task_set_id=other_project.id,
        batch_id=other_batch.id,
        kind="source",
        modality="ego",
    )
    db_session.add(foreign_source)
    db_session.flush()
    child = Episode(
        episode_uid="cross-project-derived",
        workspace_id=workspace.id,
        task_set_id=project.id,
        batch_id=batch.id,
        kind="derived",
        parent_episode_id=foreign_source.id,
        derivation_version=1,
        source_start_ns=0,
        source_end_ns=1_000_000_000,
        modality="ego",
    )
    db_session.add_all(
        [
            child,
            EpisodeCollectorAttribution(
                root_source_episode_id=foreign_source.id,
                collector_profile_id=collector.id,
                source="offline_curated",
                created_by_user_id=actor.id,
            ),
        ]
    )
    db_session.commit()

    response = client.get(
        "/api/v1/episodes",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "task_set_id": project.id},
    )

    assert response.status_code == 200
    item = next(row for row in response.json()["data"]["items"] if row["id"] == child.id)
    assert item["attribution"]["collector"]["collector"] is None
    assert "Private Collector" not in response.text


def test_episode_and_queue_return_a_controlled_task_label_summary(
    client, admin_headers, db_session
):
    from data.services.work_queue_projection import create_episode_work_item

    actor, workspace, project = _scoped_project(db_session)
    task_label = _task_label(db_session, workspace_id=workspace.id, suffix="projection")
    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="task label projection batch",
        batch_type="ego",
        actor_id=actor.id,
    )
    episode = Episode(
        workspace_id=workspace.id,
        task_set_id=project.id,
        batch_id=batch.id,
        episode_uid="task-label-projection-source",
        kind="source",
        modality="ego",
        quality_status="passed",
        task_label_id=task_label.id,
    )
    db_session.add(episode)
    db_session.flush()
    create_episode_work_item(db_session, episode_id=episode.id, kind="cut", actor_id=actor.id)
    db_session.commit()

    expected = {"id": task_label.id, "key": task_label.key, "name": task_label.name}
    episode_response = client.get(f"/api/v1/episodes/{episode.id}", headers=admin_headers)
    queue_response = client.get(
        "/api/v1/work-queue",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "stage": "cut"},
    )

    assert episode_response.status_code == 200
    assert episode_response.json()["data"]["task_label"] == expected
    assert queue_response.status_code == 200
    assert queue_response.json()["data"]["items"][0]["episode"]["task_label"] == expected


def test_task_label_endpoints_return_controlled_vocabulary(client, admin_headers):
    created = client.post(
        "/api/v1/task-labels",
        headers=admin_headers,
        json={
            "key": "close_box_api",
            "name": "close the box",
            "description": "controlled import default",
        },
    )

    assert created.status_code == 200
    item = created.json()["data"]
    assert item == {
        "id": item["id"],
        "key": "close_box_api",
        "name": "close the box",
        "description": "controlled import default",
    }

    listed = client.get("/api/v1/task-labels", headers=admin_headers)
    assert listed.status_code == 200
    assert any(label["id"] == item["id"] for label in listed.json()["data"]["items"])


def test_episode_readers_can_list_controlled_task_labels(client, admin_headers, viewer_headers):
    created = client.post(
        "/api/v1/task-labels",
        headers=admin_headers,
        json={"key": "visible-to-reader", "name": "visible to reader"},
    )
    assert created.status_code == 200

    listed = client.get("/api/v1/task-labels", headers=viewer_headers)

    assert listed.status_code == 200
    assert any(
        label["id"] == created.json()["data"]["id"] for label in listed.json()["data"]["items"]
    )
def test_old_task_endpoint_is_not_registered(client, admin_headers):
    response = client.get("/api/v1/task/list", headers=admin_headers)

    assert response.status_code == 404


def test_legacy_generic_file_download_is_not_registered(client, admin_headers):
    response = client.get("/api/v1/files/download?path=raw/v1/unowned.mcap", headers=admin_headers)

    assert response.status_code == 404


def test_legacy_runtime_config_endpoint_is_not_registered(client, admin_headers):
    response = client.get("/api/v1/config", headers=admin_headers)

    assert response.status_code == 404


def test_episode_preview_endpoint_returns_authorized_descriptor_without_storage_uri(
    client, admin_headers, db_session, monkeypatch
):
    actor, workspace, project = _scoped_project(db_session)
    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="preview batch",
        batch_type="ego",
        actor_id=actor.id,
    )
    episode = Episode(
        workspace_id=workspace.id,
        task_set_id=project.id,
        batch_id=batch.id,
        episode_uid="preview-episode",
        kind="source",
        modality="ego",
        quality_status="passed",
    )
    db_session.add(episode)
    db_session.flush()
    db_session.add(
        EpisodeArtifact(
            episode_id=episode.id,
            artifact_type="process_preview",
            storage_role="process",
            storage_uri="oss://process-bucket/process/v1/preview-episode/preview.mp4",
            checksum_sha256="a" * 64,
            size_bytes=12,
            metadata_json={"media_type": "video/mp4"},
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        "data.routers.episodes.issue_episode_preview_url",
        lambda *_args, **_kwargs: type(
            "Access",
            (),
            {
                "as_payload": lambda self: {
                    "direct": True,
                    "url": "https://browser.example/short",
                    "media_type": "video/mp4",
                    "expires_at": "2099-01-01T00:00:00+00:00",
                }
            },
        )(),
    )

    response = client.get(f"/api/v1/episodes/{episode.id}/preview-url", headers=admin_headers)

    assert response.status_code == 200
    assert response.json()["data"] == {
        "available": True,
        "direct": True,
        "url": "https://browser.example/short",
        "media_type": "video/mp4",
        "expires_at": "2099-01-01T00:00:00+00:00",
    }
    assert "storage_uri" not in response.text


def test_episode_preview_uses_signed_same_origin_range_fallback_when_oss_direct_is_unavailable(
    client, admin_headers, db_session, tmp_storage
):
    from data.infra import oss_client

    actor, workspace, project = _scoped_project(db_session)
    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="local preview batch",
        batch_type="ego",
        actor_id=actor.id,
    )
    episode = Episode(
        workspace_id=workspace.id,
        task_set_id=project.id,
        batch_id=batch.id,
        episode_uid="local-preview-episode",
        kind="source",
        modality="ego",
        quality_status="passed",
    )
    db_session.add(episode)
    db_session.flush()
    bucket = oss_client.bucket_name("process")
    key = f"process/v1/workspaces/{workspace.id}/episodes/{episode.id}/preview.mp4"
    preview_path = tmp_storage / "cloud" / bucket / key
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.write_bytes(b"0123456789")
    db_session.add(
        EpisodeArtifact(
            episode_id=episode.id,
            artifact_type="process_preview",
            storage_role="process",
            storage_uri=f"oss://{bucket}/{key}",
            checksum_sha256="b" * 64,
            size_bytes=10,
            metadata_json={"media_type": "video/mp4"},
        )
    )
    db_session.commit()

    descriptor = client.get(f"/api/v1/episodes/{episode.id}/preview-url", headers=admin_headers)

    assert descriptor.status_code == 200
    payload = descriptor.json()["data"]
    assert payload["available"] is True
    assert payload["direct"] is False
    assert payload["url"].startswith(f"/api/v1/episodes/{episode.id}/preview/media?sig=")
    assert "storage_uri" not in descriptor.text
    assert "oss://" not in descriptor.text

    media = client.get(payload["url"], headers={"Range": "bytes=2-5"})

    assert media.status_code == 206
    assert media.content == b"2345"
    assert media.headers["accept-ranges"] == "bytes"
    assert media.headers["content-range"] == "bytes 2-5/10"

    wrong_episode = client.get(payload["url"].replace(f"/{episode.id}/", f"/{episode.id + 1}/", 1))

    assert wrong_episode.status_code == 401
