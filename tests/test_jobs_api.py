"""Read/write queue API stays local and exposes only queue state."""
from __future__ import annotations

from fastapi.testclient import TestClient

from robodata.web import create_app


def _plan(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    return {"schema_version": 1,
            "input": {"path": str(source), "label": "api queue", "source": "auto"},
            "annotations": {"task_template": "episode {episode_index}",
                            "default_outcome": "success"},
            "review": {"approve": True}, "delivery": {"package": False},
            "timeout_seconds": 60}


def test_jobs_api_submit_list_cancel_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBODATA_BUSINESS_ROOT", str(tmp_path / "business"))
    client = TestClient(create_app())
    submitted = client.post("/api/jobs", json={"plan": _plan(tmp_path)})
    assert submitted.status_code == 202
    job_id = submitted.json()["job"]["id"]
    assert submitted.json()["job"]["state"] == "queued"

    listing = client.get("/api/jobs")
    assert listing.status_code == 200
    assert listing.json()["summary"]["total"] == 1
    detail = client.get(f"/api/jobs/{job_id}")
    assert detail.status_code == 200
    assert detail.json()["state"] == "queued"
    assert detail.json()["events"][0]["event"] == "queued"

    canceled = client.post(f"/api/jobs/{job_id}/cancel")
    assert canceled.status_code == 200
    assert canceled.json()["state"] == "canceled"
    retried = client.post(f"/api/jobs/{job_id}/retry")
    assert retried.status_code == 200
    assert retried.json()["state"] == "queued"
    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 400
    assert client.get("/api/jobs/missing").status_code == 404


def test_status_api_reports_queue_and_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBODATA_BUSINESS_ROOT", str(tmp_path / "business"))
    client = TestClient(create_app())
    result = client.get("/api/status")
    assert result.status_code == 200
    value = result.json()
    assert "queue" in value and "disk" in value and "gc" in value
