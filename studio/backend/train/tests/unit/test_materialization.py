"""Workstream A — dataset materialization controller."""

from pathlib import Path

from quictrain_core import DatasetReadiness, MaterializationState
from quictrain_scheduler.materializer import Materializer, request_materialization
from tests.db_helpers import rebind_database

import quictrain_api.db as db
from quictrain_api.db import DatasetVersionRecord, init_database
from quictrain_api.service import seed_catalog


def test_materialize_file_dataset_to_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/mat.db")
    monkeypatch.setenv("QUICTRAIN_MATERIALIZATION_ROOT", str(tmp_path / "datasets"))
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    rebind_database()
    init_database()
    with db.SessionLocal() as session:
        seed_catalog(session)

    source = tmp_path / "source"
    source.mkdir()
    payload = source / "meta.json"
    payload.write_text('{"ok": true}', encoding="utf-8")

    with db.SessionLocal() as session:
        record = DatasetVersionRecord(
            id="dsv_mat_test",
            project_id="prj_robot_arm",
            external_dataset_id="mat_test",
            name="物化测试",
            version="v1",
            status=DatasetReadiness.REGISTERED.value,
            format="lerobot",
            format_version="3.0",
            uri=f"file://{source}",
            checksum="generated:mat-test",
            manifest={
                "id": "dsv_mat_test",
                "dataset_id": "mat_test",
                "name": "物化测试",
                "version": "v1",
                "status": "REGISTERED",
                "format": "lerobot",
                "format_version": "3.0",
                "uri": f"file://{source}",
                "checksum": "generated:mat-test",
                "episodes": 1,
                "frames": 10,
                "duration_hours": 0.01,
                "fps": 10,
                "robot_type": "pusht",
                "camera_keys": ["observation.image"],
                "action_dim": 2,
                "state_dim": 2,
                "language_tasks": False,
            },
        )
        session.add(record)
        session.commit()

        first = request_materialization(session, record, actor_id="usr_demo")
        second = request_materialization(session, record, actor_id="usr_demo")
        assert first.id == second.id
        session.commit()

    materializer = Materializer(db.SessionLocal)
    assert materializer.run_once() is True

    with db.SessionLocal() as session:
        record = session.get(DatasetVersionRecord, "dsv_mat_test")
        assert record is not None
        assert record.status == DatasetReadiness.READY.value
        assert record.materialized_uri
        assert Path(record.materialized_uri).joinpath("meta.json").exists()

        again = request_materialization(session, record, actor_id="usr_demo")
        session.commit()
        assert again.state == MaterializationState.SUCCEEDED.value
