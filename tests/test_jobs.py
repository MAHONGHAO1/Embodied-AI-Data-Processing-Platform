"""Durable local queue: submission, leases, cancellation, retry, and worker."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from robodata.jobs import JobStore
from robodata.worker import run_once


def _plan(tmp_path: Path) -> dict:
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    return {
        "schema_version": 1,
        "input": {"path": str(source), "label": "queue test", "source": "auto"},
        "annotations": {"task_template": "episode {episode_index}",
                        "default_outcome": "success"},
        "review": {"approve": True},
        "delivery": {"package": False},
        "timeout_seconds": 60,
    }


def test_submit_is_idempotent_and_persists_plan(tmp_path):
    store = JobStore(tmp_path / "queue")
    first = store.submit(_plan(tmp_path), business_root=tmp_path / "business")
    second = store.submit(_plan(tmp_path), business_root=tmp_path / "business")
    assert first["status"] == "queued"
    assert second["status"] == "existing"
    assert first["job"]["id"] == second["job"]["id"]
    job = store.get(first["job"]["id"])
    assert job["state"] == "queued"
    assert store.events(first["job"]["id"])[0]["event"] == "queued"
    snapshot = store.job_dir(first["job"]["id"]) / "plan.json"
    assert json.loads(snapshot.read_text(encoding="utf-8"))["input"]["label"] == "queue test"


def test_claim_heartbeat_finish_and_retry(tmp_path):
    store = JobStore(tmp_path / "queue")
    job_id = store.submit(_plan(tmp_path))["job"]["id"]
    claimed = store.claim("worker-a", lease_seconds=30)
    assert claimed["id"] == job_id and claimed["state"] == "running"
    assert claimed["adopt"] is False
    assert store.heartbeat(job_id, "worker-a", 30) is False
    assert store.finish(job_id, "worker-a", "failed", result={"exit_code": 2}, error="rejected",
                        exit_code=2)
    assert store.get(job_id)["state"] == "failed"
    assert store.get(job_id)["result"]["exit_code"] == 2
    retried = store.retry(job_id)
    assert retried["state"] == "queued"
    assert store.claim("worker-b", 30)["id"] == job_id


def test_queued_and_running_cancel(tmp_path):
    store = JobStore(tmp_path / "queue")
    job_id = store.submit(_plan(tmp_path))["job"]["id"]
    assert store.cancel(job_id)["state"] == "canceled"
    assert store.claim("worker", 30) is None

    other = store.submit({**_plan(tmp_path), "input": {**_plan(tmp_path)["input"],
                                                      "label": "running"}})["job"]["id"]
    store.claim("worker", 30)
    canceled = store.cancel(other)
    assert canceled["cancel_requested"] is True
    assert canceled["state"] == "running"


def test_expired_lease_with_live_child_is_adopted(tmp_path):
    import os
    from robodata.runtime import _identity

    store = JobStore(tmp_path / "queue")
    job_id = store.submit(_plan(tmp_path))["job"]["id"]
    store.claim("first-worker", 30)
    pid, identity = os.getpid(), _identity(os.getpid())
    store.set_pid(job_id, "first-worker", pid, identity)
    with store.connect() as connection:
        connection.execute("UPDATE jobs SET lease_expires=0 WHERE id=?", (job_id,))
    adopted = store.claim("second-worker", 30)
    assert adopted["id"] == job_id
    assert adopted["adopt"] is True
    assert adopted["attempts"] == 1


def test_expired_lease_requeues_or_fails_by_attempt_limit(tmp_path):
    store = JobStore(tmp_path / "queue")
    job_id = store.submit(_plan(tmp_path), max_attempts=2)["job"]["id"]
    store.claim("dead-worker", 30)
    with store.connect() as connection:  # simulate a worker that died before spawning
        connection.execute("UPDATE jobs SET lease_expires=0, pid=NULL WHERE id=?", (job_id,))
    claimed = store.claim("next-worker", 30)
    assert claimed["id"] == job_id and claimed["adopt"] is False
    with store.connect() as connection:
        connection.execute("UPDATE jobs SET lease_expires=0, pid=NULL WHERE id=?", (job_id,))
    assert store.claim("last-worker", 30) is None
    assert store.get(job_id)["state"] == "failed"


def test_expired_cancel_request_is_not_started_again(tmp_path):
    store = JobStore(tmp_path / "queue")
    job_id = store.submit(_plan(tmp_path))["job"]["id"]
    store.claim("dead-worker", 30)
    store.cancel(job_id)
    with store.connect() as connection:
        connection.execute("UPDATE jobs SET lease_expires=0, pid=NULL WHERE id=?", (job_id,))
    assert store.claim("next-worker", 30) is None
    assert store.get(job_id)["state"] == "canceled"


def test_worker_runs_a_child_and_records_result(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "queue")
    job_id = store.submit(_plan(tmp_path))["job"]["id"]

    class FakeProcess:
        pid = 999999
        returncode = 0

        def poll(self):
            return 0

    def fake_spawn(directory, row):
        (directory / "result.json").write_text(json.dumps(
            {"status": "passed", "exit_code": 0, "message": "ok"}), encoding="utf-8")
        return FakeProcess()

    monkeypatch.setattr("robodata.worker._spawn", fake_spawn)
    result = run_once(store, owner="test-worker", poll_seconds=0.01)
    assert result["state"] == "succeeded"
    assert store.get(job_id)["result"]["status"] == "passed"

