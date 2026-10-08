"""Authorized raw source file delivery without exposing storage locations."""

from __future__ import annotations

from uuid import uuid4

from data.database import (
    ArtifactOperation,
    Batch,
    Episode,
    EpisodeArtifact,
    TaskSet,
    User,
    Workspace,
)
from data.services.batch_paths import raw_episode_source_prefix
from data.services.episode_raw_source_access import RawSourceAccess


def create_batch(
    db_session,
    *,
    workspace_id: int,
    task_set_id: int,
    name: str,
    batch_type: str,
    actor_id: int | None,
) -> Batch:
    """Insert a collection batch row; the public batch API is retired."""

    batch = Batch(
        workspace_id=workspace_id,
        task_set_id=task_set_id,
        name=name,
        batch_type=batch_type,
        created_by_user_id=actor_id,
    )
    db_session.add(batch)
    db_session.flush()
    return batch


def _source_episode(db_session, tmp_storage):
    actor = db_session.query(User).order_by(User.id).first()
    suffix = uuid4().hex
    workspace = Workspace(name=f"raw artifact workspace {suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    task_set = TaskSet(workspace_id=workspace.id, name=f"raw artifact task set {suffix}")
    db_session.add(task_set)
    db_session.flush()
    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name="raw artifact batch",
        batch_type="ego",
        actor_id=actor.id,
    )
    episode = Episode(
        episode_uid=f"raw-artifact-{suffix}",
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=batch.id,
        kind="source",
        modality="ego",
        quality_status="failed",
        metadata_json={"source": {"data_file": "data.mcap"}},
    )
    db_session.add(episode)
    db_session.flush()
    artifact = EpisodeArtifact(
        episode_id=episode.id,
        artifact_type="raw_source",
        storage_role="raw",
        storage_uri="nas://pending",
        size_bytes=17,
        retention_policy="permanent",
    )
    db_session.add(artifact)
    db_session.flush()
    prefix = raw_episode_source_prefix(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=batch.id,
        episode_id=episode.id,
        artifact_id=artifact.id,
    )
    artifact.storage_uri = f"nas://{prefix}"
    source_dir = tmp_storage / prefix
    source_dir.mkdir(parents=True)
    (source_dir / "data.mcap").write_bytes(b"raw-mcap")
    (source_dir / "metadata.json").write_text('{"data_file":"data.mcap"}', encoding="utf-8")
    (source_dir / "complete.json").write_text('{"complete":true}', encoding="utf-8")
    db_session.commit()
    return workspace, episode, artifact


def test_failed_source_raw_files_are_authorized_and_streamed(
    client, admin_headers, db_session, tmp_storage
):
    _workspace, episode, artifact = _source_episode(db_session, tmp_storage)

    descriptor = client.get(
        f"/api/v1/episodes/{episode.id}/raw-source/downloads",
        headers=admin_headers,
    )

    assert descriptor.status_code == 200
    payload = descriptor.json()["data"]
    assert payload["available"] is True
    assert payload["artifact_id"] == artifact.id
    assert [item["name"] for item in payload["files"]] == [
        "data.mcap",
        "metadata.json",
        "complete.json",
    ]
    assert all(item["direct"] is False and item["available"] is True for item in payload["files"])
    assert "nas://" not in descriptor.text
    assert str(tmp_storage) not in descriptor.text

    data_file = payload["files"][0]
    streamed = client.get(data_file["url"], headers={"Accept-Encoding": "gzip"})

    assert streamed.status_code == 200
    assert streamed.content == b"raw-mcap"
    assert "attachment" in streamed.headers["content-disposition"]
    assert "content-encoding" not in streamed.headers


def test_cloud_raw_source_downloads_use_published_manifest_entry_sizes(
    client,
    admin_headers,
    db_session,
    tmp_storage,
    monkeypatch,
):
    _workspace, episode, artifact = _source_episode(db_session, tmp_storage)
    prefix = artifact.storage_uri.removeprefix("nas://")
    artifact.storage_uri = f"oss://raw-bucket/{prefix}"
    db_session.add(
        ArtifactOperation(
            id=str(uuid4()),
            artifact_id=artifact.id,
            operation_kind="raw_source_publish",
            status="published",
            target_uri=artifact.storage_uri,
            checksum_sha256="a" * 64,
            size_bytes=126_000_270,
            manifest_json={
                "kind": "directory",
                "entries": [
                    {"path": "data.mcap", "size": 126_000_000},
                    {"path": "metadata.json", "size": 180},
                    {"path": "complete.json", "size": 90},
                ],
            },
        )
    )
    db_session.commit()

    import data.routers.episodes as episode_router
    from data.infra import oss_client

    monkeypatch.setattr(oss_client, "bucket_name", lambda _role: "raw-bucket")
    monkeypatch.setattr(
        episode_router,
        "issue_raw_source_file_access",
        lambda **_kwargs: RawSourceAccess(
            url="https://delivery.example/raw-source",
            expires_at="2026-08-28T00:00:00+00:00",
            media_type="application/octet-stream",
            direct=True,
        ),
    )

    response = client.get(
        f"/api/v1/episodes/{episode.id}/raw-source/downloads",
        headers=admin_headers,
    )

    assert response.status_code == 200
    files = {item["name"]: item for item in response.json()["data"]["files"]}
    assert files["data.mcap"]["size_bytes"] == 126_000_000
    assert files["metadata.json"]["size_bytes"] == 180
    assert files["complete.json"]["size_bytes"] == 90
    assert "oss://" not in response.text


def test_cloud_raw_source_downloads_leave_untrusted_manifest_sizes_unknown(
    client,
    admin_headers,
    db_session,
    tmp_storage,
    monkeypatch,
):
    _workspace, episode, artifact = _source_episode(db_session, tmp_storage)
    prefix = artifact.storage_uri.removeprefix("nas://")
    artifact.storage_uri = f"oss://raw-bucket/{prefix}"
    db_session.add(
        ArtifactOperation(
            id=str(uuid4()),
            artifact_id=artifact.id,
            operation_kind="raw_source_publish",
            status="published",
            target_uri=artifact.storage_uri,
            checksum_sha256="a" * 64,
            size_bytes=126_000_270,
            manifest_json={
                "kind": "directory",
                "entries": [
                    {"path": "data.mcap", "size": 126_000_000},
                    {"path": "metadata.json", "size": "180"},
                    {"path": "complete.json", "size": 90},
                ],
            },
        )
    )
    db_session.commit()

    import data.routers.episodes as episode_router
    from data.infra import oss_client

    monkeypatch.setattr(oss_client, "bucket_name", lambda _role: "raw-bucket")
    monkeypatch.setattr(
        episode_router,
        "issue_raw_source_file_access",
        lambda **_kwargs: RawSourceAccess(
            url="https://delivery.example/raw-source",
            expires_at="2026-08-28T00:00:00+00:00",
            media_type="application/octet-stream",
            direct=True,
        ),
    )

    response = client.get(
        f"/api/v1/episodes/{episode.id}/raw-source/downloads",
        headers=admin_headers,
    )

    assert response.status_code == 200
    files = {item["name"]: item for item in response.json()["data"]["files"]}
    assert files["data.mcap"]["size_bytes"] == 126_000_000
    assert files["metadata.json"]["size_bytes"] is None
    assert files["complete.json"]["size_bytes"] == 90


def test_raw_source_download_rejects_unregistered_or_escaped_locations(
    client, admin_headers, db_session, tmp_storage
):
    _workspace, episode, artifact = _source_episode(db_session, tmp_storage)
    artifact.storage_uri = (
        "nas://raw/v2/workspaces/999/task-sets/999/batches/999/episodes/999/source/999"
    )
    db_session.commit()

    response = client.get(
        f"/api/v1/episodes/{episode.id}/raw-source/downloads",
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"available": False, "artifact_id": artifact.id, "files": []}
    assert "nas://" not in response.text


def test_raw_source_download_rejects_symlinked_file_escape(
    client, admin_headers, db_session, tmp_storage
):
    _workspace, episode, artifact = _source_episode(db_session, tmp_storage)
    source_file = tmp_storage / artifact.storage_uri.removeprefix("nas://") / "data.mcap"
    escaped_target = tmp_storage / f"outside-{uuid4().hex}.mcap"
    escaped_target.write_bytes(b"outside raw package")
    source_file.unlink()
    source_file.symlink_to(escaped_target)

    response = client.get(
        f"/api/v1/episodes/{episode.id}/raw-source/downloads",
        headers=admin_headers,
    )

    assert response.status_code == 200
    files = {item["name"]: item for item in response.json()["data"]["files"]}
    assert files["data.mcap"]["available"] is False
    assert files["data.mcap"]["url"] == ""


def test_raw_source_download_requires_episode_workspace_access(
    client, viewer_headers, db_session, tmp_storage
):
    _workspace, episode, _artifact = _source_episode(db_session, tmp_storage)

    response = client.get(
        f"/api/v1/episodes/{episode.id}/raw-source/downloads",
        headers=viewer_headers,
    )

    assert response.status_code == 403
