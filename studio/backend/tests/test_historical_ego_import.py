from __future__ import annotations

import hashlib
import json
from io import BytesIO
from uuid import uuid4

import pytest
from collector_fixtures import make_collector
from mcap.writer import Writer

from data.database import (
    ArtifactOperation,
    CollectionDevice,
    Episode,
    EpisodeArtifact,
    EpisodeCollectorAttribution,
    EpisodeDeviceAttribution,
    ExternalOssImportScope,
    ImportAttempt,
    ImportCandidate,
    JobRun,
    TaskLabel,
    TaskSet,
    TaskSetSourceImport,
    User,
    WorkItem,
    Workspace,
)
from data.infra.oss_client import OSSObjectInfo
from data.services.batches import create_batch
from data.services.import_intake import (
    import_scanned_candidate,
    list_import_candidates,
    retry_import_parse,
    scan_import_candidates,
)
from data.services.import_sessions import create_import_session


def _session(db_session):
    actor = db_session.query(User).order_by(User.id).first()
    suffix = uuid4().hex
    workspace = Workspace(name=f"legacy ego intake workspace {suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    project = TaskSet(workspace_id=workspace.id, name=f"legacy ego intake project {suffix}")
    db_session.add(project)
    db_session.flush()
    db_session.add(
        ExternalOssImportScope(
            workspace_id=workspace.id,
            task_set_id=project.id,
            bucket="approved-source-bucket",
            prefixes_json=["incoming"],
        )
    )
    task_label = TaskLabel(key=f"legacy-ego-{workspace.id}", name="close the box")
    db_session.add(task_label)
    db_session.flush()
    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="legacy ego intake batch",
        batch_type="ego",
        actor_id=actor.id,
    )
    import_session = create_import_session(
        db_session,
        batch_id=batch.id,
        import_type="oss_scan",
        actor_id=actor.id,
        task_label_id=task_label.id,
    )
    db_session.commit()
    return workspace, project, batch, import_session


def _legacy_ego_objects(
    *, task_id: str, device_id: str, episode_id: str, capture_type: str = "human_ego_demo"
):
    parent = f"incoming/raw/v1/tasks/{task_id}/devices/{device_id}/episodes/{episode_id}"
    data_key = f"{parent}/data.mcap"
    metadata_key = f"{parent}/metadata.json"
    complete_key = f"{parent}/complete.json"
    data = f"mcap:{task_id}:{device_id}:{episode_id}".encode()
    digest = hashlib.sha256(data).hexdigest()
    metadata = {
        "qrdf_version": "0.2.0",
        "episode_id": episode_id,
        "data_file": "data.mcap",
        "capture": {"mode": "ego", "episode_type": capture_type},
    }
    complete = {
        "schema_version": 1,
        "task_id": task_id,
        "device_id": device_id,
        "episode_id": episode_id,
        "objects": ["data.mcap", "metadata.json"],
        "file_digest": digest,
        "file_size": len(data),
        "recording_status": "complete",
        "integrity_status": "not_checked",
        "integrity_issues": [],
        "completed_at": "2026-08-07T00:00:00Z",
    }
    return {
        data_key: (data, {"x-oss-meta-sha256": digest}),
        metadata_key: (
            json.dumps(metadata).encode("utf-8"),
            {"x-oss-meta-sha256": hashlib.sha256(json.dumps(metadata).encode("utf-8")).hexdigest()},
        ),
        complete_key: (json.dumps(complete).encode("utf-8"), {"x-oss-meta-sha256": digest}),
    }


def _capture_source_objects(
    *,
    episode_id: str,
    episode_type: str = "human_demonstration",
) -> dict[str, tuple[bytes, dict[str, str]]]:
    parent = f"incoming/raw/v2/sources/{episode_id}"
    data_key = f"{parent}/data.mcap"
    metadata_key = f"{parent}/metadata.json"
    complete_key = f"{parent}/complete.json"
    data = f"mcap:capture:{episode_id}".encode()
    metadata = {
        "qrdf_version": "0.2.0",
        "episode_id": episode_id,
        "data_file": "data.mcap",
        "capture": {"mode": "iphone_umi", "episode_type": episode_type},
    }
    metadata_data = json.dumps(metadata, sort_keys=True).encode("utf-8")

    def etag(payload: bytes) -> str:
        return f"etag-{hashlib.sha256(payload).hexdigest()[:16]}"

    marker = {
        "schema": "quicdata.capture.upload-complete.v1",
        "schema_version": 1,
        "episode_id": episode_id,
        "ingest_mode": "trusted_offline",
        "objects": {
            "data.mcap": {"size": len(data), "etag": etag(data)},
            "metadata.json": {
                "size": len(metadata_data),
                "etag": etag(metadata_data),
            },
        },
        "completed_at": "2026-08-20T00:00:00Z",
    }
    return {
        data_key: (data, {}),
        metadata_key: (metadata_data, {}),
        complete_key: (json.dumps(marker, sort_keys=True).encode("utf-8"), {}),
    }


def _replace_capture_metadata(
    objects: dict[str, tuple[bytes, dict[str, str]]],
    metadata: dict[str, object],
) -> None:
    metadata_key = next(key for key in objects if key.endswith("metadata.json"))
    complete_key = next(key for key in objects if key.endswith("complete.json"))
    metadata_data = json.dumps(metadata, sort_keys=True).encode("utf-8")
    marker = json.loads(objects[complete_key][0].decode("utf-8"))
    marker["objects"]["metadata.json"] = {
        "size": len(metadata_data),
        "etag": f"etag-{hashlib.sha256(metadata_data).hexdigest()[:16]}",
    }
    objects[metadata_key] = (metadata_data, {})
    objects[complete_key] = (json.dumps(marker, sort_keys=True).encode("utf-8"), {})


def _replace_capture_data(
    objects: dict[str, tuple[bytes, dict[str, str]]],
    data: bytes,
) -> None:
    data_key = next(key for key in objects if key.endswith("data.mcap"))
    complete_key = next(key for key in objects if key.endswith("complete.json"))
    marker = json.loads(objects[complete_key][0].decode("utf-8"))
    marker["objects"]["data.mcap"] = {
        "size": len(data),
        "etag": f"etag-{hashlib.sha256(data).hexdigest()[:16]}",
    }
    objects[data_key] = (data, {})
    objects[complete_key] = (json.dumps(marker, sort_keys=True).encode("utf-8"), {})


def _mcap_bytes(*, topic: str = "/camera/front/rgb", frame_count: int = 30) -> bytes:
    output = BytesIO()
    writer = Writer(output)
    writer.start()
    schema_id = writer.register_schema(name="test.Frame", encoding="jsonschema", data=b"{}")
    channel_id = writer.register_channel(topic=topic, message_encoding="json", schema_id=schema_id)
    for index in range(frame_count):
        timestamp = 1_000_000_000 + index * 33_333_333
        writer.add_message(
            channel_id=channel_id, log_time=timestamp, publish_time=timestamp, data=b"{}"
        )
    writer.finish()
    return output.getvalue()


def _make_legacy_ego_quality_ready(objects: dict[str, tuple[bytes, dict[str, str]]]) -> None:
    data_key = next(key for key in objects if key.endswith("data.mcap"))
    metadata_key = next(key for key in objects if key.endswith("metadata.json"))
    complete_key = next(key for key in objects if key.endswith("complete.json"))
    data = _mcap_bytes()
    digest = hashlib.sha256(data).hexdigest()
    metadata = json.loads(objects[metadata_key][0].decode("utf-8"))
    metadata["timing"] = {"duration_s": 1.0}
    metadata["sensors"] = {
        "cameras": [
            {
                "name": "front",
                "topic": "/camera/front/rgb",
                "encoding": "jpeg",
                "width": 640,
                "height": 480,
                "fps": 30.0,
            }
        ]
    }
    metadata_data = json.dumps(metadata).encode("utf-8")
    marker = json.loads(objects[complete_key][0].decode("utf-8"))
    marker["file_digest"] = digest
    marker["file_size"] = len(data)
    marker_data = json.dumps(marker).encode("utf-8")
    objects[data_key] = (data, {"x-oss-meta-sha256": digest})
    objects[metadata_key] = (
        metadata_data,
        {"x-oss-meta-sha256": hashlib.sha256(metadata_data).hexdigest()},
    )
    objects[complete_key] = (marker_data, {"x-oss-meta-sha256": digest})


def _configure_legacy_ego_oss(
    monkeypatch,
    *,
    workspace_id: int,
    task_set_id: int,
    objects: dict[str, tuple[bytes, dict[str, str]]],
):
    import data.services.import_intake as intake
    from data.infra import oss_client

    monkeypatch.setattr(intake, "allow_oss_import", lambda: True)
    monkeypatch.setattr(
        oss_client,
        "list_prefix",
        lambda bucket, prefix, **kwargs: [
            {"key": key, "size": len(payload)}
            for key, (payload, _metadata) in sorted(objects.items())
            if key.startswith(prefix.rstrip("/") + "/")
        ],
    )

    def object_info(bucket: str, key: str):
        record = objects.get(key)
        if record is None:
            return None
        payload, metadata = record
        return OSSObjectInfo(
            size=len(payload),
            etag=f"etag-{hashlib.sha256(payload).hexdigest()[:16]}",
            crc64=None,
            version_id="version-1",
            metadata=dict(metadata),
        )

    def read_json_object(bucket: str, key: str, *, if_match: str | None = None, **_kwargs):
        info = object_info(bucket, key)
        assert info is not None
        assert if_match == info.etag
        return json.loads(objects[key][0].decode("utf-8"))

    monkeypatch.setattr(oss_client, "object_info", object_info)
    monkeypatch.setattr(oss_client, "read_json_object", read_json_object)


@pytest.mark.parametrize("episode_type", ["human_demonstration", "future_human_capture"])
def test_capture_source_scan_uses_episode_id_without_ego_extension(
    db_session,
    monkeypatch,
    episode_type,
):
    workspace, project, _batch, import_session = _session(db_session)
    objects = _capture_source_objects(
        episode_id="episode-capture-a",
        episode_type=episode_type,
    )
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=objects,
    )

    candidates = scan_import_candidates(db_session, import_session_id=import_session.id)

    assert candidates == [
        {
            "id": candidates[0]["id"],
            "candidate_type": "capture_episode_oss",
            "status": "discovered",
            "original_name": "episode-capture-a",
            "size_bytes": candidates[0]["size_bytes"],
            "source_group": {"key": None, "name": None, "status": "missing"},
            "collector_attribution": {
                "source": "unknown",
                "state": "unreported",
                "reported_identifier": "",
                "profile": None,
                "importable": True,
                "error_code": "",
            },
            "source_status": "discovered",
            "captured_ended_at": None,
        }
    ]
    assert not {"bucket", "key", "etag", "metadata", "locator_json"} & set(candidates[0])
    stored = db_session.get(ImportCandidate, candidates[0]["id"])
    assert stored is not None
    assert stored.locator_json["schema"] == "quicdata.capture-episode-oss.v1"
    assert stored.locator_json["source"]["episode_id"] == "episode-capture-a"
    assert "ego_source_id" not in json.dumps(stored.locator_json)


def test_capture_candidate_treats_unknown_collector_as_default_and_keeps_old_snapshot_importable(
    db_session,
    monkeypatch,
):
    workspace, project, _batch, import_session = _session(db_session)
    default = make_collector(
        db_session,
        workspace_id=workspace.id,
        name="Default collector",
        is_active=True,
    )
    db_session.add(default)
    db_session.flush()
    import_session.default_collector_profile_id = default.id
    db_session.commit()

    objects = _capture_source_objects(episode_id="episode-capture-unknown-collector")
    metadata_key = next(key for key in objects if key.endswith("metadata.json"))
    metadata = json.loads(objects[metadata_key][0].decode("utf-8"))
    metadata["operator"] = {"id": "unknown"}
    _replace_capture_metadata(objects, metadata)
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=objects,
    )

    candidate_view = scan_import_candidates(db_session, import_session_id=import_session.id)[0]
    assert candidate_view["collector_attribution"] == {
        "source": "default",
        "state": "default",
        "reported_identifier": "",
        "profile": {
            "id": default.id,
            "name": "Default collector",
            "profile_key": default.profile_key,
            "display_label": "Default collector",
            "is_active": True,
        },
        "importable": True,
        "error_code": "",
    }

    # Simulate a candidate persisted by the old scanner before ``unknown``
    # was recognized as an absence marker.
    stored = db_session.get(ImportCandidate, candidate_view["id"])
    assert stored is not None
    stored.collector_hint_status = "invalid"
    stored.reported_collector_identifier = "unknown"
    db_session.flush()

    repaired_view = scan_import_candidates(db_session, import_session_id=import_session.id)[0]
    assert repaired_view["collector_attribution"]["state"] == "default"
    assert repaired_view["collector_attribution"]["importable"] is True

    selected = import_scanned_candidate(
        db_session,
        import_session_id=import_session.id,
        candidate_id=stored.id,
    )
    assert selected["parse_job_id"]


def test_capture_candidate_projects_reported_collector_and_preserves_it_over_default(
    db_session,
    monkeypatch,
):
    import data.services.import_intake as intake
    from data.infra import oss_client
    from data.services import import_parser

    workspace, project, _batch, import_session = _session(db_session)
    reported = make_collector(
        db_session,
        workspace_id=workspace.id,
        name="Historical collector",
        is_active=False,
    )
    default = make_collector(
        db_session,
        workspace_id=workspace.id,
        name="Default collector",
        is_active=True,
    )
    db_session.add_all((reported, default))
    db_session.flush()
    import_session.default_collector_profile_id = default.id
    db_session.commit()

    objects = _capture_source_objects(episode_id="episode-capture-reported-collector")
    metadata_key = next(key for key in objects if key.endswith("metadata.json"))
    metadata = json.loads(objects[metadata_key][0].decode("utf-8"))
    metadata["operator"] = {"id": reported.profile_key}
    _replace_capture_metadata(objects, metadata)
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=objects,
    )
    monkeypatch.setattr(intake, "uses_cloud_uri_authority", lambda: True)
    monkeypatch.setattr(import_parser, "uses_cloud_uri_authority", lambda: True)
    monkeypatch.setattr(
        oss_client, "bucket_name", lambda role: "raw-bucket" if role == "raw" else role
    )
    monkeypatch.setattr(
        oss_client,
        "copy_object",
        lambda _source_bucket, source_key, _target_bucket, target_key, **_kwargs: (
            objects.__setitem__(target_key, objects[source_key]),
            f"oss://raw-bucket/{target_key}",
        )[-1],
    )

    candidate_view = scan_import_candidates(db_session, import_session_id=import_session.id)[0]

    assert candidate_view["collector_attribution"] == {
        "source": "machine_reported",
        "state": "automatic",
        "reported_identifier": reported.profile_key,
        "profile": {
            "id": reported.id,
            "name": "Historical collector",
            "profile_key": reported.profile_key,
            "display_label": "Historical collector",
            "is_active": False,
        },
        "importable": True,
        "error_code": "",
    }

    selected = import_scanned_candidate(
        db_session,
        import_session_id=import_session.id,
        candidate_id=candidate_view["id"],
        collector_profile_id=default.id,
    )
    db_session.commit()
    parse_job = db_session.get(JobRun, selected["parse_job_id"])
    assert parse_job is not None
    import_parser.parse_import_session_original(db_session, parse_job)

    source = db_session.query(Episode).filter(Episode.import_session_id == import_session.id).one()
    collector_rows = (
        db_session.query(EpisodeCollectorAttribution)
        .filter(EpisodeCollectorAttribution.root_source_episode_id == source.id)
        .order_by(EpisodeCollectorAttribution.id)
        .all()
    )
    assert [
        (row.source, row.collector_profile_id, row.reported_identifier, row.match_status)
        for row in collector_rows
    ] == [("machine_reported", reported.id, reported.profile_key, "matched")]


@pytest.mark.parametrize(
    ("operator_id", "expected_state"),
    (("9999", "mapping_error"), ("not-four-digits", "format_error")),
)
def test_capture_candidate_blocks_unmapped_or_invalid_reported_collector(
    db_session,
    monkeypatch,
    operator_id,
    expected_state,
):
    workspace, project, _batch, import_session = _session(db_session)
    objects = _capture_source_objects(episode_id=f"episode-capture-{expected_state}")
    metadata_key = next(key for key in objects if key.endswith("metadata.json"))
    metadata = json.loads(objects[metadata_key][0].decode("utf-8"))
    metadata["operator"] = {"id": operator_id}
    _replace_capture_metadata(objects, metadata)
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=objects,
    )

    candidate_view = scan_import_candidates(db_session, import_session_id=import_session.id)[0]

    assert candidate_view["collector_attribution"]["state"] == expected_state
    assert candidate_view["collector_attribution"]["reported_identifier"] == operator_id
    assert candidate_view["collector_attribution"]["importable"] is False
    with pytest.raises(ValueError, match="reported collector"):
        import_scanned_candidate(
            db_session,
            import_session_id=import_session.id,
            candidate_id=candidate_view["id"],
        )


@pytest.mark.parametrize(
    "invalid_shape",
    ["ego_source_id", "marker_episode_id", "metadata_episode_id", "data_etag"],
)
def test_capture_source_scan_rejects_conflicting_or_ego_specific_identity(
    db_session,
    monkeypatch,
    invalid_shape,
):
    workspace, project, _batch, import_session = _session(db_session)
    objects = _capture_source_objects(episode_id="episode-capture-a")
    complete_key = next(key for key in objects if key.endswith("complete.json"))
    marker = json.loads(objects[complete_key][0].decode("utf-8"))
    if invalid_shape == "ego_source_id":
        marker["ego_source_id"] = marker["episode_id"]
        objects[complete_key] = (json.dumps(marker, sort_keys=True).encode("utf-8"), {})
    elif invalid_shape == "marker_episode_id":
        marker["episode_id"] = "episode-conflict"
        objects[complete_key] = (json.dumps(marker, sort_keys=True).encode("utf-8"), {})
    elif invalid_shape == "metadata_episode_id":
        metadata_key = next(key for key in objects if key.endswith("metadata.json"))
        metadata = json.loads(objects[metadata_key][0].decode("utf-8"))
        metadata["episode_id"] = "episode-conflict"
        _replace_capture_metadata(objects, metadata)
    else:
        marker["objects"]["data.mcap"]["etag"] = "changed-etag"
        objects[complete_key] = (json.dumps(marker, sort_keys=True).encode("utf-8"), {})
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=objects,
    )

    assert scan_import_candidates(db_session, import_session_id=import_session.id) == []
    assert (
        db_session.query(ImportCandidate)
        .filter(ImportCandidate.import_session_id == import_session.id)
        .count()
        == 0
    )


def test_capture_source_scan_rejects_changed_content_for_an_existing_episode_id(
    db_session,
    monkeypatch,
):
    workspace, project, batch, first_import_session = _session(db_session)
    first_objects = _capture_source_objects(episode_id="episode-capture-a")
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=first_objects,
    )
    assert len(scan_import_candidates(db_session, import_session_id=first_import_session.id)) == 1

    second_import_session = create_import_session(
        db_session,
        batch_id=batch.id,
        import_type="oss_scan",
        actor_id=first_import_session.owner_user_id,
        task_label_id=first_import_session.task_label_id,
    )
    db_session.commit()
    changed_objects = _capture_source_objects(episode_id="episode-capture-a")
    _replace_capture_data(changed_objects, b"changed-capture-source")
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=changed_objects,
    )

    assert scan_import_candidates(db_session, import_session_id=second_import_session.id) == []
    assert (
        db_session.query(TaskSetSourceImport)
        .filter(TaskSetSourceImport.task_set_id == project.id)
        .count()
        == 1
    )


def test_capture_source_ledger_reuses_same_identity_after_concurrent_insert(
    db_session,
):
    from threading import Event, Thread

    from sqlalchemy import event

    from data.database import SessionLocal
    from data.services.capture_batch_import import _upsert_task_set_source_import

    _workspace, project, _batch, _import_session = _session(db_session)
    fingerprint = "d" * 64
    locator = {"schema": "quicdata.capture-episode-oss.v1"}
    first = SessionLocal()
    second = SessionLocal()
    second_flush_started = Event()
    result: dict[str, int] = {}
    errors: list[BaseException] = []

    @event.listens_for(second, "before_flush")
    def _signal_second_flush(*_args):
        second_flush_started.set()

    first_ledger = _upsert_task_set_source_import(
        first,
        task_set_id=project.id,
        source_fingerprint=fingerprint,
        display_name="episode-concurrent",
        source_locator_json=locator,
        captured_ended_at=None,
    )

    def _insert_same_source() -> None:
        try:
            ledger = _upsert_task_set_source_import(
                second,
                task_set_id=project.id,
                source_fingerprint=fingerprint,
                display_name="episode-concurrent",
                source_locator_json=locator,
                captured_ended_at=None,
            )
            second.commit()
            result["ledger_id"] = ledger.id
        except BaseException as exc:
            errors.append(exc)
            second.rollback()

    worker = Thread(target=_insert_same_source)
    worker.start()
    try:
        assert second_flush_started.wait(timeout=2)
        first.commit()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert errors == []
        assert result["ledger_id"] == first_ledger.id
    finally:
        if worker.is_alive():
            first.rollback()
            worker.join(timeout=5)
        first.close()
        second.close()


def test_capture_source_candidate_imports_with_generic_provenance(
    db_session,
    monkeypatch,
):
    import data.services.import_intake as intake
    from data.infra import oss_client
    from data.services import import_parser

    workspace, project, _batch, import_session = _session(db_session)
    objects = _capture_source_objects(episode_id="episode-capture-a")
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=objects,
    )
    monkeypatch.setattr(intake, "uses_cloud_uri_authority", lambda: True)
    monkeypatch.setattr(import_parser, "uses_cloud_uri_authority", lambda: True)
    monkeypatch.setattr(
        oss_client, "bucket_name", lambda role: "raw-bucket" if role == "raw" else role
    )

    copied: list[tuple[str, str]] = []

    def copy_object(source_bucket, source_key, target_bucket, target_key, **_kwargs):
        copied.append((source_key, target_key))
        objects[target_key] = objects[source_key]
        return f"oss://{target_bucket}/{target_key}"

    monkeypatch.setattr(oss_client, "copy_object", copy_object)

    candidate = scan_import_candidates(db_session, import_session_id=import_session.id)[0]
    selected = import_scanned_candidate(
        db_session,
        import_session_id=import_session.id,
        candidate_id=candidate["id"],
    )
    db_session.commit()
    parse_job = db_session.get(JobRun, selected["parse_job_id"])
    assert parse_job is not None

    result = import_parser.parse_import_session_original(db_session, parse_job)

    source = db_session.query(Episode).filter(Episode.import_session_id == import_session.id).one()
    assert result["episode_ids"] == [source.id]
    assert source.kind == "source"
    assert source.metadata_json["import"] == {
        "import_session_id": import_session.id,
        "source_protocol": "quicdata.capture.upload-complete.v1",
        "source_episode_id": "episode-capture-a",
    }
    assert "ego_source_id" not in json.dumps(source.metadata_json)
    assert "legacy_ego_task_id" not in json.dumps(source.metadata_json)
    assert {name.rsplit("/", 1)[-1] for _source, name in copied} == {
        "data.mcap",
        "metadata.json",
        "complete.json",
    }


def test_capture_source_import_persists_workspace_scoped_machine_provenance(
    db_session,
    monkeypatch,
):
    """Producer hints remain descriptive and cannot override session tenancy."""
    import data.services.import_intake as intake
    from data.infra import oss_client
    from data.services import import_parser

    workspace, project, _batch, import_session = _session(db_session)
    collector = make_collector(
        db_session,
        workspace_id=workspace.id,
        name="Recorded collector",
        is_active=False,
    )
    device = CollectionDevice(
        workspace_id=workspace.id,
        name="Recorded device",
        device_type="phone",
        serial_number="SN-RECORDED-42",
        is_active=False,
    )
    db_session.add_all((collector, device))
    db_session.flush()

    objects = _capture_source_objects(episode_id="episode-capture-provenance")
    source_group_name = "Morning Line A"
    _replace_capture_metadata(
        objects,
        {
            "qrdf_version": "0.2.0",
            "episode_id": "episode-capture-provenance",
            "data_file": "data.mcap",
            "capture": {"mode": "iphone_umi", "episode_type": "human_demonstration"},
            "source_group": {
                "key": f"sg1_{hashlib.sha256(source_group_name.encode('utf-8')).hexdigest()}",
                "name": source_group_name,
            },
            "operator": {"id": collector.profile_key},
            "devices": [{"serial_number": "SN-RECORDED-42"}],
        },
    )
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=objects,
    )
    monkeypatch.setattr(intake, "uses_cloud_uri_authority", lambda: True)
    monkeypatch.setattr(import_parser, "uses_cloud_uri_authority", lambda: True)
    monkeypatch.setattr(
        oss_client, "bucket_name", lambda role: "raw-bucket" if role == "raw" else role
    )
    monkeypatch.setattr(
        oss_client,
        "copy_object",
        lambda _source_bucket, source_key, _target_bucket, target_key, **_kwargs: (
            objects.__setitem__(target_key, objects[source_key]),
            f"oss://raw-bucket/{target_key}",
        )[-1],
    )

    candidate_view = scan_import_candidates(db_session, import_session_id=import_session.id)[0]
    candidate = db_session.get(ImportCandidate, candidate_view["id"])
    assert candidate is not None
    assert candidate.source_group_name == source_group_name
    assert candidate.source_group_status == "valid"

    selected = import_scanned_candidate(
        db_session,
        import_session_id=import_session.id,
        candidate_id=candidate.id,
    )
    db_session.commit()
    parse_job = db_session.get(JobRun, selected["parse_job_id"])
    assert parse_job is not None
    import_parser.parse_import_session_original(db_session, parse_job)

    source = db_session.query(Episode).filter(Episode.import_session_id == import_session.id).one()
    assert source.reported_source_group_name == source_group_name
    assert source.reported_source_group_status == "valid"
    collector_rows = (
        db_session.query(EpisodeCollectorAttribution)
        .filter(EpisodeCollectorAttribution.root_source_episode_id == source.id)
        .order_by(EpisodeCollectorAttribution.id)
        .all()
    )
    device_rows = (
        db_session.query(EpisodeDeviceAttribution)
        .filter(EpisodeDeviceAttribution.root_source_episode_id == source.id)
        .order_by(EpisodeDeviceAttribution.id)
        .all()
    )
    machine_collector = next(row for row in collector_rows if row.source == "machine_reported")
    machine_device = next(row for row in device_rows if row.source == "machine_reported")
    assert (
        machine_collector.collector_profile_id,
        machine_collector.reported_identifier,
        machine_collector.match_status,
    ) == (
        collector.id,
        collector.profile_key,
        "matched",
    )
    assert (
        machine_device.collection_device_id,
        machine_device.reported_identifier,
        machine_device.match_status,
    ) == (
        device.id,
        "SN-RECORDED-42",
        "matched",
    )
    assert [row.source for row in collector_rows] == ["machine_reported"]
    assert [row.source for row in device_rows] == ["machine_reported"]


def test_capture_source_invalid_source_group_or_device_provenance_does_not_reject_core_qrdf_import(
    db_session,
    monkeypatch,
):
    """Untrusted hints never make an otherwise valid capture package unusable."""
    import data.services.import_intake as intake
    from data.infra import oss_client
    from data.services import import_parser

    workspace, project, _batch, import_session = _session(db_session)
    objects = _capture_source_objects(episode_id="episode-capture-invalid-provenance")
    _replace_capture_metadata(
        objects,
        {
            "qrdf_version": "0.2.0",
            "episode_id": "episode-capture-invalid-provenance",
            "data_file": "data.mcap",
            "capture": {"mode": "iphone_umi", "episode_type": "human_demonstration"},
            "source_group": {"name": "trusted", "key": "sg1_wrong"},
            "devices": [{"serial_number": "\x1cnot-safe"}],
        },
    )
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=objects,
    )
    monkeypatch.setattr(intake, "uses_cloud_uri_authority", lambda: True)
    monkeypatch.setattr(import_parser, "uses_cloud_uri_authority", lambda: True)
    monkeypatch.setattr(
        oss_client, "bucket_name", lambda role: "raw-bucket" if role == "raw" else role
    )
    monkeypatch.setattr(
        oss_client,
        "copy_object",
        lambda _source_bucket, source_key, _target_bucket, target_key, **_kwargs: (
            objects.__setitem__(target_key, objects[source_key]),
            f"oss://raw-bucket/{target_key}",
        )[-1],
    )

    candidate_view = scan_import_candidates(db_session, import_session_id=import_session.id)[0]
    candidate = db_session.get(ImportCandidate, candidate_view["id"])
    assert candidate is not None
    assert candidate.source_group_status == "invalid"

    selected = import_scanned_candidate(
        db_session,
        import_session_id=import_session.id,
        candidate_id=candidate.id,
    )
    db_session.commit()
    parse_job = db_session.get(JobRun, selected["parse_job_id"])
    assert parse_job is not None
    import_parser.parse_import_session_original(db_session, parse_job)

    source = db_session.query(Episode).filter(Episode.import_session_id == import_session.id).one()
    assert source.reported_source_group_status == "invalid"
    collector = (
        db_session.query(EpisodeCollectorAttribution)
        .filter(EpisodeCollectorAttribution.root_source_episode_id == source.id)
        .one()
    )
    device = (
        db_session.query(EpisodeDeviceAttribution)
        .filter(EpisodeDeviceAttribution.root_source_episode_id == source.id)
        .one()
    )
    assert (collector.source, collector.collector_profile_id, collector.match_status) == (
        "machine_reported",
        None,
        "unknown",
    )
    assert (device.source, device.collection_device_id, device.match_status) == (
        "machine_reported",
        None,
        "unmatched",
    )


def test_capture_source_parse_rejects_post_scan_mutation_and_retries(
    db_session,
    monkeypatch,
):
    import pytest

    import data.services.import_intake as intake
    from data.infra import oss_client
    from data.services import import_parser

    workspace, project, _batch, import_session = _session(db_session)
    objects = _capture_source_objects(episode_id="episode-capture-retry")
    original_objects = dict(objects)
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=objects,
    )
    monkeypatch.setattr(intake, "uses_cloud_uri_authority", lambda: True)
    monkeypatch.setattr(import_parser, "uses_cloud_uri_authority", lambda: True)
    monkeypatch.setattr(
        oss_client, "bucket_name", lambda role: "raw-bucket" if role == "raw" else role
    )
    copied: list[str] = []
    monkeypatch.setattr(
        oss_client,
        "copy_object",
        lambda _source_bucket, source_key, _target_bucket, target_key, **_kwargs: (
            copied.append(target_key),
            objects.__setitem__(target_key, objects[source_key]),
            f"oss://raw-bucket/{target_key}",
        )[-1],
    )

    candidate = scan_import_candidates(db_session, import_session_id=import_session.id)[0]
    selected = import_scanned_candidate(
        db_session,
        import_session_id=import_session.id,
        candidate_id=candidate["id"],
    )
    db_session.commit()
    parse_job = db_session.get(JobRun, selected["parse_job_id"])
    assert parse_job is not None
    data_key = next(key for key in original_objects if key.endswith("data.mcap"))
    objects[data_key] = (b"changed-after-scan", {})

    with pytest.raises(ValueError, match="capture source could not be parsed"):
        import_parser.parse_import_session_original(db_session, parse_job)

    db_session.expire_all()
    assert (
        db_session.query(Episode).filter(Episode.import_session_id == import_session.id).count()
        == 0
    )
    assert copied == []
    assert db_session.get(type(import_session), import_session.id).status == "failed"

    objects.clear()
    objects.update(original_objects)
    retry = retry_import_parse(db_session, import_session_id=import_session.id)
    retry_job = db_session.get(JobRun, retry["parse_job_id"])
    assert retry_job is not None
    result = import_parser.parse_import_session_original(db_session, retry_job)
    assert len(result["episode_ids"]) == 1
    assert {key.rsplit("/", 1)[-1] for key in copied} == {
        "data.mcap",
        "metadata.json",
        "complete.json",
    }


def test_legacy_ego_scan_creates_one_candidate_and_ledger_row_per_source_episode(
    db_session, monkeypatch
):
    workspace, project, _batch, import_session = _session(db_session)
    objects = _legacy_ego_objects(task_id="task-a", device_id="device-a", episode_id="episode-a")
    objects.update(
        _legacy_ego_objects(task_id="task-a", device_id="device-a", episode_id="episode-b")
    )
    for key, (payload, metadata) in list(objects.items()):
        if not key.endswith("metadata.json"):
            continue
        document = json.loads(payload.decode("utf-8"))
        document["timing"] = {
            "start_timestamp_ns": 1_785_839_985_970_235_904,
            "end_timestamp_ns": 1_785_840_126_707_515_904,
            "duration_s": 140.73728,
        }
        objects[key] = (json.dumps(document).encode("utf-8"), metadata)
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=objects,
    )

    candidates = scan_import_candidates(db_session, import_session_id=import_session.id)

    assert {candidate["original_name"] for candidate in candidates} == {"episode-a", "episode-b"}
    assert all(candidate["candidate_type"] == "ego_episode_oss" for candidate in candidates)
    assert all(candidate["status"] == "discovered" for candidate in candidates)
    assert all(candidate["source_status"] == "discovered" for candidate in candidates)
    assert all(candidate["legacy_source_group"] == "task-a" for candidate in candidates)
    assert all(candidate["captured_ended_at"] is not None for candidate in candidates)
    assert all(
        "legacy_task_id" not in candidate and "episode_count" not in candidate
        for candidate in candidates
    )
    assert all(
        "bucket" not in candidate and "key" not in candidate and "locator_json" not in candidate
        for candidate in candidates
    )

    internal = [db_session.get(ImportCandidate, candidate["id"]) for candidate in candidates]
    assert all(candidate is not None for candidate in internal)
    assert all(
        candidate.task_set_source_import_id is not None
        for candidate in internal
        if candidate is not None
    )
    assert all(
        candidate.locator_json["bucket"] == "approved-source-bucket"
        for candidate in internal
        if candidate is not None
    )
    assert {
        candidate.locator_json["source"]["legacy_episode_id"]
        for candidate in internal
        if candidate is not None
    } == {"episode-a", "episode-b"}
    ledger_rows = (
        db_session.query(TaskSetSourceImport)
        .filter(TaskSetSourceImport.task_set_id == project.id)
        .order_by(TaskSetSourceImport.id)
        .all()
    )
    assert len(ledger_rows) == 2
    assert {row.status for row in ledger_rows} == {"discovered"}
    assert all(row.captured_ended_at is not None for row in ledger_rows)
    assert all("bucket" not in row.display_name for row in ledger_rows)


def test_legacy_ego_scan_accepts_a_unicode_historical_task_directory(db_session, monkeypatch):
    workspace, project, batch, import_session = _session(db_session)
    objects = _legacy_ego_objects(
        task_id="第一个采集任务",
        device_id="device-a",
        episode_id="episode-a",
    )
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=objects,
    )

    candidates = scan_import_candidates(db_session, import_session_id=import_session.id)

    assert len(candidates) == 1
    assert candidates[0]["legacy_source_group"] == "第一个采集任务"
    stored = db_session.get(ImportCandidate, candidates[0]["id"])
    assert stored is not None
    assert stored.locator_json["legacy_source_group"] == "第一个采集任务"


def test_legacy_ego_scan_rejects_control_characters_in_task_directory(db_session, monkeypatch):
    workspace, project, _batch, import_session = _session(db_session)
    objects = _legacy_ego_objects(
        task_id="unsafe\ntask",
        device_id="device-a",
        episode_id="episode-a",
    )
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=objects,
    )

    assert scan_import_candidates(db_session, import_session_id=import_session.id) == []


def test_invalid_legacy_ego_triplets_never_fall_back_to_generic_single_object_import(
    db_session, monkeypatch, caplog
):
    workspace, project, _batch, import_session = _session(db_session)
    objects = _legacy_ego_objects(
        task_id="task-a",
        device_id="device-a",
        episode_id="episode-a",
        capture_type="not_ego",
    )
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=objects,
    )

    candidates = scan_import_candidates(db_session, import_session_id=import_session.id)

    assert candidates == []
    assert (
        db_session.query(ImportCandidate)
        .filter(ImportCandidate.import_session_id == import_session.id)
        .count()
        == 0
    )
    assert "legacy EGO source rejected reason=unsupported_episode_type" in caplog.messages


def test_legacy_ego_scan_accepts_the_historical_empty_digest_marker_compatibility_shape(
    db_session, monkeypatch
):
    workspace, project, _batch, import_session = _session(db_session)
    objects = _legacy_ego_objects(task_id="task-a", device_id="device-a", episode_id="episode-a")
    complete_key = next(key for key in objects if key.endswith("complete.json"))
    marker = json.loads(objects[complete_key][0].decode("utf-8"))
    marker["file_digest"] = ""
    marker["file_size"] = 0
    objects[complete_key] = (json.dumps(marker).encode("utf-8"), {})
    data_key = next(key for key in objects if key.endswith("data.mcap"))
    objects[data_key] = (objects[data_key][0], {})
    metadata_key = next(key for key in objects if key.endswith("metadata.json"))
    objects[metadata_key] = (objects[metadata_key][0], {})
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=objects,
    )

    candidates = scan_import_candidates(db_session, import_session_id=import_session.id)

    assert len(candidates) == 1
    assert candidates[0]["candidate_type"] == "ego_episode_oss"
    assert candidates[0]["source_status"] == "discovered"


def test_legacy_ego_scan_rejects_an_empty_digest_marker_mixed_with_digest_metadata(
    db_session, monkeypatch
):
    workspace, project, _batch, import_session = _session(db_session)
    objects = _legacy_ego_objects(task_id="task-a", device_id="device-a", episode_id="episode-a")
    complete_key = next(key for key in objects if key.endswith("complete.json"))
    marker = json.loads(objects[complete_key][0].decode("utf-8"))
    marker["file_digest"] = ""
    marker["file_size"] = 0
    objects[complete_key] = (json.dumps(marker).encode("utf-8"), {})
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=objects,
    )

    assert scan_import_candidates(db_session, import_session_id=import_session.id) == []


def test_legacy_ego_scan_rejects_marker_path_and_digest_identity_mismatches(
    db_session, monkeypatch
):
    workspace, project, _batch, import_session = _session(db_session)
    objects = _legacy_ego_objects(task_id="task-a", device_id="device-a", episode_id="episode-a")
    complete_key = next(key for key in objects if key.endswith("complete.json"))
    marker = json.loads(objects[complete_key][0].decode("utf-8"))
    marker["task_id"] = "task-b"
    objects[complete_key] = (json.dumps(marker).encode("utf-8"), objects[complete_key][1])
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=objects,
    )

    assert scan_import_candidates(db_session, import_session_id=import_session.id) == []

    workspace, project, _batch, import_session = _session(db_session)
    objects = _legacy_ego_objects(task_id="task-c", device_id="device-a", episode_id="episode-a")
    data_key = next(key for key in objects if key.endswith("data.mcap"))
    objects[data_key] = (objects[data_key][0], {"x-oss-meta-sha256": "0" * 64})
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=objects,
    )

    assert scan_import_candidates(db_session, import_session_id=import_session.id) == []


def test_malformed_legacy_ego_directory_never_bypasses_into_a_generic_candidate(
    db_session, monkeypatch
):
    workspace, project, _batch, import_session = _session(db_session)
    objects = _legacy_ego_objects(task_id="task-a", device_id="device-a", episode_id="episode-a")
    malformed: dict[str, tuple[bytes, dict[str, str]]] = {}
    for key, value in objects.items():
        malformed[key.replace("tasks/task-a", "tasks/task@a")] = value
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=malformed,
    )

    assert scan_import_candidates(db_session, import_session_id=import_session.id) == []


def test_legacy_ego_scan_rejects_type_confused_marker_fields_without_crashing(
    db_session, monkeypatch
):
    workspace, project, _batch, import_session = _session(db_session)
    objects = _legacy_ego_objects(task_id="task-a", device_id="device-a", episode_id="episode-a")
    complete_key = next(key for key in objects if key.endswith("complete.json"))
    marker = json.loads(objects[complete_key][0].decode("utf-8"))
    marker["objects"] = ["data.mcap", 7]
    objects[complete_key] = (json.dumps(marker).encode("utf-8"), objects[complete_key][1])
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=objects,
    )

    assert scan_import_candidates(db_session, import_session_id=import_session.id) == []

    workspace, project, _batch, import_session = _session(db_session)
    objects = _legacy_ego_objects(task_id="task-b", device_id="device-a", episode_id="episode-a")
    complete_key = next(key for key in objects if key.endswith("complete.json"))
    marker = json.loads(objects[complete_key][0].decode("utf-8"))
    marker["file_digest"] = ""
    marker["file_size"] = False
    objects[complete_key] = (json.dumps(marker).encode("utf-8"), {})
    data_key = next(key for key in objects if key.endswith("data.mcap"))
    objects[data_key] = (objects[data_key][0], {})
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=objects,
    )

    assert scan_import_candidates(db_session, import_session_id=import_session.id) == []


def test_ego_episode_candidate_queues_async_parse_and_reserves_ledger_without_copying_in_http_request(
    db_session, monkeypatch
):
    import data.services.import_intake as intake
    from data.infra import oss_client

    workspace, project, batch, import_session = _session(db_session)
    objects = _legacy_ego_objects(task_id="task-a", device_id="device-a", episode_id="episode-a")
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=objects,
    )
    monkeypatch.setattr(intake, "uses_cloud_uri_authority", lambda: True)
    copied: list[tuple[str, str, str, str, dict[str, object]]] = []
    monkeypatch.setattr(
        oss_client,
        "copy_object",
        lambda source_bucket, source_key, target_bucket, target_key, **kwargs: copied.append(
            (source_bucket, source_key, target_bucket, target_key, kwargs)
        ),
    )

    candidate = scan_import_candidates(db_session, import_session_id=import_session.id)[0]
    result = import_scanned_candidate(
        db_session,
        import_session_id=import_session.id,
        candidate_id=candidate["id"],
    )

    assert result == {
        "import_session_id": import_session.id,
        "status": "uploaded",
        "artifact_id": None,
        "parse_job_id": result["parse_job_id"],
    }
    assert copied == []
    selected = db_session.get(ImportCandidate, candidate["id"])
    attempt = (
        db_session.query(ImportAttempt)
        .filter(ImportAttempt.import_session_id == import_session.id)
        .one()
    )
    parse_job = db_session.get(JobRun, result["parse_job_id"])
    assert selected is not None and selected.status == "consumed"
    assert attempt.status == "completed"
    assert parse_job is not None
    assert parse_job.detail_json == {
        "import_session_id": import_session.id,
        "candidate_id": candidate["id"],
        "candidate_type": "ego_episode_oss",
    }
    assert "legacy_ego_task_id" not in batch.metadata_json
    assert "legacy_ego_import_session_id" not in batch.metadata_json
    ledger = db_session.get(TaskSetSourceImport, selected.task_set_source_import_id)
    assert ledger is not None
    assert ledger.status == "importing"
    assert ledger.batch_id == batch.id
    assert ledger.import_session_id == import_session.id


def test_ego_episode_parse_copies_triplet_creates_source_episode_and_marks_ledger_imported(
    db_session, monkeypatch
):
    import data.services.import_intake as intake
    from data.infra import oss_client
    from data.services import import_parser

    workspace, project, _batch, import_session = _session(db_session)
    objects = _legacy_ego_objects(task_id="task-a", device_id="device-a", episode_id="episode-a")
    source_objects = dict(objects)
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=objects,
    )
    monkeypatch.setattr(intake, "uses_cloud_uri_authority", lambda: True)
    monkeypatch.setattr(import_parser, "uses_cloud_uri_authority", lambda: True)
    monkeypatch.setattr(
        oss_client, "bucket_name", lambda role: "raw-bucket" if role == "raw" else role
    )
    copied: list[tuple[str, str, str, str, dict[str, object]]] = []

    source_object_info = oss_client.object_info

    def object_info(bucket: str, key: str):
        info = source_object_info(bucket, key)
        if info is None or bucket != "raw-bucket":
            return info
        return OSSObjectInfo(
            size=info.size,
            etag=info.etag,
            crc64=info.crc64,
            version_id="raw-target-version",
            metadata=info.metadata,
        )

    monkeypatch.setattr(oss_client, "object_info", object_info)

    def copy_object(source_bucket, source_key, target_bucket, target_key, **kwargs):
        copied.append((source_bucket, source_key, target_bucket, target_key, kwargs))
        objects[target_key] = objects[source_key]
        return f"oss://{target_bucket}/{target_key}"

    monkeypatch.setattr(oss_client, "copy_object", copy_object)
    monkeypatch.setattr(oss_client, "object_exists", lambda _bucket, key: key in objects)

    candidate = scan_import_candidates(db_session, import_session_id=import_session.id)[0]
    stored_candidate = db_session.get(ImportCandidate, candidate["id"])
    assert stored_candidate is not None
    assert stored_candidate.source_group_name == "task-a"
    assert stored_candidate.source_group_status == "legacy"
    selected = import_scanned_candidate(
        db_session,
        import_session_id=import_session.id,
        candidate_id=candidate["id"],
    )
    db_session.commit()
    parse_job = db_session.get(JobRun, selected["parse_job_id"])
    assert parse_job is not None

    first = import_parser.parse_import_session_original(db_session, parse_job)
    second = import_parser.parse_import_session_original(db_session, parse_job)

    assert first["episode_ids"] == second["episode_ids"]
    assert len(copied) == 3
    assert {item[1].rsplit("/", 1)[-1] for item in copied} == {
        "data.mcap",
        "metadata.json",
        "complete.json",
    }
    assert all(item[0] == "approved-source-bucket" and item[2] == "raw-bucket" for item in copied)
    assert all(item[4]["forbid_overwrite"] is True for item in copied)
    assert all(item[4]["source_etag"].startswith("etag-") for item in copied)
    assert objects.keys() >= source_objects.keys()
    assert all(objects[key] == value for key, value in source_objects.items())
    source = db_session.query(Episode).filter(Episode.import_session_id == import_session.id).one()
    artifact = (
        db_session.query(EpisodeArtifact).filter(EpisodeArtifact.episode_id == source.id).one()
    )
    quality_job = (
        db_session.query(JobRun)
        .filter(JobRun.resource_id == str(source.id), JobRun.kind == "episode_quality")
        .one()
    )
    db_session.refresh(import_session)
    assert source.quality_status == "pending"
    assert source.reported_source_group_name == "task-a"
    assert source.reported_source_group_status == "legacy"
    assert artifact.artifact_type == "raw_source"
    assert artifact.storage_uri.startswith("oss://raw-bucket/raw/v2/")
    assert quality_job.queue == "media"
    assert import_session.status == "succeeded"
    ledger = db_session.get(
        TaskSetSourceImport,
        db_session.get(ImportCandidate, candidate["id"]).task_set_source_import_id,
    )
    assert ledger is not None
    assert ledger.status == "imported"
    assert ledger.source_episode_id == source.id


def test_legacy_ego_parse_materializes_a_real_mcap_for_automatic_quality(
    db_session, tmp_path, monkeypatch
):
    from data.config import settings
    from data.infra import oss_client
    from data.services import import_intake as intake
    from data.services import import_parser

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    workspace, project, _batch, import_session = _session(db_session)
    objects = _legacy_ego_objects(task_id="task-a", device_id="device-a", episode_id="episode-a")
    _make_legacy_ego_quality_ready(objects)
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=objects,
    )
    monkeypatch.setattr(intake, "uses_cloud_uri_authority", lambda: True)
    monkeypatch.setattr(import_parser, "uses_cloud_uri_authority", lambda: True)
    monkeypatch.setattr(
        oss_client, "bucket_name", lambda role: "raw-bucket" if role == "raw" else role
    )

    def copy_object(_source_bucket, source_key, _target_bucket, target_key, **_kwargs):
        objects[target_key] = objects[source_key]
        return f"oss://raw-bucket/{target_key}"

    def download_to(local_dest, bucket, key):
        assert bucket == "raw-bucket"
        local_dest.mkdir(parents=True, exist_ok=True)
        for file_name in ("data.mcap", "metadata.json", "complete.json"):
            (local_dest / file_name).write_bytes(objects[f"{key.rstrip('/')}/{file_name}"][0])
        return local_dest

    monkeypatch.setattr(oss_client, "copy_object", copy_object)
    monkeypatch.setattr(oss_client, "download_to", download_to)

    candidate = scan_import_candidates(db_session, import_session_id=import_session.id)[0]
    selected = import_scanned_candidate(
        db_session,
        import_session_id=import_session.id,
        candidate_id=candidate["id"],
    )
    db_session.commit()
    parse_job = db_session.get(JobRun, selected["parse_job_id"])
    assert parse_job is not None

    import_parser.parse_import_session_original(db_session, parse_job)
    source = db_session.query(Episode).filter(Episode.import_session_id == import_session.id).one()
    quality_job = (
        db_session.query(JobRun)
        .filter(
            JobRun.kind == "episode_quality",
            JobRun.resource_id == str(source.id),
        )
        .one()
    )

    quality = import_parser.run_episode_quality(db_session, quality_job)

    db_session.refresh(source)
    assert quality["quality_status"] == "passed"
    assert source.quality_status == "passed"
    assert source.workflow_status == "ready"
    assert quality["metrics"] == {
        "reference_frame_count": 30,
        "duration_s": 1.0,
        "average_rgb_rate_hz": 30.0,
    }
    cut_item = (
        db_session.query(WorkItem)
        .filter(WorkItem.episode_id == source.id, WorkItem.kind == "cut")
        .one()
    )
    assert cut_item.status == "pending"


def test_legacy_ego_parse_failure_cleans_only_new_raw_objects_and_allows_parse_retry(
    db_session, monkeypatch
):
    import pytest

    from data.infra import oss_client
    from data.services import import_parser

    workspace, project, _batch, import_session = _session(db_session)
    objects = _legacy_ego_objects(task_id="task-a", device_id="device-a", episode_id="episode-a")
    source_objects = dict(objects)
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=objects,
    )
    monkeypatch.setattr(
        oss_client, "bucket_name", lambda role: "raw-bucket" if role == "raw" else role
    )
    copied_targets: list[str] = []
    deleted_targets: list[str] = []

    class ProviderCopyError(OSError):
        status = 400
        code = "InvalidObject"

    def copy_object(source_bucket, source_key, target_bucket, target_key, **_kwargs):
        if source_key.endswith("metadata.json"):
            raise ProviderCopyError("source=incoming/raw/v1/tasks/... target=raw/v2/...")
        copied_targets.append(target_key)
        objects[target_key] = objects[source_key]
        return f"oss://{target_bucket}/{target_key}"

    def delete_object(bucket: str, key: str):
        assert bucket == "raw-bucket"
        deleted_targets.append(key)
        objects.pop(key, None)
        return True

    monkeypatch.setattr(oss_client, "copy_object", copy_object)
    monkeypatch.setattr(oss_client, "delete_object", delete_object)

    candidate = scan_import_candidates(db_session, import_session_id=import_session.id)[0]
    selected = import_scanned_candidate(
        db_session,
        import_session_id=import_session.id,
        candidate_id=candidate["id"],
    )
    db_session.commit()
    parse_job = db_session.get(JobRun, selected["parse_job_id"])
    assert parse_job is not None

    with pytest.raises(ValueError, match="EGO source could not be parsed"):
        import_parser.parse_import_session_original(db_session, parse_job)

    db_session.expire_all()
    failed = db_session.get(type(import_session), import_session.id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.result_json["error_code"] == "raw_copy_metadata_failed"
    assert failed.result_json["failure_stage"] == "raw_copy"
    assert failed.result_json["failed_object"] == "metadata"
    assert failed.result_json["failure_error_type"] == "ProviderCopyError"
    assert failed.result_json["failure_provider_status"] == "400"
    assert failed.result_json["failure_provider_code"] == "InvalidObject"
    assert failed.result_json["cleanup_failed"] is False
    assert failed.result_json["episode_ids"] == []
    assert failed.result_json["imported_source_count"] == 0
    assert failed.result_json["failed_source_count"] == 1
    assert "incoming/" not in json.dumps(failed.result_json)
    ledger = db_session.get(
        TaskSetSourceImport,
        db_session.get(ImportCandidate, candidate["id"]).task_set_source_import_id,
    )
    assert ledger is not None
    assert ledger.status == "failed"
    assert ledger.error_code == "raw_copy_metadata_failed"
    assert ledger.error_message == "source metadata copy failed"
    listed, total = list_import_candidates(
        db_session,
        import_session_id=import_session.id,
        source_status="failed",
        captured_ended_after=None,
        captured_ended_before=None,
        limit=50,
        offset=0,
    )
    assert total == 1
    assert listed[0]["failure_code"] == "raw_copy_metadata_failed"
    # The operation intent is committed before server-side copy. A failed
    # attempt keeps the exact Episode placeholder for a fenced retry.
    source = db_session.query(Episode).filter(Episode.import_session_id == import_session.id).one()
    raw_artifact = (
        db_session.query(EpisodeArtifact)
        .filter(
            EpisodeArtifact.episode_id == source.id,
            EpisodeArtifact.artifact_type == "raw_source",
        )
        .one()
    )
    operation = (
        db_session.query(ArtifactOperation)
        .filter(
            ArtifactOperation.artifact_id == raw_artifact.id,
            ArtifactOperation.operation_kind == "raw_source_publish",
        )
        .one()
    )
    placeholder_ids = (source.id, raw_artifact.id, operation.id)
    assert source.workflow_status == "import_failed"
    assert source.quality_status == "not_started"
    assert operation.status == "pending"
    assert (
        db_session.query(JobRun)
        .filter(
            JobRun.kind == "episode_quality",
            JobRun.resource_id == str(source.id),
        )
        .count()
        == 0
    )
    assert copied_targets == deleted_targets
    assert all(key not in objects for key in copied_targets)
    assert all(objects[key] == value for key, value in source_objects.items())

    retry = retry_import_parse(db_session, import_session_id=import_session.id)
    retry_job = db_session.get(JobRun, retry["parse_job_id"])
    assert retry["artifact_id"] is None
    assert retry_job is not None
    assert retry_job.detail_json == {
        "import_session_id": import_session.id,
        "candidate_id": candidate["id"],
        "candidate_type": "ego_episode_oss",
    }

    def successful_copy(source_bucket, source_key, target_bucket, target_key, **_kwargs):
        objects[target_key] = objects[source_key]
        return f"oss://{target_bucket}/{target_key}"

    monkeypatch.setattr(oss_client, "copy_object", successful_copy)
    original_quality_job = import_parser._get_or_create_quality_job
    monkeypatch.setattr(
        import_parser,
        "_get_or_create_quality_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("injected quality job failure")
        ),
    )
    db_session.commit()
    with pytest.raises(ValueError, match="EGO source could not be parsed"):
        import_parser.parse_import_session_original(db_session, retry_job)

    db_session.expire_all()
    interrupted_operation = db_session.get(ArtifactOperation, operation.id)
    interrupted_source = db_session.get(Episode, source.id)
    assert interrupted_operation is not None and interrupted_operation.status == "pending"
    assert interrupted_source is not None and interrupted_source.workflow_status == "import_failed"
    assert (
        db_session.query(JobRun)
        .filter(
            JobRun.kind == "episode_quality",
            JobRun.resource_id == str(source.id),
        )
        .count()
        == 0
    )
    assert set(objects) == set(source_objects)

    final_retry = retry_import_parse(db_session, import_session_id=import_session.id)
    final_retry_job = db_session.get(JobRun, final_retry["parse_job_id"])
    assert final_retry_job is not None
    monkeypatch.setattr(import_parser, "_get_or_create_quality_job", original_quality_job)
    db_session.commit()
    result = import_parser.parse_import_session_original(db_session, final_retry_job)

    db_session.expire_all()
    retried_source = (
        db_session.query(Episode).filter(Episode.import_session_id == import_session.id).one()
    )
    retried_artifact = (
        db_session.query(EpisodeArtifact)
        .filter(
            EpisodeArtifact.episode_id == retried_source.id,
            EpisodeArtifact.artifact_type == "raw_source",
        )
        .one()
    )
    retried_operation = (
        db_session.query(ArtifactOperation)
        .filter(
            ArtifactOperation.artifact_id == retried_artifact.id,
            ArtifactOperation.operation_kind == "raw_source_publish",
        )
        .one()
    )
    quality_jobs = (
        db_session.query(JobRun)
        .filter(
            JobRun.kind == "episode_quality",
            JobRun.resource_id == str(retried_source.id),
        )
        .all()
    )
    retried_ledger = db_session.get(TaskSetSourceImport, ledger.id)

    assert (retried_source.id, retried_artifact.id, retried_operation.id) == placeholder_ids
    assert retried_source.workflow_status == "quality_pending"
    assert retried_source.quality_status == "pending"
    assert retried_operation.status == "published"
    raw_target_keys = set(objects) - set(source_objects)
    assert len(raw_target_keys) == 3
    assert {key.rsplit("/", 1)[-1] for key in raw_target_keys} == {
        "data.mcap",
        "metadata.json",
        "complete.json",
    }
    assert len(quality_jobs) == 1
    assert result["_follow_up_job_ids"] == [quality_jobs[0].id]
    assert retried_ledger is not None
    assert retried_ledger.status == "imported"
    assert retried_ledger.source_episode_id == retried_source.id


def test_legacy_ego_parse_rejects_a_source_changed_after_scan_before_copying(
    db_session, monkeypatch
):
    import pytest

    from data.infra import oss_client
    from data.services import import_parser

    workspace, project, _batch, import_session = _session(db_session)
    objects = _legacy_ego_objects(task_id="task-a", device_id="device-a", episode_id="episode-a")
    _configure_legacy_ego_oss(
        monkeypatch,
        workspace_id=workspace.id,
        task_set_id=project.id,
        objects=objects,
    )
    monkeypatch.setattr(
        oss_client, "bucket_name", lambda role: "raw-bucket" if role == "raw" else role
    )
    copied: list[tuple[str, str]] = []
    monkeypatch.setattr(
        oss_client,
        "copy_object",
        lambda _source_bucket, source_key, _target_bucket, target_key, **_kwargs: copied.append(
            (source_key, target_key)
        ),
    )

    candidate = scan_import_candidates(db_session, import_session_id=import_session.id)[0]
    data_key = next(key for key in objects if key.endswith("data.mcap"))
    objects[data_key] = (b"source-mutated-after-scan", {"x-oss-meta-sha256": "f" * 64})
    selected = import_scanned_candidate(
        db_session,
        import_session_id=import_session.id,
        candidate_id=candidate["id"],
    )
    db_session.commit()
    parse_job = db_session.get(JobRun, selected["parse_job_id"])
    assert parse_job is not None

    with pytest.raises(ValueError, match="EGO source could not be parsed"):
        import_parser.parse_import_session_original(db_session, parse_job)

    db_session.expire_all()
    failed = db_session.get(type(import_session), import_session.id)
    assert failed is not None and failed.status == "failed"
    assert copied == []
    assert (
        db_session.query(Episode).filter(Episode.import_session_id == import_session.id).count()
        == 0
    )


def test_ego_import_aggregate_projects_partial_source_counts(db_session):
    from data.services import import_parser

    _workspace, project, batch, import_session = _session(db_session)
    imported_episode = Episode(
        episode_uid="partial-import-source",
        workspace_id=batch.workspace_id,
        task_set_id=project.id,
        batch_id=batch.id,
        import_session_id=import_session.id,
        kind="source",
        modality="ego",
        workflow_status="quality_pending",
        quality_status="pending",
    )
    db_session.add(imported_episode)
    db_session.flush()
    imported = TaskSetSourceImport(
        task_set_id=project.id,
        batch_id=batch.id,
        import_session_id=import_session.id,
        source_episode_id=imported_episode.id,
        source_fingerprint="p" * 64,
        source_kind="ego_oss",
        display_name="episode-imported",
        status="imported",
    )
    failed = TaskSetSourceImport(
        task_set_id=project.id,
        batch_id=batch.id,
        import_session_id=import_session.id,
        source_fingerprint="q" * 64,
        source_kind="ego_oss",
        display_name="episode-failed",
        status="failed",
        error_code="raw_copy_data_failed",
        error_message="source data copy failed",
    )
    db_session.add_all([imported, failed])
    db_session.flush()

    import_parser._refresh_source_import_aggregate(
        db_session,
        import_session=import_session,
        batch=batch,
    )
    db_session.commit()
    db_session.refresh(import_session)

    assert import_session.status == "failed"
    assert import_session.result_json["imported_source_count"] == 1
    assert import_session.result_json["failed_source_count"] == 1


def test_legacy_ego_mcap_above_oss_copy_limit_uses_opt_in_multipart_server_side_copy(
    db_session, monkeypatch
):
    from data.config import settings
    from data.infra import oss_client
    from data.services.historical_ego_import import (
        LegacyEgoImportSource,
        LegacyEgoObjectIdentity,
        copy_legacy_ego_source_object,
    )

    source_key = "incoming/raw/v1/tasks/task-a/devices/device-a/episodes/episode-a/data.mcap"
    identity = LegacyEgoObjectIdentity(
        key=source_key,
        # OSS CopyObject has a decimal 1 GB limit. This was the production
        # failure size: below 1 GiB but above the provider's direct-copy cap.
        size_bytes=1_072_588_899,
        etag="source-etag",
        crc64="",
        version_id="source-version",
    )
    source = LegacyEgoImportSource(
        legacy_task_id="task-a",
        legacy_device_id="device-a",
        legacy_episode_id="episode-a",
        metadata={},
        objects={"data": identity},
        source_fingerprint="a" * 64,
    )
    target_key = "raw/v1/workspaces/1/projects/1/batches/1/episodes/1/source/1/data.mcap"
    target: OSSObjectInfo | None = None
    multipart_calls: list[dict[str, object]] = []

    def object_info(bucket: str, key: str):
        if bucket == "source-bucket" and key == source_key:
            return OSSObjectInfo(
                size=identity.size_bytes,
                etag=identity.etag,
                crc64=None,
                version_id=identity.version_id,
                metadata={},
            )
        if bucket == "raw-bucket" and key == target_key:
            return target
        return None

    def multipart_copy_object(
        source_bucket, copied_source_key, target_bucket, copied_target_key, **kwargs
    ):
        nonlocal target
        multipart_calls.append(
            {
                "source_bucket": source_bucket,
                "source_key": copied_source_key,
                "target_bucket": target_bucket,
                "target_key": copied_target_key,
                **kwargs,
            }
        )
        target = OSSObjectInfo(
            size=identity.size_bytes,
            etag="multipart-target-etag",
            crc64=None,
            version_id="raw-target-version",
            metadata={
                "x-oss-meta-quicdata-copy-format": "ego-multipart-v1",
                "x-oss-meta-quicdata-copy-origin": str(kwargs["copy_origin"]),
            },
        )
        return f"oss://{target_bucket}/{copied_target_key}"

    monkeypatch.setattr(settings, "ego_source_oss_multipart_copy_enabled", True)
    monkeypatch.setattr(
        oss_client, "bucket_name", lambda role: "raw-bucket" if role == "raw" else role
    )
    monkeypatch.setattr(oss_client, "object_info", object_info)
    monkeypatch.setattr(oss_client, "multipart_copy_object", multipart_copy_object)
    monkeypatch.setattr(
        oss_client,
        "copy_object",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("normal copy must not handle large MCAPs")
        ),
    )

    assert (
        copy_legacy_ego_source_object(
            source_bucket="source-bucket",
            source=source,
            object_name="data",
            target_key=target_key,
        )
        is True
    )
    assert len(multipart_calls) == 1
    assert multipart_calls[0]["source_bucket"] == "source-bucket"
    assert multipart_calls[0]["source_key"] == source_key
    assert multipart_calls[0]["target_bucket"] == "raw-bucket"
    assert multipart_calls[0]["target_key"] == target_key
    assert multipart_calls[0]["source_etag"] == "source-etag"
    assert multipart_calls[0]["source_version_id"] == "source-version"
    assert len(str(multipart_calls[0]["copy_origin"])) == 64


def test_capture_mcap_above_oss_copy_limit_uses_opt_in_multipart_server_side_copy(
    db_session,
    monkeypatch,
):
    from data.config import settings
    from data.infra import oss_client
    from data.services.capture_batch_import import CaptureImportSource, copy_capture_source_object

    source_key = "incoming/raw/v2/sources/episode-a/data.mcap"
    target_key = "raw/v2/workspaces/1/task-sets/1/batches/1/episodes/1/source/1/data.mcap"
    identity = {
        "key": source_key,
        "size_bytes": 1_072_588_899,
        "etag": "source-etag",
        "crc64": "",
        "version_id": "source-version",
    }
    source = CaptureImportSource(
        episode_id="episode-a",
        metadata={},
        objects={"data": identity},
        source_fingerprint="c" * 64,
    )
    target: OSSObjectInfo | None = None
    multipart_calls: list[dict[str, object]] = []

    def object_info(bucket: str, key: str):
        if bucket == "source-bucket" and key == source_key:
            return OSSObjectInfo(
                size=int(identity["size_bytes"]),
                etag=str(identity["etag"]),
                crc64=None,
                version_id=str(identity["version_id"]),
                metadata={},
            )
        if bucket == "raw-bucket" and key == target_key:
            return target
        return None

    def multipart_copy_object(
        source_bucket, copied_source_key, target_bucket, copied_target_key, **kwargs
    ):
        nonlocal target
        multipart_calls.append(
            {
                "source_bucket": source_bucket,
                "source_key": copied_source_key,
                "target_bucket": target_bucket,
                "target_key": copied_target_key,
                **kwargs,
            }
        )
        target = OSSObjectInfo(
            size=int(identity["size_bytes"]),
            etag="multipart-target-etag",
            crc64=None,
            version_id="raw-target-version",
            metadata={
                "x-oss-meta-quicdata-copy-format": "ego-multipart-v1",
                "x-oss-meta-quicdata-copy-origin": str(kwargs["copy_origin"]),
            },
        )
        return f"oss://{target_bucket}/{copied_target_key}"

    monkeypatch.setattr(settings, "ego_source_oss_multipart_copy_enabled", True)
    monkeypatch.setattr(
        oss_client, "bucket_name", lambda role: "raw-bucket" if role == "raw" else role
    )
    monkeypatch.setattr(oss_client, "object_info", object_info)
    monkeypatch.setattr(oss_client, "multipart_copy_object", multipart_copy_object)
    monkeypatch.setattr(
        oss_client,
        "copy_object",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("normal copy must not handle large MCAPs")
        ),
    )

    assert (
        copy_capture_source_object(
            source_bucket="source-bucket",
            source=source,
            object_name="data",
            target_key=target_key,
        )
        is True
    )
    assert len(multipart_calls) == 1
    assert multipart_calls[0]["source_key"] == source_key
    assert multipart_calls[0]["source_etag"] == "source-etag"
    assert multipart_calls[0]["source_version_id"] == "source-version"
    assert len(str(multipart_calls[0]["copy_origin"])) == 64


def test_capture_multipart_copy_reuses_crc64_verified_target_without_copy_metadata(
    db_session, monkeypatch
):
    from data.config import settings
    from data.infra import oss_client
    from data.services.capture_batch_import import CaptureImportSource, copy_capture_source_object

    source_key = "incoming/raw/v2/sources/episode-a/data.mcap"
    target_key = "raw/v2/workspaces/1/task-sets/1/batches/1/episodes/1/source/1/data.mcap"
    identity = {
        "key": source_key,
        "size_bytes": 1_072_588_899,
        "etag": "source-etag",
        "crc64": "source-crc64",
        "version_id": "",
    }
    source = CaptureImportSource(
        episode_id="episode-a",
        metadata={},
        objects={"data": identity},
        source_fingerprint="c" * 64,
    )
    target: OSSObjectInfo | None = None
    multipart_calls = 0

    def object_info(bucket: str, key: str):
        if bucket == "source-bucket" and key == source_key:
            return OSSObjectInfo(
                size=int(identity["size_bytes"]),
                etag=str(identity["etag"]),
                crc64=str(identity["crc64"]),
                version_id=None,
                metadata={"x-oss-meta-sha256": "source-proof"},
            )
        if bucket == "raw-bucket" and key == target_key:
            return target
        return None

    def multipart_copy_object(*_args, **_kwargs):
        nonlocal multipart_calls, target
        multipart_calls += 1
        # UploadPartCopy can preserve source user metadata instead of the
        # metadata supplied at multipart initialization. CRC64 still proves
        # the copied bytes and is the only durable verification signal here.
        target = OSSObjectInfo(
            size=int(identity["size_bytes"]),
            etag="multipart-target-etag",
            crc64=str(identity["crc64"]),
            version_id=None,
            metadata={"x-oss-meta-sha256": "source-proof"},
        )
        return f"oss://raw-bucket/{target_key}"

    monkeypatch.setattr(settings, "source_oss_multipart_copy_enabled", True)
    monkeypatch.setattr(
        oss_client, "bucket_name", lambda role: "raw-bucket" if role == "raw" else role
    )
    monkeypatch.setattr(oss_client, "object_info", object_info)
    monkeypatch.setattr(oss_client, "multipart_copy_object", multipart_copy_object)

    assert (
        copy_capture_source_object(
            source_bucket="source-bucket",
            source=source,
            object_name="data",
            target_key=target_key,
        )
        is True
    )
    assert (
        copy_capture_source_object(
            source_bucket="source-bucket",
            source=source,
            object_name="data",
            target_key=target_key,
        )
        is False
    )
    assert multipart_calls == 1

    target = OSSObjectInfo(
        size=int(identity["size_bytes"]),
        etag="multipart-target-etag",
        crc64="different-crc64",
        version_id=None,
        metadata={"x-oss-meta-sha256": "source-proof"},
    )
    with pytest.raises(ValueError, match="capture raw target identity"):
        copy_capture_source_object(
            source_bucket="source-bucket",
            source=source,
            object_name="data",
            target_key=target_key,
        )


def test_legacy_ego_multipart_copy_reuses_crc64_verified_target_without_copy_metadata(
    db_session, monkeypatch
):
    from data.config import settings
    from data.infra import oss_client
    from data.services.historical_ego_import import (
        LegacyEgoImportSource,
        LegacyEgoObjectIdentity,
        copy_legacy_ego_source_object,
    )

    source_key = "incoming/raw/v1/tasks/task-a/devices/device-a/episodes/episode-a/data.mcap"
    target_key = "raw/v1/workspaces/1/projects/1/batches/1/episodes/1/source/1/data.mcap"
    identity = LegacyEgoObjectIdentity(
        key=source_key,
        size_bytes=1_072_588_899,
        etag="source-etag",
        crc64="source-crc64",
        version_id="",
    )
    source = LegacyEgoImportSource(
        legacy_task_id="task-a",
        legacy_device_id="device-a",
        legacy_episode_id="episode-a",
        metadata={},
        objects={"data": identity},
        source_fingerprint="c" * 64,
    )
    target: OSSObjectInfo | None = None
    multipart_calls = 0

    def object_info(bucket: str, key: str):
        if bucket == "source-bucket" and key == source_key:
            return OSSObjectInfo(
                size=identity.size_bytes,
                etag=identity.etag,
                crc64=identity.crc64,
                version_id=None,
                metadata={"x-oss-meta-sha256": "source-proof"},
            )
        if bucket == "raw-bucket" and key == target_key:
            return target
        return None

    def multipart_copy_object(*_args, **_kwargs):
        nonlocal multipart_calls, target
        multipart_calls += 1
        # OSS UploadPartCopy can retain source metadata rather than the
        # InitiateMultipartUpload metadata. CRC64 still proves the bytes.
        target = OSSObjectInfo(
            size=identity.size_bytes,
            etag="multipart-target-etag",
            crc64=identity.crc64,
            version_id=None,
            metadata={"x-oss-meta-sha256": "source-proof"},
        )
        return f"oss://raw-bucket/{target_key}"

    monkeypatch.setattr(settings, "ego_source_oss_multipart_copy_enabled", True)
    monkeypatch.setattr(
        oss_client, "bucket_name", lambda role: "raw-bucket" if role == "raw" else role
    )
    monkeypatch.setattr(oss_client, "object_info", object_info)
    monkeypatch.setattr(oss_client, "multipart_copy_object", multipart_copy_object)

    assert (
        copy_legacy_ego_source_object(
            source_bucket="source-bucket",
            source=source,
            object_name="data",
            target_key=target_key,
        )
        is True
    )
    assert (
        copy_legacy_ego_source_object(
            source_bucket="source-bucket",
            source=source,
            object_name="data",
            target_key=target_key,
        )
        is False
    )
    assert multipart_calls == 1

    target = OSSObjectInfo(
        size=identity.size_bytes,
        etag="multipart-target-etag",
        crc64="different-crc64",
        version_id=None,
        metadata={"x-oss-meta-sha256": "source-proof"},
    )
    with pytest.raises(ValueError, match="raw target identity"):
        copy_legacy_ego_source_object(
            source_bucket="source-bucket",
            source=source,
            object_name="data",
            target_key=target_key,
        )


def test_capture_copy_rejects_existing_raw_target_with_wrong_crc64(
    db_session,
    monkeypatch,
):
    import pytest

    from data.infra import oss_client
    from data.services.capture_batch_import import CaptureImportSource, copy_capture_source_object

    source_key = "incoming/raw/v2/sources/episode-a/metadata.json"
    target_key = "raw/v2/workspaces/1/task-sets/1/batches/1/episodes/1/source/1/metadata.json"
    identity = {
        "key": source_key,
        "size_bytes": 42,
        "etag": "source-etag",
        "crc64": "123",
        "version_id": "source-version",
    }
    source = CaptureImportSource(
        episode_id="episode-a",
        metadata={},
        objects={"metadata": identity},
        source_fingerprint="c" * 64,
    )

    def object_info(bucket: str, key: str):
        if bucket == "source-bucket" and key == source_key:
            return OSSObjectInfo(
                size=42,
                etag="source-etag",
                crc64="123",
                version_id="source-version",
                metadata={},
            )
        if bucket == "raw-bucket" and key == target_key:
            return OSSObjectInfo(
                size=42,
                etag="source-etag",
                crc64="456",
                version_id="source-version",
                metadata={},
            )
        return None

    monkeypatch.setattr(
        oss_client, "bucket_name", lambda role: "raw-bucket" if role == "raw" else role
    )
    monkeypatch.setattr(oss_client, "object_info", object_info)
    monkeypatch.setattr(
        oss_client,
        "copy_object",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("incompatible target must not be copied over")
        ),
    )

    with pytest.raises(ValueError, match="raw target identity"):
        copy_capture_source_object(
            source_bucket="source-bucket",
            source=source,
            object_name="metadata",
            target_key=target_key,
        )


def test_legacy_ego_copy_never_overwrites_an_incompatible_raw_target(db_session, monkeypatch):
    import pytest

    from data.infra import oss_client
    from data.services.historical_ego_import import (
        LegacyEgoImportSource,
        LegacyEgoObjectIdentity,
        copy_legacy_ego_source_object,
    )

    source_key = "incoming/raw/v1/tasks/task-a/devices/device-a/episodes/episode-a/data.mcap"
    target_key = "raw/v1/workspaces/1/projects/1/batches/1/episodes/1/source/1/data.mcap"
    identity = LegacyEgoObjectIdentity(
        key=source_key,
        size_bytes=42,
        etag="source-etag",
        crc64="",
        version_id="source-version",
    )
    source = LegacyEgoImportSource(
        legacy_task_id="task-a",
        legacy_device_id="device-a",
        legacy_episode_id="episode-a",
        metadata={},
        objects={"data": identity},
        source_fingerprint="b" * 64,
    )

    def object_info(bucket: str, key: str):
        if bucket == "source-bucket" and key == source_key:
            return OSSObjectInfo(42, "source-etag", None, "source-version", {})
        if bucket == "raw-bucket" and key == target_key:
            return OSSObjectInfo(42, "different-etag", None, "raw-version", {})
        return None

    monkeypatch.setattr(
        oss_client, "bucket_name", lambda role: "raw-bucket" if role == "raw" else role
    )
    monkeypatch.setattr(oss_client, "object_info", object_info)
    monkeypatch.setattr(
        oss_client,
        "copy_object",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("existing target must not be overwritten")
        ),
    )

    with pytest.raises(ValueError, match="raw target identity"):
        copy_legacy_ego_source_object(
            source_bucket="source-bucket",
            source=source,
            object_name="data",
            target_key=target_key,
        )


def test_legacy_ego_copy_accepts_cross_bucket_target_with_changed_etag_and_matching_crc64(
    monkeypatch,
):
    from data.infra import oss_client
    from data.services.historical_ego_import import (
        LegacyEgoImportSource,
        LegacyEgoObjectIdentity,
        copy_legacy_ego_source_object,
    )

    source_key = "incoming/raw/v1/tasks/task-a/devices/device-a/episodes/episode-a/data.mcap"
    target_key = "raw/v2/workspaces/1/task-sets/1/batches/1/episodes/1/source/1/data.mcap"
    identity = LegacyEgoObjectIdentity(
        key=source_key,
        size_bytes=42,
        etag="source-multipart-etag-2",
        crc64="1234567890123456789",
        version_id="source-version",
    )
    source = LegacyEgoImportSource(
        legacy_task_id="task-a",
        legacy_device_id="device-a",
        legacy_episode_id="episode-a",
        metadata={},
        objects={"data": identity},
        source_fingerprint="c" * 64,
    )
    target: OSSObjectInfo | None = None
    copy_calls: list[tuple[str, str, str, str]] = []

    def object_info(bucket: str, key: str):
        if bucket == "source-bucket" and key == source_key:
            return OSSObjectInfo(
                size=identity.size_bytes,
                etag=identity.etag,
                crc64=identity.crc64,
                version_id=identity.version_id,
                metadata={},
            )
        if bucket == "raw-bucket" and key == target_key:
            return target
        return None

    def copy_object(
        source_bucket: str,
        copied_source_key: str,
        target_bucket: str,
        copied_target_key: str,
        **_kwargs,
    ):
        nonlocal target
        copy_calls.append((source_bucket, copied_source_key, target_bucket, copied_target_key))
        # OSS may re-materialize a multipart source as a single-part target,
        # producing a different ETag despite an identical byte stream.
        target = OSSObjectInfo(
            size=identity.size_bytes,
            etag="target-copy-etag",
            crc64=identity.crc64,
            version_id="target-version",
            metadata={},
        )
        return f"oss://{target_bucket}/{copied_target_key}"

    monkeypatch.setattr(
        oss_client, "bucket_name", lambda role: "raw-bucket" if role == "raw" else role
    )
    monkeypatch.setattr(oss_client, "object_info", object_info)
    monkeypatch.setattr(oss_client, "copy_object", copy_object)

    assert (
        copy_legacy_ego_source_object(
            source_bucket="source-bucket",
            source=source,
            object_name="data",
            target_key=target_key,
        )
        is True
    )
    assert (
        copy_legacy_ego_source_object(
            source_bucket="source-bucket",
            source=source,
            object_name="data",
            target_key=target_key,
        )
        is False
    )
    assert copy_calls == [("source-bucket", source_key, "raw-bucket", target_key)]


def test_legacy_ego_copy_cleans_new_target_when_post_copy_identity_check_fails(monkeypatch):
    import pytest

    from data.infra import oss_client
    from data.services.historical_ego_import import (
        LegacyEgoImportSource,
        LegacyEgoObjectIdentity,
        copy_legacy_ego_source_object,
    )

    source_key = "incoming/raw/v1/tasks/task-a/devices/device-a/episodes/episode-a/data.mcap"
    target_key = "raw/v2/workspaces/1/task-sets/1/batches/1/episodes/1/source/1/data.mcap"
    identity = LegacyEgoObjectIdentity(
        key=source_key,
        size_bytes=42,
        etag="source-etag",
        crc64="1234567890123456789",
        version_id="source-version",
    )
    source = LegacyEgoImportSource(
        legacy_task_id="task-a",
        legacy_device_id="device-a",
        legacy_episode_id="episode-a",
        metadata={},
        objects={"data": identity},
        source_fingerprint="d" * 64,
    )
    target: OSSObjectInfo | None = None
    deleted_targets: list[str] = []

    def object_info(bucket: str, key: str):
        if bucket == "source-bucket" and key == source_key:
            return OSSObjectInfo(
                size=identity.size_bytes,
                etag=identity.etag,
                crc64=identity.crc64,
                version_id=identity.version_id,
                metadata={},
            )
        if bucket == "raw-bucket" and key == target_key:
            return target
        return None

    def copy_object(
        _source_bucket: str, _source_key: str, _target_bucket: str, _target_key: str, **_kwargs
    ):
        nonlocal target
        target = OSSObjectInfo(
            size=identity.size_bytes,
            etag="target-copy-etag",
            crc64="different-crc64",
            version_id="target-version",
            metadata={},
        )
        return f"oss://raw-bucket/{target_key}"

    def delete_object(bucket: str, key: str):
        nonlocal target
        assert bucket == "raw-bucket"
        assert key == target_key
        deleted_targets.append(key)
        target = None
        return True

    monkeypatch.setattr(
        oss_client, "bucket_name", lambda role: "raw-bucket" if role == "raw" else role
    )
    monkeypatch.setattr(oss_client, "object_info", object_info)
    monkeypatch.setattr(oss_client, "copy_object", copy_object)
    monkeypatch.setattr(oss_client, "delete_object", delete_object)

    with pytest.raises(ValueError, match="raw target identity"):
        copy_legacy_ego_source_object(
            source_bucket="source-bucket",
            source=source,
            object_name="data",
            target_key=target_key,
        )

    assert deleted_targets == [target_key]
    assert target is None
