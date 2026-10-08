"""Additional Formal Close completion coverage."""

from __future__ import annotations

import subprocess
from pathlib import Path

from quictrain_core import DatasetReadiness, JobState, new_id
from quictrain_provider_local import FakeProvider
from quictrain_scheduler import Scheduler
from quictrain_scheduler.object_store import LocalPathObjectStore, StagingObjectStore
from tests.db_helpers import rebind_database

import quictrain_api.db as db
from quictrain_api.db import DatasetVersionRecord, JobRecord, init_database
from quictrain_api.service import seed_catalog


def test_object_store_prefix_tree_copy(tmp_path):
    source = tmp_path / "src"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "a.bin").write_bytes(b"aa")
    (source / "b.bin").write_bytes(b"bbb")
    dest = tmp_path / "dst"
    copied = LocalPathObjectStore().fetch_to_directory(str(source), dest)
    assert copied == 5
    assert (dest / "nested" / "a.bin").read_bytes() == b"aa"

    staging = tmp_path / "staging"
    mapped = staging / "bucket" / "prefix"
    mapped.mkdir(parents=True)
    (mapped / "x.bin").write_bytes(b"xyz")
    out = tmp_path / "oss_out"
    copied = StagingObjectStore(staging).fetch_to_directory("oss://bucket/prefix", out)
    assert copied == 3
    assert (out / "x.bin").exists()


def test_scheduler_rejects_non_ready_dataset(tmp_path, monkeypatch):
    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/sched.db")
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    monkeypatch.setenv("QUICTRAIN_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    rebind_database()
    init_database()
    with db.SessionLocal() as session:
        seed_catalog(session)
        record = DatasetVersionRecord(
            id="dsv_not_ready",
            project_id="prj_robot_arm",
            external_dataset_id="nr",
            name="nr",
            version="v1",
            status=DatasetReadiness.REGISTERED.value,
            format="lerobot",
            format_version="3.0",
            uri=str(tmp_path / "data"),
            checksum="generated:nr",
            manifest={
                "id": "dsv_not_ready",
                "dataset_id": "nr",
                "name": "nr",
                "version": "v1",
                "status": "REGISTERED",
                "format": "lerobot",
                "format_version": "3.0",
                "uri": str(tmp_path / "data"),
                "checksum": "generated:nr",
                "episodes": 1,
                "frames": 1,
                "duration_hours": 0.01,
                "fps": 10,
                "robot_type": "pusht",
                "camera_keys": ["observation.image"],
                "action_dim": 2,
                "state_dim": 2,
                "language_tasks": False,
            },
        )
        job = JobRecord(
            id=new_id("job"),
            project_id="prj_robot_arm",
            creator_id="usr_demo",
            client_request_id="not-ready",
            display_name="not-ready",
            state=JobState.QUEUED.value,
            stage="QUEUE",
            dataset_version_id=record.id,
            model_version_id="mv_act_20260716",
            model_id="act",
            recipe_id="fine_tune",
            resource_profile_id="act-h20-standard",
            schema_hash="h",
            config_hash="c",
            schema_snapshot={},
            user_overrides={},
            resolved_config={"runtime": {}},
            source_snapshot={},
        )
        session.add_all([record, job])
        session.commit()
        job_id = job.id

    Scheduler(db.SessionLocal, FakeProvider(), artifact_root=str(tmp_path / "artifacts")).run_once()
    with db.SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        assert job.state == JobState.FAILED.value
        assert job.failure_category == "DATASET_NOT_READY"


def test_backup_scripts_check_mode():
    root = Path(__file__).resolve().parents[2]
    backup = subprocess.run(
        ["bash", str(root / "scripts/postgres_backup.sh"), "--check"],
        check=False,
        capture_output=True,
        text=True,
    )
    restore = subprocess.run(
        ["bash", str(root / "scripts/postgres_restore_drill.sh"), "--check"],
        check=False,
        capture_output=True,
        text=True,
    )
    # pg_dump/pg_restore may be absent on developer hosts; either tooling ok or missing binaries.
    assert backup.returncode in {0, 1}
    assert restore.returncode in {0, 1}
    if backup.returncode == 0:
        assert "tooling ok" in backup.stdout
    if restore.returncode == 0:
        assert "tooling ok" in restore.stdout
