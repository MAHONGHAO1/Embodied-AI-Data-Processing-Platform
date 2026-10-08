from __future__ import annotations

from uuid import uuid4

import pytest
from collector_fixtures import make_collector
from sqlalchemy.exc import IntegrityError

import data.database as database
from data.database import (
    Batch,
    CollectionDevice,
    Episode,
    EpisodeAnnotation,
    EpisodeArtifact,
    EpisodeCollectorAttribution,
    EpisodeDeviceAttribution,
    EpisodeReview,
    JobRun,
    RealtimeEvent,
    TaskLabel,
    TaskSet,
    User,
    WorkItem,
    Workspace,
    WorkspaceMember,
)
from data.services.episode_workbench import create_derived_episodes_from_cut_draft


def _source_context(db_session):
    suffix = uuid4().hex
    workspace = Workspace(name=f"workbench workspace {suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    project = TaskSet(workspace_id=workspace.id, name=f"workbench project {suffix}")
    db_session.add(project)
    db_session.flush()
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=project.id,
        name=f"workbench batch {suffix}",
        batch_type="ego",
    )
    db_session.add(batch)
    db_session.flush()
    source = Episode(
        workspace_id=workspace.id,
        task_set_id=project.id,
        batch_id=batch.id,
        episode_uid=f"workbench-source-{suffix}",
        kind="source",
        modality="ego",
        quality_status="passed",
        source_fingerprint=uuid4().hex + uuid4().hex[:32],
    )
    db_session.add(source)
    db_session.flush()
    return workspace, project, source


def _grant_admin_workspace_access(db_session, workspace: Workspace) -> None:
    actor = db_session.query(User).filter(User.email == "admin@quicdata.com").one()
    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=actor.id))
    db_session.commit()


def _claimed_cut_context(db_session):
    workspace, project, source = _source_context(db_session)
    source.metadata_json = {
        "timing": {
            "start_timestamp_ns": "1000000000",
            "end_timestamp_ns": "3000000000",
        },
        "metrics": {
            "reference_frame_count": 60,
            "duration_s": 2.0,
            "average_rgb_rate_hz": 30.0,
        },
    }
    actor = db_session.query(User).filter(User.email == "admin@quicdata.com").one()
    item = WorkItem(
        workspace_id=workspace.id,
        episode_id=source.id,
        kind="cut",
        status="in_progress",
        assignee_user_id=actor.id,
        created_by_user_id=actor.id,
    )
    db_session.add(item)
    db_session.commit()
    return actor, workspace, project, source, item


def _claimed_annotation_context(db_session):
    actor, workspace, project, source, _cut_item = _claimed_cut_context(db_session)
    derived = Episode(
        workspace_id=workspace.id,
        task_set_id=project.id,
        batch_id=source.batch_id,
        episode_uid=f"workbench-derived-{uuid4().hex}",
        kind="derived",
        parent_episode_id=source.id,
        derivation_version=1,
        modality=source.modality,
        source_fingerprint=source.source_fingerprint,
        source_start_ns=1_100_000_000,
        source_end_ns=1_300_000_000,
        quality_status="passed",
    )
    db_session.add(derived)
    db_session.flush()
    item = WorkItem(
        workspace_id=workspace.id,
        episode_id=derived.id,
        kind="annotation",
        status="in_progress",
        assignee_user_id=actor.id,
        created_by_user_id=actor.id,
    )
    db_session.add(item)
    db_session.commit()
    return actor, workspace, project, source, derived, item


def _accept_cut_draft(
    client, admin_headers, db_session, *, item: WorkItem, payload: dict[str, object]
):
    payload = {**payload, "note": payload.get("note", "submitted cut")}
    saved = client.put(
        f"/api/v1/work-queue/items/{item.id}/draft",
        json={"base_version": 0, "payload": payload},
        headers=admin_headers,
    )
    assert saved.status_code == 200
    submitted = client.post(
        f"/api/v1/work-queue/items/{item.id}/submit",
        json={"base_version": 1},
        headers=admin_headers,
    )
    assert submitted.status_code == 200
    review = db_session.query(WorkItem).filter(WorkItem.review_of_work_item_id == item.id).one()
    assert (
        client.post(
            f"/api/v1/work-queue/items/{review.id}/claim", headers=admin_headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/work-queue/items/{review.id}/continue", headers=admin_headers
        ).status_code
        == 200
    )
    accepted = client.post(
        f"/api/v1/work-queue/items/{review.id}/review",
        json={"decision": "accepted"},
        headers=admin_headers,
    )
    assert accepted.status_code == 200
    return review, accepted


def test_work_item_schema_persists_versioned_draft_and_review_link(db_session):
    assert hasattr(WorkItem, "draft_json")
    assert hasattr(WorkItem, "draft_version")
    assert hasattr(WorkItem, "review_target_kind")
    assert hasattr(WorkItem, "review_of_work_item_id")

    workspace, _project, source = _source_context(db_session)
    cut = WorkItem(workspace_id=workspace.id, episode_id=source.id, kind="cut", status="pending")
    db_session.add(cut)
    db_session.commit()

    assert cut.draft_json == {}
    assert cut.draft_version == 0
    assert cut.review_target_kind is None
    assert cut.review_of_work_item_id is None


def test_draft_save_response_keeps_queue_row_and_returns_realtime_version(
    client, admin_headers, db_session
):
    _actor, _workspace, _project, _source, item = _claimed_cut_context(db_session)

    response = client.put(
        f"/api/v1/work-queue/items/{item.id}/draft",
        headers=admin_headers,
        json={
            "base_version": 0,
            "payload": {
                "mode": "partitioned",
                "segments": [
                    {
                        "id": "segment-1",
                        "start_ns": "1000000000",
                        "end_ns": "3000000000",
                        "eligibility": "included",
                    }
                ],
            },
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["work_item"]["work_item"]["id"] == item.id
    assert isinstance(data["work_item_realtime_version"], int)
    assert data["work_item_realtime_version"] >= 1


def test_task_set_source_import_ledger_is_unique_per_task_set_and_source(db_session):
    ledger_model = getattr(database, "TaskSetSourceImport", None)
    assert ledger_model is not None

    _workspace, project, source = _source_context(db_session)
    first = ledger_model(
        task_set_id=project.id,
        source_fingerprint=source.source_fingerprint,
        status="discovered",
    )
    db_session.add(first)
    db_session.commit()

    duplicate = ledger_model(
        task_set_id=project.id,
        source_fingerprint=source.source_fingerprint,
        status="discovered",
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_collector_attribution_is_append_only_and_root_source_scoped(db_session):
    profile_model = getattr(database, "PersonnelProfile", None)
    attribution_model = getattr(database, "EpisodeCollectorAttribution", None)
    assert profile_model is not None
    assert attribution_model is not None

    workspace, _project, source = _source_context(db_session)
    profile = make_collector(
        db_session,
        workspace_id=workspace.id,
        name="Collector One",
        is_active=True,
    )
    db_session.add(profile)
    db_session.flush()
    first = attribution_model(
        root_source_episode_id=source.id,
        collector_profile_id=profile.id,
        source="offline_curated",
        created_by_user_id=None,
    )
    second = attribution_model(
        root_source_episode_id=source.id,
        collector_profile_id=None,
        source="offline_curated",
        created_by_user_id=None,
    )
    db_session.add_all((first, second))
    db_session.commit()

    rows = (
        db_session.query(attribution_model)
        .filter(attribution_model.root_source_episode_id == source.id)
        .order_by(attribution_model.id)
        .all()
    )
    assert [row.collector_profile_id for row in rows] == [profile.id, None]


def test_online_verified_device_has_priority_over_later_offline_evidence(db_session):
    from data.services.episode_workbench import effective_device_attribution

    workspace, _project, source = _source_context(db_session)
    verified = CollectionDevice(
        workspace_id=workspace.id,
        name="Verified phone",
        device_type="phone",
        serial_number=f"VERIFIED-{uuid4().hex[:8]}",
    )
    reported = CollectionDevice(
        workspace_id=workspace.id,
        name="Reported phone",
        device_type="phone",
        serial_number=f"REPORTED-{uuid4().hex[:8]}",
    )
    db_session.add_all((verified, reported))
    db_session.flush()
    db_session.add_all(
        (
            EpisodeDeviceAttribution(
                root_source_episode_id=source.id,
                collection_device_id=verified.id,
                source="online_verified",
            ),
            EpisodeDeviceAttribution(
                root_source_episode_id=source.id,
                collection_device_id=reported.id,
                source="offline_curated",
            ),
        )
    )
    db_session.commit()

    effective = effective_device_attribution(db_session, episode=source)

    assert effective["device"]["id"] == verified.id
    assert effective["effective_source"] == "online_verified"
    assert effective["editable"] is False


def test_claimed_cut_workbench_saves_a_versioned_safe_draft(client, admin_headers, db_session):
    _actor, _workspace, _project, source, item = _claimed_cut_context(db_session)

    workbench = client.get(
        f"/api/v1/episodes/{source.id}/workbench",
        params={"work_item_id": item.id},
        headers=admin_headers,
    )

    assert workbench.status_code == 200
    snapshot = workbench.json()["data"]
    assert snapshot["timeline"] == {
        "start_ns": "1000000000",
        "end_ns": "3000000000",
        "duration_s": 2.0,
        "reference_topic": None,
        "event_tracks": [],
        "boundary_suggestions": [],
    }
    assert snapshot["draft"] == {"kind": "cut", "version": 0, "payload": {}}
    assert snapshot["episode"]["workspace_id"] == _workspace.id
    assert snapshot["episode"]["task_set_id"] == _project.id
    assert "storage_uri" not in workbench.text

    payload = {
        "base_version": 0,
        "payload": {
            "mode": "whole",
            "segments": [
                {
                    "id": "cut-a",
                    "start_ns": "1000000000",
                    "end_ns": "3000000000",
                    "eligibility": "included",
                }
            ],
        },
    }
    saved = client.put(
        f"/api/v1/work-queue/items/{item.id}/draft",
        json=payload,
        headers=admin_headers,
    )
    assert saved.status_code == 200
    assert saved.json()["data"]["draft"] == {
        "kind": "cut",
        "version": 1,
        "payload": payload["payload"],
    }

    conflict = client.put(
        f"/api/v1/work-queue/items/{item.id}/draft",
        json=payload,
        headers=admin_headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "draft_conflict"


def test_cut_workbench_exposes_only_safe_bounded_boundary_suggestions(
    client, admin_headers, db_session
):
    _actor, _workspace, _project, source, item = _claimed_cut_context(db_session)
    source.metadata_json = {
        **source.metadata_json,
        "cut_suggestions": {
            "schema": "quicdata.cut-suggestions.v1",
            "items": [
                {
                    "timestamp_ns": "2000000000",
                    "segment_id_hint": "qr-1",
                    "segment_index": 1,
                    "protocol_version": 1,
                    "collector_id": "must-not-leak",
                    "raw_message": {"secret": "must-not-leak"},
                },
                {
                    "timestamp_ns": "9999999999",
                    "segment_id_hint": "out-of-range",
                    "segment_index": 2,
                    "protocol_version": 1,
                },
            ],
        },
    }
    db_session.commit()

    response = client.get(
        f"/api/v1/episodes/{source.id}/workbench",
        params={"work_item_id": item.id},
        headers=admin_headers,
    )

    assert response.status_code == 200
    suggestions = response.json()["data"]["timeline"]["boundary_suggestions"]
    assert suggestions == [
        {
            "timestamp_ns": "2000000000",
            "segment_id_hint": "qr-1",
            "segment_index": 1,
            "protocol_version": 1,
        }
    ]
    serialized_suggestions = str(suggestions)
    assert "collector_id" not in serialized_suggestions
    assert "raw_message" not in serialized_suggestions


def test_cut_workbench_exposes_a_signed_local_range_preview(
    client, admin_headers, db_session, tmp_storage
):
    from data.infra import oss_client

    _actor, workspace, _project, source, item = _claimed_cut_context(db_session)
    bucket = oss_client.bucket_name("process")
    key = f"process/v1/workspaces/{workspace.id}/episodes/{source.id}/preview.mp4"
    preview_path = tmp_storage / "cloud" / bucket / key
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.write_bytes(b"preview-bytes")
    db_session.add(
        EpisodeArtifact(
            episode_id=source.id,
            artifact_type="process_preview",
            storage_role="process",
            storage_uri=f"oss://{bucket}/{key}",
            checksum_sha256="c" * 64,
            size_bytes=len(b"preview-bytes"),
            metadata_json={
                "media_type": "video/mp4",
                "encoded_fps": 15.0,
                "frame_timestamps_ns": ["1000000000", "1100000000", "2900000000"],
            },
        )
    )
    db_session.commit()

    response = client.get(
        f"/api/v1/episodes/{source.id}/workbench",
        params={"work_item_id": item.id},
        headers=admin_headers,
    )

    assert response.status_code == 200
    preview = response.json()["data"]["media"]["preview"]
    assert preview["available"] is True
    assert preview["direct"] is False
    assert preview["url"].startswith(f"/api/v1/episodes/{source.id}/preview/media?sig=")
    assert preview["playback_timeline"] == {
        "encoded_fps": 15.0,
        "frame_timestamps_ns": ["1000000000", "1100000000", "2900000000"],
    }
    assert "storage_uri" not in response.text

    media = client.get(preview["url"], headers={"Range": "bytes=0-6"})

    assert media.status_code == 206
    assert media.content == b"preview"


def test_review_workbench_exposes_the_exact_submitted_draft_without_storage_details(
    client, admin_headers, db_session
):
    _actor, _workspace, _project, source, item = _claimed_cut_context(db_session)
    submitted_payload = {
        "mode": "whole",
        "segments": [
            {
                "id": "cut-review-target",
                "start_ns": "1000000000",
                "end_ns": "3000000000",
                "eligibility": "included",
            }
        ],
        "note": "review this interval",
    }
    assert (
        client.put(
            f"/api/v1/work-queue/items/{item.id}/draft",
            json={"base_version": 0, "payload": submitted_payload},
            headers=admin_headers,
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/work-queue/items/{item.id}/submit",
            json={"base_version": 1},
            headers=admin_headers,
        ).status_code
        == 200
    )
    review_item = (
        db_session.query(WorkItem).filter(WorkItem.review_of_work_item_id == item.id).one()
    )
    assert (
        client.post(
            f"/api/v1/work-queue/items/{review_item.id}/claim", headers=admin_headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/work-queue/items/{review_item.id}/continue", headers=admin_headers
        ).status_code
        == 200
    )

    response = client.get(
        f"/api/v1/episodes/{source.id}/workbench",
        params={"work_item_id": review_item.id},
        headers=admin_headers,
    )

    assert response.status_code == 200
    snapshot = response.json()["data"]
    assert snapshot["review_target"] == {
        "kind": "cut",
        "work_item_id": item.id,
        "draft_version": 1,
        "payload": submitted_payload,
    }
    assert "storage_uri" not in response.text
    assert "oss://" not in response.text


def test_accepted_cut_review_creates_derived_episodes_once_without_raw_duplication(
    client, admin_headers, db_session
):
    _actor, workspace, _project, source, item = _claimed_cut_context(db_session)
    source.metadata_json = {
        **source.metadata_json,
        "reference_topic": "/camera/head/rgb",
        "multimodal": {
            "streams": [
                {
                    "id": "/camera/head/rgb",
                    "kind": "camera",
                    "width": 960,
                    "height": 720,
                    "fps": 30.0,
                }
            ],
            "timeseries": [{"id": "/sensor/head/imu", "kind": "lowdim", "frequency_hz": 50.0}],
        },
    }
    label = TaskLabel(key=f"carry-box-{uuid4().hex}", name="Carry box")
    source.task_label_id = None
    db_session.add(label)
    db_session.flush()
    source.task_label_id = label.id
    db_session.add(
        EpisodeArtifact(
            episode_id=source.id,
            artifact_type="raw_source",
            storage_role="raw",
            storage_uri=f"local://raw/{uuid4().hex}",
            retention_policy="permanent",
        )
    )
    db_session.commit()
    payload = {
        "base_version": 0,
        "payload": {
            "mode": "partitioned",
            "segments": [
                {
                    "id": "cut-a",
                    "start_ns": "1000000000",
                    "end_ns": "2000000000",
                    "eligibility": "included",
                    "boundary_after": {"origin": "human"},
                },
                {
                    "id": "cut-b",
                    "start_ns": "2000000000",
                    "end_ns": "3000000000",
                    "eligibility": "included",
                },
            ],
            "note": "publish this whole episode",
        },
    }
    assert (
        client.put(
            f"/api/v1/work-queue/items/{item.id}/draft", json=payload, headers=admin_headers
        ).status_code
        == 200
    )
    submitted = client.post(
        f"/api/v1/work-queue/items/{item.id}/submit",
        json={"base_version": 1},
        headers=admin_headers,
    )
    assert submitted.status_code == 200
    review = db_session.query(WorkItem).filter(WorkItem.review_of_work_item_id == item.id).one()
    assert review.kind == "review"
    assert review.review_target_kind == "cut"

    assert (
        client.post(
            f"/api/v1/work-queue/items/{review.id}/claim", headers=admin_headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/work-queue/items/{review.id}/continue", headers=admin_headers
        ).status_code
        == 200
    )
    event_ids_before_review = {
        event_id for (event_id,) in db_session.query(RealtimeEvent.event_id).all()
    }
    accepted = client.post(
        f"/api/v1/work-queue/items/{review.id}/review",
        json={"decision": "accepted"},
        headers=admin_headers,
    )
    assert accepted.status_code == 200
    review_events = (
        db_session.query(RealtimeEvent)
        .filter(RealtimeEvent.event_id.notin_(event_ids_before_review))
        .order_by(RealtimeEvent.created_at, RealtimeEvent.event_id)
        .all()
    )
    jobs = (
        db_session.query(JobRun)
        .filter(
            JobRun.kind == "derived_preview_batch",
            JobRun.resource_type == "episode",
            JobRun.resource_id == str(source.id),
        )
        .all()
    )
    assert len(jobs) == 1
    assert jobs[0].queue == "media"
    assert jobs[0].detail_json == {
        "source_episode_id": source.id,
        "cut_review_work_item_id": review.id,
    }
    children = db_session.query(Episode).filter(Episode.parent_episode_id == source.id).all()
    child_ids = {str(child.id) for child in children}
    assert not [
        event
        for event in review_events
        if event.resource_type == "episode" and event.resource_id in child_ids
    ]
    range_events = [
        event for event in review_events if event.event_name == "work_queue.invalidated"
    ]
    assert len(range_events) == 1
    assert range_events[0].safe_payload == {
        "workspace_id": workspace.id,
        "source_episode_id": source.id,
        "affected_episode_count": len(children),
        "reason": "derived_episodes_created",
    }
    assert (
        db_session.query(JobRun)
        .filter(
            JobRun.kind == "episode_preview",
            JobRun.resource_type == "episode",
            JobRun.resource_id.in_([str(source.id), *[str(child.id) for child in children]]),
        )
        .count()
        == 0
    )
    job_count = db_session.query(JobRun).count()
    realtime_event_count = db_session.query(RealtimeEvent).count()
    retry = client.post(
        f"/api/v1/work-queue/items/{review.id}/review",
        json={"decision": "accepted"},
        headers=admin_headers,
    )
    assert retry.status_code == 200
    assert db_session.query(JobRun).count() == job_count
    assert db_session.query(RealtimeEvent).count() == realtime_event_count

    assert [(child.source_start_ns, child.source_end_ns) for child in children] == [
        (1_000_000_000, 2_000_000_000),
        (2_000_000_000, 3_000_000_000),
    ]
    assert children[0].task_label_id == label.id
    assert children[0].artifacts == []
    assert children[0].metadata_json["reference_topic"] == "/camera/head/rgb"
    assert children[0].metadata_json["multimodal"] == {
        "streams": [
            {"id": "/camera/head/rgb", "kind": "camera", "width": 960, "height": 720, "fps": 30.0}
        ],
        "timeseries": [{"id": "/sensor/head/imu", "kind": "lowdim", "frequency_hz": 50.0}],
    }
    assert (
        db_session.query(WorkItem)
        .filter(WorkItem.episode_id == children[0].id, WorkItem.kind == "annotation")
        .count()
        == 1
    )


def test_cut_derivation_flush_count_is_bounded_for_many_segments(db_session, monkeypatch):
    actor, workspace, _project, source, cut_item = _claimed_cut_context(db_session)
    segment_count = 200
    interval_ns = 10_000_000
    cut_item.draft_json = {
        "mode": "partitioned",
        "segments": [
            {
                "id": f"segment-{index}",
                "start_ns": str(1_000_000_000 + index * interval_ns),
                "end_ns": str(1_000_000_000 + (index + 1) * interval_ns),
                "eligibility": "included",
            }
            for index in range(segment_count)
        ],
        "note": "batch derivation",
    }
    review_item = WorkItem(
        workspace_id=workspace.id,
        episode_id=source.id,
        kind="review",
        review_target_kind="cut",
        review_of_work_item_id=cut_item.id,
        status="in_progress",
        assignee_user_id=actor.id,
        created_by_user_id=actor.id,
    )
    db_session.add(review_item)
    db_session.commit()

    original_flush = db_session.flush
    flush_calls = 0

    def tracked_flush(*args, **kwargs):
        nonlocal flush_calls
        flush_calls += 1
        return original_flush(*args, **kwargs)

    monkeypatch.setattr(db_session, "flush", tracked_flush)
    children = create_derived_episodes_from_cut_draft(
        db_session,
        source=source,
        cut_item=cut_item,
        review_item=review_item,
        actor_id=actor.id,
    )

    assert len(children) == segment_count
    assert all(child.id is not None for child in children)
    assert flush_calls <= 2
    assert len([item for item in db_session.new if isinstance(item, WorkItem)]) == segment_count


def test_accepted_cut_review_materializes_only_included_segments(client, admin_headers, db_session):
    _actor, _workspace, _project, source, item = _claimed_cut_context(db_session)
    review, _accepted = _accept_cut_draft(
        client,
        admin_headers,
        db_session,
        item=item,
        payload={
            "mode": "partitioned",
            "segments": [
                {
                    "id": "included-a",
                    "start_ns": "1000000000",
                    "end_ns": "1500000000",
                    "eligibility": "included",
                    "boundary_after": {"origin": "human"},
                },
                {
                    "id": "excluded",
                    "start_ns": "1500000000",
                    "end_ns": "2500000000",
                    "eligibility": "excluded",
                    "exclusion_reason": "off_task",
                    "boundary_after": {"origin": "human"},
                },
                {
                    "id": "included-b",
                    "start_ns": "2500000000",
                    "end_ns": "3000000000",
                    "eligibility": "included",
                },
            ],
        },
    )

    children = (
        db_session.query(Episode)
        .filter(Episode.parent_episode_id == source.id)
        .order_by(Episode.source_start_ns)
        .all()
    )
    assert [(child.source_start_ns, child.source_end_ns) for child in children] == [
        (1_000_000_000, 1_500_000_000),
        (2_500_000_000, 3_000_000_000),
    ]
    assert (
        db_session.query(WorkItem)
        .filter(
            WorkItem.episode_id.in_([child.id for child in children]),
            WorkItem.kind == "annotation",
        )
        .count()
        == 2
    )
    assert all(child.artifacts == [] for child in children)

    retry = client.post(
        f"/api/v1/work-queue/items/{review.id}/review",
        json={"decision": "accepted"},
        headers=admin_headers,
    )
    assert retry.status_code == 200
    assert db_session.query(Episode).filter(Episode.parent_episode_id == source.id).count() == 2


def test_accepted_all_excluded_cut_plan_creates_no_children_and_is_idempotent(
    client, admin_headers, db_session
):
    _actor, _workspace, _project, source, item = _claimed_cut_context(db_session)
    review, _accepted = _accept_cut_draft(
        client,
        admin_headers,
        db_session,
        item=item,
        payload={
            "mode": "whole",
            "segments": [
                {
                    "id": "all-excluded",
                    "start_ns": "1000000000",
                    "end_ns": "3000000000",
                    "eligibility": "excluded",
                    "exclusion_reason": "off_task",
                }
            ],
        },
    )

    db_session.refresh(source)
    assert source.workflow_status == "excluded"
    assert db_session.query(Episode).filter(Episode.parent_episode_id == source.id).count() == 0
    assert (
        db_session.query(JobRun)
        .filter(JobRun.resource_type == "episode", JobRun.resource_id == str(source.id))
        .count()
        == 0
    )

    retry = client.post(
        f"/api/v1/work-queue/items/{review.id}/review",
        json={"decision": "accepted"},
        headers=admin_headers,
    )
    assert retry.status_code == 200
    assert db_session.query(Episode).filter(Episode.parent_episode_id == source.id).count() == 0


@pytest.mark.parametrize(
    ("payload", "expected_detail"),
    [
        (
            {
                "mode": "whole",
                "segments": [
                    {"id": "wrong-whole", "start_ns": "1000000000", "end_ns": "2000000000"}
                ],
            },
            "cut_whole_range_required",
        ),
        (
            {
                "mode": "partitioned",
                "segments": [
                    {"id": "gap-a", "start_ns": "1000000000", "end_ns": "1500000000"},
                    {"id": "gap-b", "start_ns": "1600000000", "end_ns": "3000000000"},
                ],
            },
            "cut_partition_must_cover_source",
        ),
        (
            {
                "mode": "partitioned",
                "segments": [{"id": "partial", "start_ns": "1100000000", "end_ns": "3000000000"}],
            },
            "cut_partition_must_cover_source",
        ),
    ],
)
def test_cut_draft_rejects_non_contiguous_or_partial_partitions(
    client, admin_headers, db_session, payload, expected_detail
):
    _actor, _workspace, _project, _source, item = _claimed_cut_context(db_session)

    response = client.put(
        f"/api/v1/work-queue/items/{item.id}/draft",
        json={"base_version": 0, "payload": payload},
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert response.json()["detail"] == expected_detail


def test_cut_draft_saves_overlong_included_window_but_rejects_submission(
    client, admin_headers, db_session
):
    _actor, _workspace, _project, source, item = _claimed_cut_context(db_session)
    source.metadata_json = {
        **source.metadata_json,
        "timing": {"start_timestamp_ns": "1000000000", "end_timestamp_ns": "302000000000"},
    }
    db_session.commit()

    saved = client.put(
        f"/api/v1/work-queue/items/{item.id}/draft",
        json={
            "base_version": 0,
            "payload": {
                "mode": "partitioned",
                "segments": [
                    {"id": "too-long", "start_ns": "1000000000", "end_ns": "302000000000"}
                ],
            },
        },
        headers=admin_headers,
    )

    assert saved.status_code == 200
    submitted = client.post(
        f"/api/v1/work-queue/items/{item.id}/submit",
        json={"base_version": 1},
        headers=admin_headers,
    )
    assert submitted.status_code == 422
    assert submitted.json()["detail"] == "cut_partition_window_too_long"


def test_cut_draft_normalizes_eligibility_exclusion_and_boundary_provenance(
    client, admin_headers, db_session
):
    _actor, _workspace, _project, _source, item = _claimed_cut_context(db_session)

    saved = client.put(
        f"/api/v1/work-queue/items/{item.id}/draft",
        json={
            "base_version": 0,
            "payload": {
                "mode": "partitioned",
                "segments": [
                    {
                        "id": "included",
                        "start_ns": "1000000000",
                        "end_ns": "2100000000",
                        "eligibility": "included",
                        "boundary_after": {
                            "origin": "qr_event",
                            "suggested_timestamp_ns": "2000000000",
                            "adjusted": False,
                            "segment_id_hint": "qr-1",
                            "segment_index": 1,
                            "protocol_version": 1,
                        },
                    },
                    {
                        "id": "excluded",
                        "start_ns": "2100000000",
                        "end_ns": "3000000000",
                        "eligibility": "excluded",
                        "exclusion_reason": "off_task",
                    },
                ],
            },
        },
        headers=admin_headers,
    )

    assert saved.status_code == 200
    segments = saved.json()["data"]["draft"]["payload"]["segments"]
    assert segments == [
        {
            "id": "included",
            "start_ns": "1000000000",
            "end_ns": "2100000000",
            "eligibility": "included",
            "boundary_after": {
                "origin": "qr_event",
                "suggested_timestamp_ns": "2000000000",
                "adjusted": True,
                "segment_id_hint": "qr-1",
                "segment_index": 1,
                "protocol_version": 1,
            },
        },
        {
            "id": "excluded",
            "start_ns": "2100000000",
            "end_ns": "3000000000",
            "eligibility": "excluded",
            "exclusion_reason": "off_task",
        },
    ]


def test_cut_draft_defaults_legacy_segments_to_included_and_human_boundaries(
    client, admin_headers, db_session
):
    _actor, _workspace, _project, _source, item = _claimed_cut_context(db_session)

    saved = client.put(
        f"/api/v1/work-queue/items/{item.id}/draft",
        json={
            "base_version": 0,
            "payload": {
                "mode": "partitioned",
                "segments": [
                    {"id": "legacy-a", "start_ns": "1000000000", "end_ns": "2000000000"},
                    {"id": "legacy-b", "start_ns": "2000000000", "end_ns": "3000000000"},
                ],
            },
        },
        headers=admin_headers,
    )

    assert saved.status_code == 200
    assert saved.json()["data"]["draft"]["payload"]["segments"] == [
        {
            "id": "legacy-a",
            "start_ns": "1000000000",
            "end_ns": "2000000000",
            "eligibility": "included",
            "boundary_after": {"origin": "human"},
        },
        {
            "id": "legacy-b",
            "start_ns": "2000000000",
            "end_ns": "3000000000",
            "eligibility": "included",
        },
    ]


def test_overlong_excluded_cut_window_can_be_submitted(client, admin_headers, db_session):
    _actor, _workspace, _project, source, item = _claimed_cut_context(db_session)
    source.metadata_json = {
        **source.metadata_json,
        "timing": {"start_timestamp_ns": "1000000000", "end_timestamp_ns": "302000000000"},
    }
    db_session.commit()

    saved = client.put(
        f"/api/v1/work-queue/items/{item.id}/draft",
        json={
            "base_version": 0,
            "payload": {
                "mode": "whole",
                "note": "exclude the idle interval",
                "segments": [
                    {
                        "id": "excluded-long",
                        "start_ns": "1000000000",
                        "end_ns": "302000000000",
                        "eligibility": "excluded",
                        "exclusion_reason": "idle_or_setup",
                    }
                ],
            },
        },
        headers=admin_headers,
    )
    assert saved.status_code == 200

    submitted = client.post(
        f"/api/v1/work-queue/items/{item.id}/submit",
        json={"base_version": 1},
        headers=admin_headers,
    )
    assert submitted.status_code == 200


def test_annotation_draft_can_save_empty_segments_but_cannot_submit(
    client, admin_headers, db_session
):
    _actor, _workspace, _project, _source, _derived, item = _claimed_annotation_context(db_session)
    payload = {"segments": [], "outcome": "unknown", "note": ""}

    saved = client.put(
        f"/api/v1/work-queue/items/{item.id}/draft",
        json={"base_version": 0, "payload": payload},
        headers=admin_headers,
    )

    assert saved.status_code == 200
    assert saved.json()["data"]["draft"]["payload"] == payload
    submitted = client.post(
        f"/api/v1/work-queue/items/{item.id}/submit",
        json={"base_version": 1},
        headers=admin_headers,
    )
    assert submitted.status_code == 422
    assert submitted.json()["detail"] == "annotation_segments_required"


def test_annotation_draft_can_save_blank_description_but_cannot_submit(
    client, admin_headers, db_session
):
    _actor, _workspace, _project, _source, _derived, item = _claimed_annotation_context(db_session)
    payload = {
        "segments": [
            {
                "id": "pending-description",
                "start_ns": "1110000000",
                "end_ns": "1150000000",
                "description": "",
            }
        ],
        "outcome": "unknown",
        "note": "ready to submit when content is complete",
    }

    saved = client.put(
        f"/api/v1/work-queue/items/{item.id}/draft",
        json={"base_version": 0, "payload": payload},
        headers=admin_headers,
    )

    assert saved.status_code == 200
    submitted = client.post(
        f"/api/v1/work-queue/items/{item.id}/submit",
        json={"base_version": 1},
        headers=admin_headers,
    )
    assert submitted.status_code == 422
    assert submitted.json()["detail"] == "annotation_description_required"


def test_annotation_draft_accepts_one_thousand_segments_but_rejects_more(
    client, admin_headers, db_session
):
    _actor, _workspace, _project, _source, _derived, item = _claimed_annotation_context(db_session)

    def payload_with_segments(count: int) -> dict[str, object]:
        return {
            "segments": [
                {
                    "id": f"dense-segment-{index}",
                    "start_ns": str(1_100_000_000 + index * 100_000),
                    "end_ns": str(1_100_000_001 + index * 100_000),
                    "description": "",
                }
                for index in range(count)
            ],
            "outcome": "unknown",
            "note": "",
        }

    accepted = client.put(
        f"/api/v1/work-queue/items/{item.id}/draft",
        json={"base_version": 0, "payload": payload_with_segments(1000)},
        headers=admin_headers,
    )

    assert accepted.status_code == 200
    rejected = client.put(
        f"/api/v1/work-queue/items/{item.id}/draft",
        json={"base_version": 1, "payload": payload_with_segments(1001)},
        headers=admin_headers,
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "draft_segments_invalid"


def test_cut_draft_can_submit_without_a_note_and_returns_realtime_version(
    client, admin_headers, db_session
):
    _actor, _workspace, _project, _source, item = _claimed_cut_context(db_session)
    payload = {
        "mode": "whole",
        "note": "",
        "segments": [
            {
                "id": "whole-cut",
                "start_ns": "1000000000",
                "end_ns": "3000000000",
                "eligibility": "included",
            }
        ],
    }

    saved = client.put(
        f"/api/v1/work-queue/items/{item.id}/draft",
        json={"base_version": 0, "payload": payload},
        headers=admin_headers,
    )

    assert saved.status_code == 200
    submitted = client.post(
        f"/api/v1/work-queue/items/{item.id}/submit",
        json={"base_version": 1},
        headers=admin_headers,
    )
    assert submitted.status_code == 200
    data = submitted.json()["data"]
    assert data["work_item"]["work_item"]["status"] == "submitted"
    assert isinstance(data["work_item_realtime_version"], int)
    assert data["work_item_realtime_version"] >= 1


def test_annotation_submit_requires_segment_content_but_not_a_note(
    client, admin_headers, db_session
):
    _actor, _workspace, _project, _source, _derived, item = _claimed_annotation_context(db_session)
    payload = {
        "segments": [
            {
                "id": "described-without-note",
                "start_ns": "1110000000",
                "end_ns": "1150000000",
                "description": "move the object onto the shelf",
            }
        ],
        "outcome": "success",
    }

    saved = client.put(
        f"/api/v1/work-queue/items/{item.id}/draft",
        json={"base_version": 0, "payload": payload},
        headers=admin_headers,
    )

    assert saved.status_code == 200
    submitted = client.post(
        f"/api/v1/work-queue/items/{item.id}/submit",
        json={"base_version": 1},
        headers=admin_headers,
    )
    assert submitted.status_code == 200


def test_review_accepts_legacy_submitted_cut_without_a_note(client, admin_headers, db_session):
    actor, workspace, _project, source, item = _claimed_cut_context(db_session)
    item.status = "submitted"
    item.draft_version = 1
    item.draft_json = {
        "mode": "whole",
        "segments": [
            {
                "id": "legacy-whole-cut",
                "start_ns": "1000000000",
                "end_ns": "3000000000",
                "eligibility": "included",
            }
        ],
    }
    review = WorkItem(
        workspace_id=workspace.id,
        episode_id=source.id,
        kind="review",
        review_target_kind="cut",
        review_of_work_item_id=item.id,
        status="in_progress",
        assignee_user_id=actor.id,
        created_by_user_id=actor.id,
    )
    db_session.add(review)
    db_session.commit()

    accepted = client.post(
        f"/api/v1/work-queue/items/{review.id}/review",
        json={"decision": "accepted", "note": "approved after inspection"},
        headers=admin_headers,
    )

    assert accepted.status_code == 200
    db_session.refresh(review)
    assert review.status == "accepted"


@pytest.mark.parametrize(
    "segment_patch",
    [
        {"eligibility": "maybe"},
        {"eligibility": "included", "exclusion_reason": "off_task"},
        {"eligibility": "excluded", "exclusion_reason": "unsupported"},
        {"eligibility": "included", "boundary_after": {"origin": "qr_event"}},
    ],
)
def test_cut_draft_rejects_invalid_eligibility_or_boundary_contract(
    client, admin_headers, db_session, segment_patch
):
    _actor, _workspace, _project, _source, item = _claimed_cut_context(db_session)
    first = {
        "id": "a",
        "start_ns": "1000000000",
        "end_ns": "2000000000",
        **segment_patch,
    }

    response = client.put(
        f"/api/v1/work-queue/items/{item.id}/draft",
        json={
            "base_version": 0,
            "payload": {
                "mode": "partitioned",
                "segments": [
                    first,
                    {"id": "b", "start_ns": "2000000000", "end_ns": "3000000000"},
                ],
            },
        },
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "draft_segments_invalid"


def test_annotation_review_rejects_retired_publication_atomically(
    client, admin_headers, db_session
):
    actor, workspace, _project, source, _cut = _claimed_cut_context(db_session)
    derived = Episode(
        workspace_id=workspace.id,
        task_set_id=source.task_set_id,
        batch_id=source.batch_id,
        episode_uid=f"workbench-derived-{uuid4().hex}",
        kind="derived",
        parent_episode_id=source.id,
        derivation_version=1,
        modality=source.modality,
        source_fingerprint=source.source_fingerprint,
        source_start_ns=1_100_000_000,
        source_end_ns=1_300_000_000,
        quality_status="passed",
    )
    collector = make_collector(
        db_session,
        workspace_id=workspace.id,
        name=f"Collector {uuid4().hex[:8]}",
    )
    device = CollectionDevice(
        workspace_id=workspace.id,
        name=f"Phone {uuid4().hex[:8]}",
        device_type="phone",
        serial_number=f"PHONE-{uuid4().hex[:8]}",
    )
    db_session.add_all((derived, collector, device))
    db_session.flush()
    annotation_item = WorkItem(
        workspace_id=workspace.id,
        episode_id=derived.id,
        kind="annotation",
        status="in_progress",
        assignee_user_id=actor.id,
        created_by_user_id=actor.id,
    )
    db_session.add(annotation_item)
    db_session.commit()
    annotation_payload = {
        "base_version": 0,
        "payload": {
            "segments": [
                {
                    "id": "describe-a",
                    "start_ns": "1110000000",
                    "end_ns": "1150000000",
                    "description": "pick up the box",
                },
                {
                    "id": "describe-b",
                    "start_ns": "1160000000",
                    "end_ns": "1250000000",
                    "description": "place the box on the table",
                },
            ],
            "outcome": "success",
            "note": "annotation completed",
            "collector_profile_id": collector.id,
            "collection_device_id": device.id,
        },
    }
    saved = client.put(
        f"/api/v1/work-queue/items/{annotation_item.id}/draft",
        json=annotation_payload,
        headers=admin_headers,
    )
    assert saved.status_code == 200
    assert (
        client.post(
            f"/api/v1/work-queue/items/{annotation_item.id}/submit",
            json={"base_version": 1},
            headers=admin_headers,
        ).status_code
        == 200
    )
    review = (
        db_session.query(WorkItem)
        .filter(WorkItem.review_of_work_item_id == annotation_item.id)
        .one()
    )
    assert review.review_target_kind == "annotation"
    assert (
        client.post(
            f"/api/v1/work-queue/items/{review.id}/claim", headers=admin_headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/work-queue/items/{review.id}/continue", headers=admin_headers
        ).status_code
        == 200
    )
    accepted = client.post(
        f"/api/v1/work-queue/items/{review.id}/review",
        json={
            "decision": "accepted",
            "collector_profile_id": None,
            "collection_device_id": None,
            "note": "capture attribution unavailable",
        },
        headers=admin_headers,
    )
    assert accepted.status_code == 200
    assert accepted.json()["data"]["work_item"]["work_item"]["status"] == "accepted"

    db_session.expire_all()
    # The annotation revision and accepted review are committed; retirement
    # removes only the legacy publication side effect.
    assert (
        db_session.query(EpisodeAnnotation)
        .filter(EpisodeAnnotation.work_item_id == annotation_item.id)
        .count()
        == 1
    )
    assert (
        db_session.query(EpisodeReview)
        .filter(EpisodeReview.review_work_item_id == review.id)
        .count()
        == 1
    )
    assert (
        db_session.query(EpisodeCollectorAttribution)
        .filter(EpisodeCollectorAttribution.root_source_episode_id == source.id)
        .count()
        == 1
    )
    assert (
        db_session.query(EpisodeDeviceAttribution)
        .filter(EpisodeDeviceAttribution.root_source_episode_id == source.id)
        .count()
        == 1
    )
    assert db_session.query(JobRun).filter(JobRun.resource_id == str(derived.id)).count() == 0


def test_rejected_annotation_review_is_terminal_and_resubmission_creates_next_generation(
    client, admin_headers, db_session
):
    _actor, workspace, _project, _source, derived, annotation_item = _claimed_annotation_context(
        db_session
    )
    payload = {
        "segments": [
            {
                "id": "describe-a",
                "start_ns": "1110000000",
                "end_ns": "1150000000",
                "description": "pick up the box",
            }
        ],
        "outcome": "success",
        "note": "first annotation attempt",
    }
    assert (
        client.put(
            f"/api/v1/work-queue/items/{annotation_item.id}/draft",
            json={"base_version": 0, "payload": payload},
            headers=admin_headers,
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/work-queue/items/{annotation_item.id}/submit",
            json={"base_version": 1},
            headers=admin_headers,
        ).status_code
        == 200
    )
    first_review = (
        db_session.query(WorkItem)
        .filter(WorkItem.review_of_work_item_id == annotation_item.id)
        .one()
    )
    assert first_review.generation == 1
    assert (
        client.post(
            f"/api/v1/work-queue/items/{first_review.id}/claim", headers=admin_headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/work-queue/items/{first_review.id}/continue", headers=admin_headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/work-queue/items/{first_review.id}/review",
            json={"decision": "rejected", "note": "description is incomplete"},
            headers=admin_headers,
        ).status_code
        == 200
    )

    review_page = client.get(
        "/api/v1/work-queue",
        params={"workspace_id": workspace.id, "stage": "review"},
        headers=admin_headers,
    )
    annotation_page = client.get(
        "/api/v1/work-queue",
        params={"workspace_id": workspace.id, "stage": "annotation"},
        headers=admin_headers,
    )
    assert review_page.status_code == 200
    assert first_review.id not in {
        row["work_item"]["id"] for row in review_page.json()["data"]["items"]
    }
    assert annotation_page.status_code == 200
    annotation_row = next(
        row
        for row in annotation_page.json()["data"]["items"]
        if row["work_item"]["id"] == annotation_item.id
    )
    assert annotation_row["work_item"]["status"] == "rejected"
    assert annotation_row["work_item"]["available_actions"] == ["claim"]
    old_review_claim = client.post(
        f"/api/v1/work-queue/items/{first_review.id}/claim",
        headers=admin_headers,
    )
    assert old_review_claim.status_code == 422

    assert (
        client.post(
            f"/api/v1/work-queue/items/{annotation_item.id}/claim",
            headers=admin_headers,
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/work-queue/items/{annotation_item.id}/continue",
            headers=admin_headers,
        ).status_code
        == 200
    )
    payload["segments"][0]["description"] = "pick up and place the box"
    payload["note"] = "second annotation attempt"
    assert (
        client.put(
            f"/api/v1/work-queue/items/{annotation_item.id}/draft",
            json={"base_version": 1, "payload": payload},
            headers=admin_headers,
        ).status_code
        == 200
    )
    first_submit = client.post(
        f"/api/v1/work-queue/items/{annotation_item.id}/submit",
        json={"base_version": 2},
        headers=admin_headers,
    )
    repeated_submit = client.post(
        f"/api/v1/work-queue/items/{annotation_item.id}/submit",
        json={"base_version": 2},
        headers=admin_headers,
    )
    assert first_submit.status_code == 200
    assert repeated_submit.status_code == 200

    db_session.expire_all()
    reviews = (
        db_session.query(WorkItem)
        .filter(WorkItem.review_of_work_item_id == annotation_item.id)
        .order_by(WorkItem.generation.asc())
        .all()
    )
    assert [(review.id, review.generation, review.status) for review in reviews] == [
        (first_review.id, 1, "rejected"),
        (reviews[1].id, 2, "pending"),
    ]
    assert reviews[1].id != first_review.id
    assert annotation_item.generation == 2


def test_collector_profile_options_are_workspace_scoped_and_only_active(
    client, admin_headers, db_session
):
    workspace, _project, _source = _source_context(db_session)
    _grant_admin_workspace_access(db_session, workspace)
    active = make_collector(
        db_session,
        workspace_id=workspace.id,
        name=f"Active {uuid4().hex[:8]}",
        is_active=True,
    )
    inactive = make_collector(
        db_session,
        workspace_id=workspace.id,
        name=f"Inactive {uuid4().hex[:8]}",
        is_active=False,
    )
    db_session.add_all((active, inactive))
    db_session.commit()

    response = client.get(
        "/api/v1/collector-profiles",
        params={"workspace_id": workspace.id},
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "items": [
            {
                "id": active.id,
                "name": active.name,
                "profile_key": active.profile_key,
                "display_label": active.name,
                "is_active": True,
            }
        ]
    }


def test_collector_profile_names_can_repeat_but_number_is_globally_unique(
    client, admin_headers, db_session
):
    workspace, _project, _source = _source_context(db_session)
    _grant_admin_workspace_access(db_session, workspace)

    first = client.post(
        "/api/v1/collector-profiles",
        json={"workspace_id": workspace.id, "name": "小王"},
        headers=admin_headers,
    )
    same_name = client.post(
        "/api/v1/collector-profiles",
        json={"workspace_id": workspace.id, "name": "小王"},
        headers=admin_headers,
    )
    other_workspace, _, _ = _source_context(db_session)
    db_session.commit()
    duplicate_number = client.post(
        "/api/v1/collector-profiles",
        json={
            "workspace_id": other_workspace.id,
            "name": "小李",
            "profile_key": first.json()["data"]["profile_key"],
        },
        headers=admin_headers,
    )

    assert first.status_code == 200
    assert first.json()["data"] == {
        "id": first.json()["data"]["id"],
        "name": "小王",
        "profile_key": first.json()["data"]["profile_key"],
        "display_label": "小王",
        "is_active": True,
    }
    assert same_name.status_code == 200
    assert same_name.json()["data"]["profile_key"] != first.json()["data"]["profile_key"]
    assert duplicate_number.status_code == 409


@pytest.mark.parametrize("invalid_key", ["0", "0042", "-1", "1.2", "１２", "abc"])
def test_collector_profile_rejects_noncanonical_number(
    client, admin_headers, db_session, invalid_key
):
    workspace, _project, _source = _source_context(db_session)
    _grant_admin_workspace_access(db_session, workspace)

    response = client.post(
        "/api/v1/collector-profiles",
        json={"workspace_id": workspace.id, "name": "小王", "profile_key": invalid_key},
        headers=admin_headers,
    )

    assert response.status_code == 422


def test_collector_profile_allocates_number_when_omitted(client, admin_headers, db_session):
    workspace, _project, _source = _source_context(db_session)
    _grant_admin_workspace_access(db_session, workspace)

    response = client.post(
        "/api/v1/collector-profiles",
        json={"workspace_id": workspace.id, "name": "自动编号"},
        headers=admin_headers,
    )

    assert response.status_code == 200
    profile = response.json()["data"]
    assert int(profile["profile_key"]) > 0
    assert profile["profile_key"].isascii()
    assert profile["profile_key"].isdecimal()
    assert profile["display_label"] == "自动编号"


def test_online_verified_collector_rejects_offline_draft_rewrite(client, admin_headers, db_session):
    actor, workspace, _project, source, _cut = _claimed_cut_context(db_session)
    profile = make_collector(
        db_session,
        workspace_id=workspace.id,
        name=f"Verified {uuid4().hex[:8]}",
    )
    db_session.add(profile)
    db_session.flush()
    db_session.add(
        EpisodeCollectorAttribution(
            root_source_episode_id=source.id,
            collector_profile_id=profile.id,
            source="online_verified",
            created_by_user_id=actor.id,
        )
    )
    derived = Episode(
        workspace_id=workspace.id,
        task_set_id=source.task_set_id,
        batch_id=source.batch_id,
        episode_uid=f"immutable-derived-{uuid4().hex}",
        kind="derived",
        parent_episode_id=source.id,
        derivation_version=1,
        modality=source.modality,
        source_fingerprint=source.source_fingerprint,
        source_start_ns=1_100_000_000,
        source_end_ns=1_300_000_000,
        quality_status="passed",
    )
    db_session.add(derived)
    db_session.flush()
    item = WorkItem(
        workspace_id=workspace.id,
        episode_id=derived.id,
        kind="annotation",
        status="in_progress",
        assignee_user_id=actor.id,
        created_by_user_id=actor.id,
    )
    db_session.add(item)
    db_session.commit()

    response = client.put(
        f"/api/v1/work-queue/items/{item.id}/draft",
        headers=admin_headers,
        json={
            "base_version": 0,
            "payload": {
                "segments": [
                    {
                        "id": "immutable-a",
                        "start_ns": "1110000000",
                        "end_ns": "1150000000",
                        "description": "inspect the object",
                    }
                ],
                "collector_profile_id": None,
            },
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "collector_attribution_immutable"


def test_episode_multimodal_projections_are_bounded_and_redacted(client, admin_headers, db_session):
    _actor, _workspace, _project, source, _item = _claimed_cut_context(db_session)
    source.metadata_json = {
        "timing": {
            "start_timestamp_ns": "1000000000",
            "end_timestamp_ns": "3000000000",
            "duration_s": 2.0,
        },
        "reference_topic": "/camera/head/rgb",
        "multimodal": {
            "streams": [
                {
                    "id": "/camera/head/rgb",
                    "kind": "camera",
                    "width": 960,
                    "height": 720,
                    "fps": 15.0,
                },
            ],
            "timeseries": [
                {"id": "/sensor/head/imu", "kind": "lowdim", "frequency_hz": 50.0},
            ],
        },
        "storage_uri": "oss://must-not-leak/internal",
    }
    db_session.add(source)
    db_session.commit()

    timeline = client.get(f"/api/v1/episodes/{source.id}/timeline", headers=admin_headers)
    multimodal = client.get(
        f"/api/v1/episodes/{source.id}/multimodal/session", headers=admin_headers
    )
    series = client.get(
        f"/api/v1/episodes/{source.id}/timeseries",
        params={"series_id": "/sensor/head/imu", "limit": 12},
        headers=admin_headers,
    )

    assert timeline.status_code == 200
    assert timeline.json()["data"] == {
        "start_ns": "1000000000",
        "end_ns": "3000000000",
        "duration_s": 2.0,
        "reference_topic": "/camera/head/rgb",
        "event_tracks": [],
    }
    assert multimodal.status_code == 200
    assert multimodal.json()["data"]["streams"] == [
        {"id": "/camera/head/rgb", "kind": "camera", "width": 960, "height": 720, "fps": 15.0}
    ]
    assert series.status_code == 200
    assert series.json()["data"] == {
        "available": False,
        "series_id": "/sensor/head/imu",
        "points": [],
    }
    assert "storage_uri" not in timeline.text
    assert "storage_uri" not in multimodal.text
    assert "storage_uri" not in series.text


def test_episode_ai_projection_is_safe_and_explicitly_disabled_without_provider(
    client, admin_headers, db_session
):
    _actor, workspace, _project, source, _item = _claimed_cut_context(db_session)
    derived = Episode(
        workspace_id=workspace.id,
        task_set_id=source.task_set_id,
        batch_id=source.batch_id,
        episode_uid=f"ai-derived-{uuid4().hex}",
        kind="derived",
        parent_episode_id=source.id,
        derivation_version=1,
        modality="ego",
        source_fingerprint=source.source_fingerprint,
        source_start_ns=1_100_000_000,
        source_end_ns=1_300_000_000,
        quality_status="passed",
        metadata_json={
            "metrics": {"reference_frame_count": 60, "duration_s": 0.2},
            "multimodal": {"streams": [{"id": "/camera/head/rgb", "kind": "camera", "fps": 30.0}]},
        },
    )
    db_session.add(derived)
    db_session.flush()
    db_session.add(
        EpisodeArtifact(
            episode_id=derived.id,
            artifact_type="process_preview",
            storage_role="process",
            storage_uri=f"oss://must-not-leak/process/v1/{uuid4().hex}.mp4",
            checksum_sha256="a" * 64,
            size_bytes=100,
        )
    )
    db_session.commit()

    response = client.get(f"/api/v1/episodes/{derived.id}/ai-suggestions", headers=admin_headers)

    assert response.status_code == 200
    assert response.json()["data"] == {
        "capability": {
            "enabled": False,
            "eligible": False,
            "disabled_reason": "ai_disabled",
            "rgb_topics": ["/camera/head/rgb"],
            "default_rgb_topic": "/camera/head/rgb",
        },
        "items": [],
    }
    assert "storage_uri" not in response.text


def test_episode_ai_projection_is_disabled_when_the_ai_worker_is_unavailable(
    client, admin_headers, db_session, monkeypatch
):
    from data.routers import episodes as episode_router
    from data.services import episode_multimodal

    _actor, workspace, _project, source, _item = _claimed_cut_context(db_session)
    derived = Episode(
        workspace_id=workspace.id,
        task_set_id=source.task_set_id,
        batch_id=source.batch_id,
        episode_uid=f"ai-worker-unavailable-{uuid4().hex}",
        kind="derived",
        parent_episode_id=source.id,
        derivation_version=1,
        modality="ego",
        source_fingerprint=source.source_fingerprint,
        source_start_ns=1_100_000_000,
        source_end_ns=1_300_000_000,
        quality_status="passed",
        metadata_json={
            "metrics": {"reference_frame_count": 60, "duration_s": 0.2},
            "multimodal": {"streams": [{"id": "/camera/head/rgb", "kind": "camera", "fps": 30.0}]},
        },
    )
    db_session.add(derived)
    db_session.flush()
    db_session.add(
        EpisodeArtifact(
            episode_id=derived.id,
            artifact_type="process_preview",
            storage_role="process",
            storage_uri=f"oss://must-not-leak/process/v1/{uuid4().hex}.mp4",
            checksum_sha256="a" * 64,
            size_bytes=100,
        )
    )
    db_session.commit()

    def configured_capability(_episode, *, worker_available=None):
        assert worker_available is False
        return {
            "enabled": True,
            "eligible": False,
            "disabled_reason": "ai_worker_unavailable",
            "rgb_topics": ["/camera/head/rgb"],
            "default_rgb_topic": "/camera/head/rgb",
        }

    monkeypatch.setattr(episode_multimodal, "ai_suggestion_capability", configured_capability)
    monkeypatch.setattr(
        episode_router, "celery_available", lambda queue: queue != "ai", raising=False
    )

    response = client.get(f"/api/v1/episodes/{derived.id}/ai-suggestions", headers=admin_headers)

    assert response.status_code == 200
    assert response.json()["data"]["capability"]["enabled"] is True
    assert response.json()["data"]["capability"]["eligible"] is False
    assert response.json()["data"]["capability"]["disabled_reason"] == "ai_worker_unavailable"
    assert "storage_uri" not in response.text


def test_annotation_ai_request_is_idempotent_and_never_serializes_preview_location(
    client, admin_headers, db_session, monkeypatch
):
    from data.routers import episodes as episode_router
    from data.services import episode_multimodal

    actor, workspace, _project, source, _item = _claimed_cut_context(db_session)
    derived = Episode(
        workspace_id=workspace.id,
        task_set_id=source.task_set_id,
        batch_id=source.batch_id,
        episode_uid=f"ai-request-derived-{uuid4().hex}",
        kind="derived",
        parent_episode_id=source.id,
        derivation_version=1,
        modality="ego",
        source_fingerprint=source.source_fingerprint,
        source_start_ns=1_100_000_000,
        source_end_ns=1_300_000_000,
        quality_status="passed",
        metadata_json={"metrics": {"reference_frame_count": 60}},
    )
    db_session.add(derived)
    db_session.flush()
    annotation_item = WorkItem(
        workspace_id=workspace.id,
        episode_id=derived.id,
        kind="annotation",
        status="in_progress",
        assignee_user_id=actor.id,
        created_by_user_id=actor.id,
    )
    db_session.add_all(
        (
            annotation_item,
            EpisodeArtifact(
                episode_id=derived.id,
                artifact_type="process_preview",
                storage_role="process",
                storage_uri="oss://must-not-leak/process/v1/example.mp4",
                checksum_sha256="b" * 64,
                size_bytes=100,
            ),
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        episode_multimodal,
        "ai_suggestion_capability",
        lambda _episode: {
            "enabled": True,
            "eligible": True,
            "disabled_reason": "",
            "rgb_topics": ["/camera/head/rgb"],
            "default_rgb_topic": "/camera/head/rgb",
        },
    )
    monkeypatch.setattr(episode_router, "require_celery_worker", lambda _queue: None)
    dispatched: list[str] = []
    monkeypatch.setattr(
        episode_router,
        "dispatch_media_job",
        lambda job, *, worker_prechecked: dispatched.append(job.id) or f"celery:{job.id}",
    )

    first = client.post(
        f"/api/v1/episodes/{derived.id}/ai-suggestions",
        headers=admin_headers,
        json={"rgb_topic": "/camera/head/rgb"},
    )
    second = client.post(
        f"/api/v1/episodes/{derived.id}/ai-suggestions",
        headers=admin_headers,
        json={"rgb_topic": "/camera/head/rgb"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["data"]["job"]["id"] == second.json()["data"]["job"]["id"]
    assert first.json()["data"]["dispatch"].startswith("celery:")
    assert second.json()["data"]["dispatch"] == "already_exists"
    assert dispatched == [first.json()["data"]["job"]["id"]]
    job = db_session.get(JobRun, first.json()["data"]["job"]["id"])
    assert job is not None
    assert job.kind == "episode_ai_suggestion"
    assert "storage_uri" not in job.detail_json


def test_episode_ai_worker_normalizes_provider_frames_to_derived_timeline(
    db_session, tmp_path, monkeypatch
):
    from data.integrations.embodied_vl import behavior_suggestion
    from data.services import behavior_ai_delivery, episode_multimodal

    actor, workspace, _project, source, _item = _claimed_cut_context(db_session)
    derived = Episode(
        workspace_id=workspace.id,
        task_set_id=source.task_set_id,
        batch_id=source.batch_id,
        episode_uid=f"ai-worker-derived-{uuid4().hex}",
        kind="derived",
        parent_episode_id=source.id,
        derivation_version=1,
        modality="ego",
        source_fingerprint=source.source_fingerprint,
        source_start_ns=1_100_000_000,
        source_end_ns=1_300_000_000,
        quality_status="passed",
        metadata_json={"metrics": {"reference_frame_count": 20}},
    )
    db_session.add(derived)
    db_session.flush()
    item = WorkItem(
        workspace_id=workspace.id,
        episode_id=derived.id,
        kind="annotation",
        status="in_progress",
        assignee_user_id=actor.id,
        created_by_user_id=actor.id,
    )
    artifact = EpisodeArtifact(
        episode_id=derived.id,
        artifact_type="process_preview",
        storage_role="process",
        storage_uri="nas://process/v1/example.mp4",
        checksum_sha256="c" * 64,
        size_bytes=100,
        metadata_json={
            "frame_timestamps_ns": [
                str(1_100_000_000 + index * index * 500_000) for index in range(20)
            ]
        },
    )
    db_session.add_all((item, artifact))
    db_session.commit()
    monkeypatch.setattr(
        episode_multimodal,
        "ai_suggestion_capability",
        lambda _episode: {
            "enabled": True,
            "eligible": True,
            "disabled_reason": "",
            "rgb_topics": ["/camera/head/rgb"],
            "default_rgb_topic": "/camera/head/rgb",
        },
    )

    monkeypatch.setattr(
        behavior_ai_delivery,
        "issue_behavior_ai_input_url",
        lambda **_kwargs: "https://process.example.com/ai-inputs/v1/episode.mp4?signature=secret",
    )

    class FakeProvider:
        def __init__(self, **_kwargs):
            pass

        def suggest(self, preview_url, *, correlation_id):
            assert correlation_id
            assert preview_url.startswith("https://")
            segment = type(
                "Segment",
                (),
                {"start_frame": 2, "end_frame": 10, "description": "close box", "confidence": 0.9},
            )
            return type("Result", (), {"segments": (segment,)})()

    monkeypatch.setattr(behavior_suggestion, "EmbodiedVlBehaviorSuggestionProvider", FakeProvider)
    job, created = episode_multimodal.create_episode_ai_suggestion(
        db_session,
        episode=derived,
        actor_id=actor.id,
        rgb_topic="/camera/head/rgb",
    )

    assert created is True
    result = episode_multimodal.run_episode_ai_suggestion(db_session, job)

    assert result["input_fingerprint"] == f"sha256:{'c' * 64}"
    assert result["segments"] == [
        {
            "id": f"ai_{job.id[:12]}_0000",
            "start_ns": "1102000000",
            "end_ns": "1150000000",
            "description": "close box",
            "confidence": 0.9,
        }
    ]
    assert "storage_uri" not in str(result)


def test_workbench_snapshot_degrades_when_preview_signing_unavailable(db_session, monkeypatch):
    from data.services import episode_media_access
    from data.services.episode_workbench import workbench_snapshot

    actor, workspace, _project, source, cut_item = _claimed_cut_context(db_session)
    _grant_admin_workspace_access(db_session, workspace)
    source.metadata_json = {
        **(source.metadata_json or {}),
        "reference_topic": "/camera/front/rgb",
    }
    db_session.add(
        EpisodeArtifact(
            episode_id=source.id,
            artifact_type="process_preview",
            storage_role="process",
            storage_uri="oss://process-bucket/process/v1/episode/preview.mp4",
            metadata_json={"media_type": "video/mp4"},
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        episode_media_access,
        "issue_episode_preview_url",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        episode_media_access,
        "issue_episode_preview_fallback_url",
        lambda *_args, **_kwargs: None,
    )

    snapshot = workbench_snapshot(
        db_session,
        episode_id=source.id,
        work_item_id=cut_item.id,
        actor_id=actor.id,
    )

    assert snapshot["media"]["preview"]["available"] is False
    assert snapshot["media"]["preview"]["status"] == "unavailable"
    assert snapshot["timeline"]["start_ns"] == "1000000000"


def test_workbench_snapshot_reports_processing_preview_status(db_session):
    from data.services.episode_workbench import workbench_snapshot

    actor, workspace, _project, source, cut_item = _claimed_cut_context(db_session)
    _grant_admin_workspace_access(db_session, workspace)
    db_session.add(
        JobRun(
            id=uuid4().hex,
            kind="episode_preview",
            resource_type="episode",
            resource_id=str(source.id),
            workspace_id=source.workspace_id,
            task_set_id=source.task_set_id,
            idempotency_key=f"episode-preview:{source.id}:processing-status",
            queue="media",
            status="queued",
            phase="queued",
            detail_json={"episode_id": source.id},
        )
    )
    db_session.commit()

    snapshot = workbench_snapshot(
        db_session,
        episode_id=source.id,
        work_item_id=cut_item.id,
        actor_id=actor.id,
    )

    assert snapshot["media"]["preview"]["available"] is False
    assert snapshot["media"]["preview"]["status"] == "processing"
