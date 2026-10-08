"""Teleop quality should infer a reference camera when sensors.cameras is empty."""

from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path

from collector_fixtures import make_collector
from mcap.writer import Writer

from data.services.import_parser import _multimodal_projection, _quality_metrics


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


def test_quality_metrics_infers_camera_topic_without_sensors_metadata(tmp_path: Path):
    data_file = tmp_path / "data.mcap"
    data_file.write_bytes(_mcap_bytes(topic="/camera/wrist/rgb", frame_count=10))
    metadata = {
        "qrdf_version": "0.2.0",
        "episode_id": "teleop_source",
        "data_file": "data.mcap",
        "timing": {
            "start_timestamp_ns": 1_000_000_000,
            "end_timestamp_ns": 1_000_000_000 + 9 * 33_333_333,
            "duration_s": 0.3,
        },
        "sensors": {"cameras": []},
    }

    facts = _quality_metrics(metadata, data_file=data_file)

    assert facts.reference_topic == "/camera/wrist/rgb"
    assert facts.metrics["reference_frame_count"] == 10
    assert facts.timing["start_timestamp_ns"] == "1000000000"
    multimodal = _multimodal_projection(metadata, reference_topic=facts.reference_topic)
    assert multimodal["streams"] == [
        {
            "id": "/camera/wrist/rgb",
            "kind": "camera",
            "width": None,
            "height": None,
            "fps": None,
        }
    ]


def test_quality_metrics_prefers_declared_sensors_camera(tmp_path: Path):
    data_file = tmp_path / "data.mcap"
    data_file.write_bytes(_mcap_bytes(topic="/camera/front/rgb", frame_count=5))
    metadata = {
        "qrdf_version": "0.2.0",
        "episode_id": "teleop_source",
        "data_file": "data.mcap",
        "topics": [{"name": "/camera/side/rgb"}],
        "sensors": {"cameras": [{"name": "front", "topic": "/camera/front/rgb", "fps": 30.0}]},
    }

    facts = _quality_metrics(metadata, data_file=data_file)

    assert facts.reference_topic == "/camera/front/rgb"
    assert facts.metrics["reference_frame_count"] == 5


def test_episode_quality_without_sensors_queues_preview_job(db_session, tmp_path, monkeypatch):
    from data.config import settings
    from data.database import (
        Batch,
        CollectionDevice,
        Episode,
        JobRun,
        TaskLabel,
        TaskSet,
        User,
        WorkItem,
        Workspace,
    )
    from data.services.import_intake import (
        complete_import_upload,
        materialize_import_upload,
        start_import_upload,
        write_import_chunk,
    )
    from data.services.import_sessions import create_import_session
    from data.tasks.batch_workers import batch_execute_task

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    actor = db_session.query(User).order_by(User.id).first()
    workspace = Workspace(name=f"teleop quality workspace {tmp_path.name}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    project = TaskSet(workspace_id=workspace.id, name="teleop quality project")
    db_session.add(project)
    db_session.flush()
    task_label = TaskLabel(key=f"teleop-quality-{workspace.id}", name="pick object")
    collector = make_collector(db_session, workspace_id=workspace.id, name="Collector")
    device = CollectionDevice(
        workspace_id=workspace.id,
        name="Arm Station",
        device_type="collection_station",
        serial_number=f"TELEOP-{workspace.id}",
    )
    db_session.add_all((task_label, collector, device))
    db_session.flush()
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="teleop quality batch",
        batch_type="teleop",
        created_by_user_id=actor.id,
    )
    db_session.add(batch)
    db_session.flush()
    import_session = create_import_session(
        db_session,
        batch_id=batch.id,
        import_type="chunked_upload",
        actor_id=actor.id,
        task_label_id=task_label.id,
        default_collector_profile_id=collector.id,
        default_collection_device_id=device.id,
    )
    db_session.commit()

    archive = tmp_path / "teleop-bare.zip"
    metadata = {
        "qrdf_version": "0.2.0",
        "episode_id": "episode_teleop_bare",
        "data_file": "data.mcap",
        "timing": {
            "start_timestamp_ns": 1_000_000_000,
            "end_timestamp_ns": 2_000_000_000,
            "duration_s": 1.0,
        },
        "sensors": {"cameras": []},
    }
    with zipfile.ZipFile(archive, "w") as opened:
        opened.writestr("episodes/episode_teleop_bare/metadata.json", json.dumps(metadata))
        opened.writestr(
            "episodes/episode_teleop_bare/data.mcap",
            _mcap_bytes(topic="/camera/front/rgb", frame_count=30),
        )

    start_import_upload(
        db_session, import_session_id=import_session.id, file_name=archive.name, total_chunks=1
    )
    write_import_chunk(
        db_session,
        import_session_id=import_session.id,
        chunk_index=0,
        content=archive.read_bytes(),
    )
    queued = complete_import_upload(db_session, import_session_id=import_session.id)
    db_session.commit()
    materialize_job = db_session.get(JobRun, queued["materialize_job_id"])
    assert materialize_job is not None
    completed = materialize_import_upload(db_session, materialize_job)
    db_session.commit()
    batch_execute_task.run(completed["parse_job_id"])
    source = db_session.query(Episode).filter(Episode.import_session_id == import_session.id).one()
    quality_job = (
        db_session.query(JobRun)
        .filter(JobRun.kind == "episode_quality", JobRun.resource_id == str(source.id))
        .one()
    )

    result = batch_execute_task.run(quality_job.id, quality_job.recovery_dispatch_token)

    db_session.refresh(source)
    assert result["status"] == "succeeded"
    assert source.quality_status == "passed"
    assert source.modality == "teleop"
    assert source.metadata_json["reference_topic"] == "/camera/front/rgb"
    assert source.metadata_json["multimodal"]["streams"][0]["id"] == "/camera/front/rgb"
    assert (
        db_session.query(WorkItem)
        .filter(WorkItem.episode_id == source.id, WorkItem.kind == "cut")
        .count()
        == 1
    )
    assert (
        db_session.query(JobRun)
        .filter(JobRun.kind == "episode_preview", JobRun.resource_id == str(source.id))
        .count()
        == 1
    )
