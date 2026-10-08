from datetime import UTC, datetime
from types import SimpleNamespace

from quictrain_api.auth import Actor
from quictrain_api.main import job_logs


class EmptyLogSession:
    def __init__(self, job):
        self.job = job

    def get(self, _record_type, _record_id):
        return self.job

    def scalars(self, _statement):
        return []


def test_terminal_retry_uses_archived_log_when_latest_attempt_has_no_rows(monkeypatch):
    previous_attempt = SimpleNamespace(id="att_previous", logs=[object()])
    latest_attempt = SimpleNamespace(id="att_latest", logs=[])
    artifact = SimpleNamespace(
        attempt_id="att_latest",
        name="lerobot.log",
        uri="oss://bucket/jobs/job_retry/lerobot.log",
        size_bytes=12,
        created_at=datetime(2026, 7, 23, tzinfo=UTC),
    )
    job = SimpleNamespace(
        project_id="prj_robot_arm",
        attempts=[previous_attempt, latest_attempt],
        logs=[object()],
        artifacts=[artifact],
        state="FAILED",
    )

    class MemoryArtifactClient:
        def read_bytes(self, uri, max_bytes=None):
            assert uri == artifact.uri
            assert max_bytes == 2 * 1024 * 1024
            return b"latest attempt archived line\n"

    monkeypatch.setattr("quictrain_api.main.get_artifact_client", lambda: MemoryArtifactClient())
    monkeypatch.setattr(
        "quictrain_api.main.enforce_actor",
        lambda *args, **kwargs: Actor(
            user_id="usr_demo",
            display_name="Kirito",
            global_role="admin",
            project_role="admin",
        ),
    )

    result = job_logs("job_retry", cursor=0, limit=1000, session=EmptyLogSession(job))

    assert result["degraded"] is True
    assert result["eof"] is True
    assert [line["message"] for line in result["lines"]] == ["latest attempt archived line"]
