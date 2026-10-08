import time
from uuid import uuid4

from fastapi.testclient import TestClient

from quictrain_api.main import app


def request_payload(client_request_id: str = "test-create-1") -> dict:
    return {
        "project_id": "prj_robot_arm",
        "dataset_version_id": "dsv_kitchen_v17",
        "model_version_id": "mv_act_20260716",
        "recipe_id": "fine_tune",
        "config_overrides": {"training.steps": 1000, "training.batch_size": 1},
        "resource_selection": {"mode": "AUTO", "profile": "act-h20-standard"},
        "client_request_id": client_request_id,
        "display_name": "act-integration-test",
    }


def test_catalog_and_validation_contract(example_dataset_seed):
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        body = client.get("/health").json()
        assert body["status"] == "healthy"
        assert body["version"] == "1.0.0"
        datasets = client.get("/api/v1/datasets").json()["items"]
        assert len(datasets) >= 4
        smoke = next(item for item in datasets if item["id"] == "dsv_pusht_smoke_v1")
        assert smoke["uri"].endswith("quictrain-pusht-smoke-v1")
        assert smoke["episodes"] == 4
        assert {item["id"] for item in client.get("/api/v1/models").json()["items"]} == {
            "act",
            "pi05",
        }
        payload = request_payload("validation-only")
        payload.pop("client_request_id")
        payload.pop("display_name")
        result = client.post("/api/v1/jobs/validate", json=payload)
        assert result.status_code == 200
        assert result.json()["valid"] is True


def test_job_submission_is_idempotent_and_reaches_success(monkeypatch, example_dataset_seed):
    with TestClient(app) as client:
        payload = request_payload(f"integration-idempotent-{uuid4().hex}")
        first = client.post(
            "/api/v1/jobs",
            json=payload,
            headers={"Idempotency-Key": payload["client_request_id"]},
        )
        second = client.post(
            "/api/v1/jobs",
            json=payload,
            headers={"Idempotency-Key": payload["client_request_id"]},
        )
        assert first.status_code == 202
        assert first.json()["job_id"] == second.json()["job_id"]
        job_id = first.json()["job_id"]
        deadline = time.time() + 8
        state = None
        while time.time() < deadline:
            state = client.get(f"/api/v1/jobs/{job_id}").json()["state"]
            if state == "SUCCEEDED":
                break
            time.sleep(0.25)
        assert state == "SUCCEEDED"
        detail = client.get(f"/api/v1/jobs/{job_id}").json()
        assert detail["artifacts"]
        assert detail["events"][-1]["type"] == "job.state_changed"
        first_logs = client.get(f"/api/v1/jobs/{job_id}/logs?cursor=0").json()
        assert first_logs["eof"] is True
        assert len(first_logs["lines"]) == 6
        assert first_logs["next_cursor"] == 6
        assert (
            client.get(f"/api/v1/jobs/{job_id}/logs?cursor={first_logs['next_cursor']}").json()[
                "lines"
            ]
            == []
        )

        class MemoryArtifactClient:
            def presign_get(self, uri, expires_seconds=900, **kwargs):
                assert uri.startswith("oss://")
                assert expires_seconds == 900
                assert kwargs["download_name"]
                assert "content_type" not in kwargs
                return "https://artifacts.example.test/signed"

            def read_bytes(self, uri, max_bytes=None):
                assert uri.endswith("summary.json")
                assert max_bytes == 2 * 1024 * 1024
                return b'{"status":"ok"}'

        monkeypatch.setattr(
            "quictrain_api.main.get_artifact_client", lambda: MemoryArtifactClient()
        )
        summary = next(item for item in detail["artifacts"] if item["name"] == "summary.json")
        download = client.get(f"/api/v1/artifacts/{summary['id']}/download", follow_redirects=False)
        assert download.status_code == 307
        assert download.headers["location"] == "https://artifacts.example.test/signed"
        preview = client.get(f"/api/v1/artifacts/{summary['id']}/preview")
        assert preview.status_code == 200
        assert preview.json()["content"] == '{\n  "status": "ok"\n}'


def test_forbidden_system_override_has_structured_error(example_dataset_seed):
    with TestClient(app) as client:
        payload = request_payload("forbidden-config")
        payload["config_overrides"] = {"runtime.output_uri": "file:///tmp/escape"}
        payload.pop("client_request_id")
        payload.pop("display_name")
        response = client.post("/api/v1/jobs/validate", json=payload)
        assert response.status_code == 403
        body = response.json()["error"]
        assert body["code"] == "CONFIG_OVERRIDE_FORBIDDEN"
    assert body["trace_id"].startswith("trc_")


def test_pi05_cpfs_version_can_be_submitted_after_h20_acceptance(example_dataset_seed):
    with TestClient(app) as client:
        payload = request_payload(f"pi05-cpfs-accepted-{uuid4().hex}")
        payload["model_version_id"] = "mv_pi05_20260717_cpfs"
        payload["resource_selection"] = {"mode": "MANUAL", "profile": "pi05-h20-standard"}
        response = client.post(
            "/api/v1/jobs",
            json=payload,
            headers={"Idempotency-Key": payload["client_request_id"]},
        )
        assert response.status_code == 202
        assert response.json()["job_id"].startswith("job_")
