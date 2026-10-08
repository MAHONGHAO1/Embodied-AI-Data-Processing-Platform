"""Fetch manifests hand external tools signed URLs instead of copying data."""

from decimal import Decimal
from uuid import uuid4


def _seed(db_session, monkeypatch):
    from data.config import settings
    from data.database import Episode, EpisodeArtifact, Workspace, WorkspaceMember
    from data.infra import oss_client
    from data.models.collection_core import CollectionProject, CollectionTask
    from data.models.data_package import DataPackage

    monkeypatch.setattr(settings, "oss_browser_direct_enabled", True, raising=False)
    monkeypatch.setattr(
        settings, "storage_browser_endpoint", "http://127.0.0.1:19000", raising=False
    )
    monkeypatch.setattr(settings, "oss_browser_url_ttl_seconds", 900, raising=False)

    suffix = uuid4().hex[:8]
    workspace = Workspace(name=f"Fetch manifest workspace {suffix}")
    db_session.add(workspace)
    db_session.flush()
    project = CollectionProject(workspace_id=workspace.id, name=f"Fetch project {suffix}")
    db_session.add(project)
    db_session.flush()
    task = CollectionTask(
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name=f"Fetch task {suffix}",
        target_duration_hours=Decimal("2.00"),
        default_package_duration_hours=Decimal("2.00"),
    )
    db_session.add(task)
    db_session.flush()
    package = DataPackage(
        package_uid=f"pkg-fetch-{suffix}",
        workspace_id=workspace.id,
        collection_project_id=project.id,
        collection_task_id=task.id,
        status="pending_assignment",
        target_duration_hours=Decimal("2.00"),
    )
    db_session.add(package)
    db_session.flush()
    episode = Episode(
        episode_uid=f"EGO-FETCH-{suffix}",
        workspace_id=workspace.id,
        data_package_id=package.id,
        kind="source",
        modality="ego",
        workflow_status="ready",
    )
    db_session.add(episode)
    db_session.flush()
    bucket = oss_client.bucket_name("raw")
    artifact = EpisodeArtifact(
        episode_id=episode.id,
        artifact_type="raw_source",
        storage_role="raw",
        storage_uri=f"oss://{bucket}/raw/v1/{suffix}/data.mcap",
        checksum_sha256="a" * 64,
        size_bytes=4096,
        metadata_json={"media_type": "application/octet-stream", "etag": "frozen-etag"},
    )
    db_session.add(artifact)
    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=1))
    db_session.commit()
    return workspace, package, episode


def test_fetch_manifest_returns_object_entries(client, db_session, admin_headers, monkeypatch):
    workspace, package, episode = _seed(db_session, monkeypatch)

    response = client.post(
        "/api/v1/fetch-manifests",
        json={"workspace_id": workspace.id, "scope": "data_packages", "ids": [package.id]},
        headers=admin_headers,
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["objects"], "expected one object entry"
    entry = payload["objects"][0]
    assert entry["episode_uid"] == episode.episode_uid
    assert entry["key"].startswith("oss://")
    assert entry["size"] == 4096
    assert entry["sha256"] == "a" * 64
    assert entry["episode"]["data_package_id"] == package.id
    # Local MinIO plus a loopback browser endpoint cannot issue public direct
    # URLs, and the manifest must say so instead of faking a signed link.
    if entry["available"]:
        assert entry["url"].startswith(("http://", "https://"))
        assert entry["expires_at"]
    else:
        assert entry["url"] == ""
        assert entry["expires_at"] == ""


def test_fetch_manifest_rejects_other_workspaces(
    client, db_session, annotator_headers, monkeypatch
):
    workspace, package, _ = _seed(db_session, monkeypatch)

    response = client.post(
        "/api/v1/fetch-manifests",
        json={
            "workspace_id": workspace.id + 999,
            "scope": "data_packages",
            "ids": [package.id],
        },
        headers=annotator_headers,
    )

    assert response.status_code in (403, 404)


def test_fetch_freezes_members_and_rejects_foreign_work(
    client, db_session, admin_headers, monkeypatch
):
    from data.database import Episode
    from data.models.annotation_work import AnnotationWorkItem
    from data.models.data_batch import DataBatch, DataBatchEpisode
    from data.models.episode_admission import EpisodeAdmissionFact

    workspace, package, first = _seed(db_session, monkeypatch)
    extra = Episode(
        episode_uid=f"excluded-{uuid4().hex}",
        workspace_id=workspace.id,
        data_package_id=package.id,
        kind="source",
        modality="ego",
    )
    db_session.add(extra)
    batch = DataBatch(workspace_id=workspace.id, name=f"frozen-{uuid4().hex}")
    db_session.add(batch)
    db_session.flush()
    db_session.add(
        DataBatchEpisode(
            data_batch_id=batch.id,
            data_package_id=package.id,
            episode_id=first.id,
            admission_attempt=1,
            duration_hours=1,
        )
    )
    db_session.add(
        EpisodeAdmissionFact(
            episode_id=first.id,
            attempt=1,
            objects_json=[
                {
                    "path": "data.mcap",
                    "role": "raw",
                    "kind": "data",
                    "ref": {
                        "bucket_role": "raw",
                        "object_key": "raw/frozen.mcap",
                        "etag": "abc",
                        "version_id": None,
                        "size_bytes": 40,
                        "sha256": "a" * 64,
                    },
                },
                {
                    "path": "metadata.json",
                    "role": "process",
                    "kind": "metadata",
                    "ref": {
                        "bucket_role": "process",
                        "object_key": "process/metadata.json",
                        "etag": "def",
                        "version_id": None,
                        "size_bytes": 20,
                        "sha256": "b" * 64,
                    },
                },
            ],
        )
    )
    work = AnnotationWorkItem(
        workspace_id=workspace.id,
        data_batch_id=batch.id,
        data_package_id=package.id,
        assignee_user_id=1,
        episode_members_json=[{"episode_id": first.id, "admission_attempt": 1}],
    )
    db_session.add(work)
    db_session.commit()
    for scope, id_ in [("data_batch", batch.id), ("my_annotation_work_items", work.id)]:
        response = client.post(
            "/api/v1/fetch-manifests",
            json={"workspace_id": workspace.id, "scope": scope, "ids": [id_]},
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert len(data["objects"]) == 2
        assert {o["episode"]["id"] for o in data["objects"]} == {first.id}
        assert data["oss_network"] == "internal"
        assert all("X-Amz-Expires=3600" in o["url"] for o in data["objects"])
    work.assignee_user_id = 2
    db_session.commit()
    response = client.post(
        "/api/v1/fetch-manifests",
        json={"workspace_id": workspace.id, "scope": "my_annotation_work_items", "ids": [work.id]},
        headers=admin_headers,
    )
    assert response.status_code == 403


def test_fetch_network_ttl_and_missing_ids(client, db_session, admin_headers, monkeypatch):
    from data.config import settings

    workspace, package, _ = _seed(db_session, monkeypatch)
    body = {"workspace_id": workspace.id, "scope": "data_packages", "ids": [package.id]}
    for extra in [
        {"include": ["raw"]},
        {"url_ttl_seconds": 0},
        {"url_ttl_seconds": 604801},
        {"url_ttl_seconds": True},
        {"oss_network": "anything"},
    ]:
        assert (
            client.post(
                "/api/v1/fetch-manifests", json={**body, **extra}, headers=admin_headers
            ).status_code
            == 422
        )
    monkeypatch.setattr(settings, "storage_browser_endpoint", "")
    monkeypatch.setattr(settings, "oss_browser_endpoint", "")
    assert (
        client.post(
            "/api/v1/fetch-manifests", json={**body, "oss_network": "public"}, headers=admin_headers
        ).status_code
        == 503
    )
    monkeypatch.setattr(settings, "storage_browser_endpoint", "https://public.example.test")
    response = client.post(
        "/api/v1/fetch-manifests",
        json={**body, "oss_network": "public", "url_ttl_seconds": 120},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["objects"][0]["url"].startswith("https://public.example.test/")
    assert "X-Amz-Expires=120" in response.json()["data"]["objects"][0]["url"]
    assert (
        client.post(
            "/api/v1/fetch-manifests",
            json={**body, "ids": [package.id, 999999999]},
            headers=admin_headers,
        ).status_code
        == 404
    )


def test_fetch_manifest_lists_every_episode_object_with_its_path(
    client, db_session, admin_headers, monkeypatch
):
    from tests.test_episode_objects import verified_entries

    from data.services.episode_admission import record_episode_admission_fact

    workspace, package, episode = _seed(db_session, monkeypatch)
    record_episode_admission_fact(
        db_session,
        episode_id=episode.id,
        attempt=1,
        source_fingerprint=episode.source_fingerprint or "",
        validation_policy_version="v1",
        integrity_status="passed",
        preview_status="ready",
        output_verification_status="verified",
        report_ref={"ok": True},
        objects=verified_entries(),
    )
    db_session.commit()

    response = client.post(
        "/api/v1/fetch-manifests",
        json={"workspace_id": workspace.id, "scope": "data_packages", "ids": [package.id]},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    objects = response.json()["data"]["objects"]
    assert sorted(item["path"] for item in objects) == sorted(e["path"] for e in verified_entries())
    assert all(item["mime"] != "application/x-tar" for item in objects)


def test_fetch_manifest_historical_fallback_rejects_duplicate_relative_path(
    client, db_session, admin_headers, monkeypatch
):
    """Legacy episodes with no admission objects fall back to raw artifacts; two
    artifacts resolving to the same relative path must fail closed instead of
    silently overwriting one another when materialized."""
    from data.database import EpisodeArtifact
    from data.infra import oss_client

    workspace, package, episode = _seed(db_session, monkeypatch)
    bucket = oss_client.bucket_name("raw")
    db_session.add(
        EpisodeArtifact(
            episode_id=episode.id,
            artifact_type="raw_source",
            storage_role="raw",
            storage_uri=f"oss://{bucket}/raw/v1/other/data.mcap",
            checksum_sha256="b" * 64,
            size_bytes=2048,
            metadata_json={
                "media_type": "application/octet-stream",
                "etag": "second-etag",
                "relative_path": "data.mcap",
            },
        )
    )
    db_session.commit()

    response = client.post(
        "/api/v1/fetch-manifests",
        json={"workspace_id": workspace.id, "scope": "data_packages", "ids": [package.id]},
        headers=admin_headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "episode_object_path_duplicate"


def test_fetch_manifest_historical_fallback_rejects_unsafe_relative_path(
    client, db_session, admin_headers, monkeypatch
):
    workspace, package, episode = _seed(db_session, monkeypatch)
    from data.database import EpisodeArtifact

    artifact = db_session.query(EpisodeArtifact).filter_by(episode_id=episode.id).one()
    artifact.metadata_json = {
        **(artifact.metadata_json or {}),
        "relative_path": "../escape.mcap",
    }
    db_session.commit()

    response = client.post(
        "/api/v1/fetch-manifests",
        json={"workspace_id": workspace.id, "scope": "data_packages", "ids": [package.id]},
        headers=admin_headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "episode_object_path_unsafe"
