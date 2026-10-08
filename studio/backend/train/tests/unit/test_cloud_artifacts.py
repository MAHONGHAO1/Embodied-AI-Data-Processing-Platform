from types import SimpleNamespace
from typing import Any

from quictrain_provider_aliyun_dlc import OSSArtifactClient, parse_oss_uri
from quictrain_provider_local import FakeProvider
from quictrain_scheduler import Scheduler
from sqlalchemy import BigInteger

from quictrain_api.db import ArtifactRecord


def test_artifact_size_uses_64_bit_database_type():
    assert isinstance(ArtifactRecord.__table__.c.size_bytes.type, BigInteger)


class MemoryArtifactClient:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def exists(self, uri: str) -> bool:
        return uri == f"{self.prefix}/_SUCCESS"

    def read_json(self, uri: str) -> dict[str, Any]:
        assert uri == f"{self.prefix}/artifact_manifest.json"
        return {
            "protocol_version": "v1alpha1",
            "artifacts": [
                {
                    "name": "final_model.safetensors",
                    "size_bytes": 12,
                    "sha256": "a" * 64,
                    "uri": f"{self.prefix}/lerobot/final_model.safetensors",
                }
            ],
        }


class RecordingSession:
    def __init__(self) -> None:
        self.items: list[Any] = []

    def add(self, item: Any) -> None:
        self.items.append(item)


def test_parse_oss_uri_supports_dlc_internal_endpoint():
    location = parse_oss_uri(
        "oss://quictrain.oss-cn-beijing-internal.aliyuncs.com/runs/job/manifest.json"
    )
    assert location.bucket == "quictrain"
    assert location.key == "runs/job/manifest.json"
    assert location.endpoint == "https://oss-cn-beijing-internal.aliyuncs.com"


def test_oss_preview_uses_sdk_v2_stream_reader_without_size_argument():
    class Body:
        def read(self) -> bytes:
            return b'{"status":"ok"}'

    class Client:
        def get_object(self, _request):
            return SimpleNamespace(body=Body())

    client = OSSArtifactClient("cn-beijing")
    client._clients[None] = Client()

    assert client.read_bytes("oss://bucket/control/summary.json", max_bytes=1024) == (
        b'{"status":"ok"}'
    )


def test_scheduler_ingests_only_verified_real_manifest():
    prefix = "oss://bucket/runs/job/attempt"
    scheduler = Scheduler(
        None,
        FakeProvider(),
        artifact_root="oss://bucket/runs",
        artifact_client=MemoryArtifactClient(prefix),
    )
    session = RecordingSession()
    job = SimpleNamespace(id="job", events=[], updated_at=None)
    attempt = SimpleNamespace(
        id="attempt",
        provider_payload={
            "artifact_prefix": prefix,
            "artifact_manifest_uri": f"{prefix}/artifact_manifest.json",
        },
    )
    assert scheduler._ingest_artifact_manifest(session, job, attempt) is True
    artifact = next(item for item in session.items if item.__class__.__name__ == "ArtifactRecord")
    assert artifact.uri == f"{prefix}/lerobot/final_model.safetensors"
    assert artifact.sha256 == "a" * 64
    assert artifact.kind == "checkpoint"


def test_cpfs_workspace_owns_training_outputs():
    scheduler = Scheduler(
        None,
        FakeProvider(),
        artifact_root="oss://bucket/quictrain/control",
        cpfs_data_source_id="d-cpfs",
        cpfs_root_uri="bmcpfs://filesystem",
        cpfs_mount_path="/mnt/cpfs",
        cpfs_workspace_dir="quictrain",
    )

    assert scheduler._cpfs_workspace_path() == "/mnt/cpfs/quictrain"
    assert (
        scheduler._artifact_uri("job", "attempt")
        == "bmcpfs://filesystem/quictrain/runs/job/attempt"
    )
