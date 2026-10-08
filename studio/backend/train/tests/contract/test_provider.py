from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from alibabacloud_pai_dlc20201203 import models
from quictrain_core import LaunchSpec, ProviderState
from quictrain_provider_aliyun_dlc import (
    AliyunDLCProvider,
    AliyunDLCSdkClient,
    AliyunDLCSettings,
)
from quictrain_provider_local import FakeProvider


def launch_spec() -> LaunchSpec:
    return LaunchSpec(
        job_id="job_test",
        attempt_id="att_test",
        idempotency_key="job_test:attempt:1",
        image_uri="registry.invalid/runtime",
        image_digest="sha256:test",
        command=("python", "-m", "quictrain_runner"),
        environment={},
        mounts=(),
        resource={"gpu_count": 1},
    )


def test_fake_provider_submit_is_idempotent():
    provider = FakeProvider()
    first = provider.submit(launch_spec())
    second = provider.submit(launch_spec())
    assert first.external_id == second.external_id


def test_fake_provider_lifecycle_and_logs():
    provider = FakeProvider()
    submitted = provider.submit(launch_spec())
    states = [provider.get(submitted.external_id).state for _ in range(5)]
    assert ProviderState.RUNNING in states
    assert states[-1] == ProviderState.SUCCEEDED
    lines, cursor = provider.get_logs(submitted.external_id)
    assert lines
    assert cursor > 0


def test_cancel_is_idempotent():
    provider = FakeProvider()
    submitted = provider.submit(launch_spec())
    assert provider.cancel(submitted.external_id).state == ProviderState.CANCELLED
    assert provider.cancel(submitted.external_id).state == ProviderState.CANCELLED


class RecordingDLCClient:
    def __init__(self) -> None:
        self.created: dict[str, Any] | None = None

    def create_job(self, **kwargs: Any) -> dict[str, Any]:
        self.created = kwargs
        return {"JobId": "dlc-test", "Status": "Creating"}

    def get_job(self, job_id: str) -> dict[str, Any]:
        return {"JobId": job_id, "Status": "Running"}

    def list_jobs(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    def stop_job(self, job_id: str) -> dict[str, Any]:
        return {"JobId": job_id}

    def get_logs(self, job_id: str, cursor: int = 0) -> tuple[list[dict[str, Any]], int]:
        return ([{"message": f"{job_id} loss=0.5"}], cursor + 1)


def test_aliyun_provider_builds_real_dlc_shape():
    client = RecordingDLCClient()
    provider = AliyunDLCProvider(
        AliyunDLCSettings(
            region_id="cn-beijing",
            workspace_id="420135",
            resource_id="quota-h20",
            ecs_spec="ecs.h20.test",
        ),
        client,
    )
    spec = replace(
        launch_spec(),
        command=("python", "-m", "quictrain_runner", "run"),
        environment={"A": "B"},
        mounts=(
            {
                "source_uri": "oss://bucket/path",
                "target": "/quictrain/output",
                "read_only": "false",
            },
        ),
        resource={"gpu_count": 1, "gpu_type": "H20"},
    )
    submitted = provider.submit(spec)
    assert submitted.external_id == "dlc-test"
    assert client.created is not None
    assert client.created["job_type"] == "PyTorchJob"
    assert client.created["job_specs"] == [
        {
            "type": "Worker",
            "pod_count": 1,
            "image": "registry.invalid/runtime@sha256:test",
            "ecs_spec": "ecs.h20.test",
        }
    ]
    assert client.created["user_command"] == "python -m quictrain_runner run"
    assert client.created["data_sources"][0]["uri"] == (
        "oss://bucket.oss-cn-beijing-internal.aliyuncs.com/path/"
    )
    assert client.created["accessibility"] == "PUBLIC"


def test_aliyun_provider_preserves_endpoint_qualified_mount_uri():
    client = RecordingDLCClient()
    provider = AliyunDLCProvider(
        AliyunDLCSettings(
            region_id="cn-beijing",
            workspace_id="420135",
            resource_id="quota-h20",
        ),
        client,
    )
    provider.submit(
        replace(
            launch_spec(),
            mounts=(
                {
                    "source_uri": ("oss://bucket.oss-cn-beijing-internal.aliyuncs.com/path"),
                    "target": "/data",
                },
            ),
        )
    )
    assert client.created is not None
    assert client.created["data_sources"][0]["uri"] == (
        "oss://bucket.oss-cn-beijing-internal.aliyuncs.com/path/"
    )


def test_generated_sdk_request_uses_official_field_names():
    class FakeSdk:
        request: Any = None

        def create_job(self, request: Any) -> Any:
            self.request = request
            return SimpleNamespace(body=models.CreateJobResponseBody(job_id="dlc-sdk"))

    fake = FakeSdk()
    client = AliyunDLCSdkClient(fake)
    result = client.create_job(
        workspace_id="420135",
        resource_id="quota-h20",
        display_name="quictrain-job-test",
        job_type="PyTorchJob",
        job_specs=[
            {
                "type": "Worker",
                "pod_count": 1,
                "image": "registry/runtime@sha256:test",
                "resource_config": {"gpu": "1", "gputype": "Tesla-V100-16G"},
            }
        ],
        user_command="python -V",
        environment_variables={"A": "B"},
        data_sources=[{"uri": "oss://bucket/data", "mount_path": "/data"}],
        priority=1,
        accessibility="PUBLIC",
        job_max_running_time_minutes=15,
    )
    assert result["JobId"] == "dlc-sdk"
    payload = fake.request.to_map()
    assert payload["JobType"] == "PyTorchJob"
    assert payload["JobSpecs"][0]["ResourceConfig"]["GPUType"] == "Tesla-V100-16G"
    assert payload["DataSources"][0]["MountPath"] == "/data"
    assert payload["Accessibility"] == "PUBLIC"


def test_quota_submission_does_not_send_ui_gpu_label_as_provider_code():
    client = RecordingDLCClient()
    provider = AliyunDLCProvider(
        AliyunDLCSettings(
            region_id="cn-beijing",
            workspace_id="420135",
            resource_id="quota-h20",
        ),
        client,
    )
    provider.submit(
        replace(
            launch_spec(),
            resource={
                "gpu_count": 1,
                "gpu_type": "H20",
                "cpu": 8,
                "memory": "64Gi",
                "shared_memory": "16Gi",
            },
        )
    )
    assert client.created is not None
    resource_config = client.created["job_specs"][0]["resource_config"]
    assert resource_config == {
        "gpu": "1",
        "cpu": "8",
        "memory": "64Gi",
        "shared_memory": "16Gi",
    }


def test_sdk_omits_gpu_type_when_quota_selects_accelerator():
    class FakeSdk:
        request: Any = None

        def create_job(self, request: Any) -> Any:
            self.request = request
            return SimpleNamespace(body=models.CreateJobResponseBody(job_id="dlc-sdk-quota"))

    fake = FakeSdk()
    client = AliyunDLCSdkClient(fake)
    client.create_job(
        workspace_id="420135",
        resource_id="quota-h20",
        display_name="quictrain-quota-job-test",
        job_type="PyTorchJob",
        job_specs=[
            {
                "type": "Worker",
                "pod_count": 1,
                "image": "registry/runtime@sha256:test",
                "resource_config": {
                    "gpu": "1",
                    "cpu": "8",
                    "memory": "128Gi",
                    "shared_memory": "32Gi",
                },
            }
        ],
        user_command="python -V",
        environment_variables={},
        data_sources=[],
        priority=1,
        accessibility="PUBLIC",
        job_max_running_time_minutes=15,
    )
    payload = fake.request.to_map()
    assert payload["JobSpecs"][0]["ResourceConfig"] == {
        "CPU": "8",
        "GPU": "1",
        "Memory": "128Gi",
        "SharedMemory": "32Gi",
    }


def test_aliyun_provider_preserves_real_reason_and_dashboard_link():
    provider = AliyunDLCProvider(
        AliyunDLCSettings(
            region_id="cn-beijing",
            workspace_id="420135",
            resource_id="quota-h20",
        ),
        RecordingDLCClient(),
    )
    failed = provider._from_payload(
        {
            "JobId": "dlc-failed",
            "Status": "Failed",
            "ReasonCode": "InvalidParameter",
            "ReasonMessage": "ResourceConfig is invalid.",
        }
    )
    assert failed.state == ProviderState.FAILED
    assert failed.reason_code == "InvalidParameter"
    assert failed.message == "ResourceConfig is invalid."
    assert provider.get_dashboard_url("dlc-failed") == (
        "https://pai.console.aliyun.com/?regionId=cn-beijing&workspaceId=420135"
        "#/dlc/jobs/dlc-failed/detail"
    )
