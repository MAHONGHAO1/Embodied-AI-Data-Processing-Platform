from uuid import uuid4

from data.database import (
    Batch,
    BatchLog,
    Episode,
    EpisodeArtifact,
    ImportSession,
    JobRun,
    TaskSet,
    User,
    WorkItem,
    Workspace,
)


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


def _observability_context(db_session):
    suffix = uuid4().hex
    actor = db_session.query(User).order_by(User.id).first()
    workspace = Workspace(name=f"observability workspace {suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    project = TaskSet(workspace_id=workspace.id, name=f"observability project {suffix}")
    db_session.add(project)
    db_session.flush()
    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="observable batch",
        batch_type="ego",
        actor_id=actor.id,
    )
    source = Episode(
        episode_uid=f"obs-source-{suffix}",
        workspace_id=workspace.id,
        task_set_id=project.id,
        batch_id=batch.id,
        kind="source",
        modality="ego",
        quality_status="passed",
    )
    db_session.add(source)
    db_session.flush()
    derived = Episode(
        episode_uid=f"obs-derived-{suffix}",
        workspace_id=workspace.id,
        task_set_id=project.id,
        batch_id=batch.id,
        parent_episode_id=source.id,
        kind="derived",
        derivation_version=1,
        modality="ego",
        source_start_ns=1_000_000_000,
        source_end_ns=3_000_000_000,
        quality_status="passed",
    )
    db_session.add(derived)
    db_session.flush()
    import_session = ImportSession(
        id=str(uuid4()),
        batch_id=batch.id,
        import_type="chunked_upload",
        status="succeeded",
        original_name="capture.zip",
        result_json={"storage_uri": "oss://private/import-result"},
    )
    artifact = EpisodeArtifact(
        episode_id=derived.id,
        artifact_type="process_preview",
        storage_role="process",
        storage_uri=f"oss://private/process/{suffix}.mp4",
        checksum_sha256="a" * 64,
        manifest_hash="b" * 64,
        size_bytes=1024,
        metadata_json={"storage_uri": "oss://private/nested", "frame_timestamps_ns": [1, 2]},
    )
    annotation = WorkItem(
        workspace_id=workspace.id,
        episode_id=derived.id,
        kind="annotation",
        status="accepted",
        created_by_user_id=actor.id,
        assignee_user_id=actor.id,
        generation=1,
        version=1,
    )
    db_session.add(annotation)
    db_session.flush()
    completed = WorkItem(
        workspace_id=workspace.id,
        episode_id=derived.id,
        kind="review",
        review_target_kind="annotation",
        review_of_work_item_id=annotation.id,
        status="accepted",
        created_by_user_id=actor.id,
        assignee_user_id=actor.id,
        generation=1,
        version=1,
    )
    publication = JobRun(
        id=uuid4().hex,
        kind="episode_publish",
        resource_type="episode",
        resource_id=str(derived.id),
        workspace_id=workspace.id,
        task_set_id=project.id,
        idempotency_key=f"publication-observability-{suffix}",
        queue="publish",
        status="succeeded",
        phase="published",
        detail_json={"storage_uri": "oss://private/publish-job"},
        result_json={"checksum_sha256": "c" * 64},
    )
    import_job = JobRun(
        id=uuid4().hex,
        kind="import_parse",
        resource_type="import_session",
        resource_id=import_session.id,
        workspace_id=workspace.id,
        task_set_id=project.id,
        idempotency_key=f"import-observability-{suffix}",
        queue="ingest",
        status="succeeded",
        phase="parsed",
        detail_json={"storage_uri": "oss://private/parse-job"},
    )
    db_session.add_all(
        (
            import_session,
            artifact,
            annotation,
            completed,
            publication,
            import_job,
            BatchLog(
                batch_id=batch.id, action="import.completed", to_status="ready", actor_id=actor.id
            ),
        )
    )
    db_session.commit()
    return workspace, batch, derived, artifact, completed


def test_episode_assets_are_scoped_and_redacted(client, admin_headers, db_session):
    _workspace, _batch, episode, artifact, _completed = _observability_context(db_session)

    assets = client.get(f"/api/v1/episodes/{episode.id}/assets", headers=admin_headers)

    assert assets.status_code == 200
    assert assets.json()["data"]["items"] == [
        {
            "id": artifact.id,
            "artifact_type": "process_preview",
            "storage_role": "process",
            "size_bytes": 1024,
            "retention_policy": "permanent",
            "retention_until": None,
        }
    ]
    assert assets.json()["data"]["packet_metadata"] == {}
    rendered = assets.text
    assert "storage_uri" not in rendered
    assert "oss://" not in rendered
    assert "checksum_sha256" not in rendered
    assert "metadata_json" not in rendered


def test_episode_assets_project_safe_packet_metadata(client, admin_headers, db_session):
    _workspace, _batch, episode, _artifact, _completed = _observability_context(db_session)
    episode.metadata_json = {
        "source": {
            "qrdf_version": "0.2.0",
            "episode_id": "episode_000001",
            "data_file": "data.mcap",
            "task": {"name": "pick_box", "language": "pick up the box"},
            "robot": {
                "name": "agibot_a2",
                "type": "single_arm",
                "num_arms": 1,
                "base_frame": "base_link",
            },
            "capture": {
                "mode": "teleop",
                "episode_type": "robot_teleop",
                "app": "qrdf_sdk",
                "app_version": "0.2.0",
            },
            "secret_path": "/etc/passwd",
        },
        "timing": {
            "start_timestamp_ns": "1000000000",
            "end_timestamp_ns": "6000000000",
            "duration_s": 5.0,
        },
        "metrics": {
            "reference_frame_count": 150,
            "duration_s": 5.0,
            "average_rgb_rate_hz": 30.0,
        },
        "reference_topic": "/camera/front/rgb",
        "multimodal": {
            "streams": [
                {
                    "id": "/camera/front/rgb",
                    "kind": "camera",
                    "width": 640,
                    "height": 480,
                    "fps": 30.0,
                }
            ],
            "timeseries": [
                {"id": "/observation/eef_state", "kind": "lowdim", "frequency_hz": 50.0}
            ],
        },
    }
    db_session.commit()

    assets = client.get(f"/api/v1/episodes/{episode.id}/assets", headers=admin_headers)
    detail = client.get(f"/api/v1/episodes/{episode.id}", headers=admin_headers)

    assert assets.status_code == 200
    assert detail.status_code == 200
    packet = assets.json()["data"]["packet_metadata"]
    assert packet == detail.json()["data"]["packet_metadata"]
    assert packet["qrdf_version"] == "0.2.0"
    assert packet["episode_id"] == "episode_000001"
    assert packet["task"] == {"name": "pick_box", "language": "pick up the box"}
    assert packet["robot"]["name"] == "agibot_a2"
    assert packet["capture"]["mode"] == "teleop"
    assert packet["reference_topic"] == "/camera/front/rgb"
    assert packet["cameras"] == [
        {"topic": "/camera/front/rgb", "width": 640, "height": 480, "fps": 30.0}
    ]
    assert packet["sensors"] == [{"topic": "/observation/eef_state", "frequency_hz": 50.0}]
    assert "secret_path" not in packet
    assert "storage_uri" not in assets.text
    assert "/etc/passwd" not in assets.text
    assert "/etc/passwd" not in detail.text


def test_completed_queue_includes_safe_publication_summary(client, admin_headers, db_session):
    workspace, _batch, _episode, _artifact, completed = _observability_context(db_session)

    response = client.get(
        "/api/v1/work-queue",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "stage": "completed"},
    )

    assert response.status_code == 200
    row = next(
        item for item in response.json()["data"]["items"] if item["work_item"]["id"] == completed.id
    )
    assert row["publication"] == {
        "status": "succeeded",
        "job_id": row["publication"]["job_id"],
        "retry_allowed": False,
    }
    assert "storage_uri" not in response.text


def test_assets_default_deny_a_nonmember(client, operator_headers, db_session):
    _workspace, _batch, episode, _artifact, _completed = _observability_context(db_session)

    assets = client.get(f"/api/v1/episodes/{episode.id}/assets", headers=operator_headers)

    assert assets.status_code == 403
