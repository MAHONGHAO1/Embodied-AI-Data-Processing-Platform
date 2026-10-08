from __future__ import annotations

from typing import Any

from .provider import AliyunDLCSettings


class AliyunDLCSdkClient:
    """Small normalization layer around Alibaba Cloud's generated DLC SDK."""

    def __init__(self, sdk_client: Any) -> None:
        self.sdk_client = sdk_client

    def create_job(self, **kwargs: Any) -> dict[str, Any]:
        from alibabacloud_pai_dlc20201203 import models

        job_specs = []
        for value in kwargs["job_specs"]:
            spec: dict[str, Any] = {
                "Type": value["type"],
                "Image": value["image"],
                "PodCount": value["pod_count"],
            }
            if value.get("ecs_spec"):
                spec["EcsSpec"] = value["ecs_spec"]
            if value.get("resource_config"):
                resource = value["resource_config"]
                spec["ResourceConfig"] = {
                    api_name: resource[key]
                    for key, api_name in (
                        ("gpu", "GPU"),
                        ("gputype", "GPUType"),
                        ("cpu", "CPU"),
                        ("memory", "Memory"),
                        ("shared_memory", "SharedMemory"),
                    )
                    if resource.get(key) is not None
                }
            job_specs.append(spec)

        data_sources = []
        for value in kwargs.get("data_sources", []):
            source = {
                "MountPath": value["mount_path"],
            }
            if value.get("data_source_id"):
                source["DataSourceId"] = value["data_source_id"]
            if value.get("uri"):
                source["Uri"] = value["uri"]
            if value.get("options"):
                source["Options"] = value["options"]
            data_sources.append(source)

        request_map: dict[str, Any] = {
            "WorkspaceId": kwargs["workspace_id"],
            "ResourceId": kwargs.get("resource_id") or None,
            "DisplayName": kwargs["display_name"],
            "JobType": kwargs["job_type"],
            "JobSpecs": job_specs,
            "UserCommand": kwargs["user_command"],
            "Envs": kwargs.get("environment_variables", {}),
            "DataSources": data_sources,
            "Priority": kwargs.get("priority", 1),
            "Accessibility": kwargs.get("accessibility", "PUBLIC"),
            "JobMaxRunningTimeMinutes": kwargs.get("job_max_running_time_minutes"),
        }
        if kwargs.get("user_vpc"):
            request_map["UserVpc"] = kwargs["user_vpc"]
        request = models.CreateJobRequest().from_map(request_map)
        response = self.sdk_client.create_job(request)
        return response.body.to_map()

    def get_job(self, job_id: str) -> dict[str, Any]:
        from alibabacloud_pai_dlc20201203 import models

        response = self.sdk_client.get_job(job_id, models.GetJobRequest(need_detail=True))
        return response.body.to_map()

    def list_jobs(self, **kwargs: Any) -> list[dict[str, Any]]:
        from alibabacloud_pai_dlc20201203 import models

        request = models.ListJobsRequest(
            workspace_id=kwargs.get("workspace_id"),
            display_name=kwargs.get("display_name"),
            page_number=1,
            page_size=100,
            sort_by="GmtCreateTime",
            order="desc",
        )
        response = self.sdk_client.list_jobs(request)
        jobs = response.body.jobs or []
        expected = kwargs.get("display_name")
        return [item.to_map() for item in jobs if expected is None or item.display_name == expected]

    def stop_job(self, job_id: str) -> dict[str, Any]:
        response = self.sdk_client.stop_job(job_id)
        return response.body.to_map() if response.body is not None else {"JobId": job_id}

    def get_logs(self, job_id: str, cursor: int = 0) -> tuple[list[dict[str, Any]], int]:
        from alibabacloud_pai_dlc20201203 import models

        job = self.sdk_client.get_job(job_id, models.GetJobRequest(need_detail=True)).body
        pods = job.pods or []
        worker = next((pod for pod in pods if pod.type == "Worker"), pods[0] if pods else None)
        if worker is None or not worker.pod_id:
            return [], cursor
        response = self.sdk_client.get_pod_logs(
            job_id,
            worker.pod_id,
            models.GetPodLogsRequest(pod_uid=worker.pod_uid, max_lines=2000),
        )
        raw_lines = response.body.logs or []
        if cursor > len(raw_lines):
            cursor = 0
        return (
            [
                {
                    "sequence": index + 1,
                    "source": f"dlc/{worker.pod_id}",
                    "message": line,
                }
                for index, line in enumerate(raw_lines[cursor:], start=cursor)
            ],
            len(raw_lines),
        )


def create_sdk_client(settings: AliyunDLCSettings) -> AliyunDLCSdkClient:
    """Create a DLC client from the official default credential-provider chain."""

    try:
        from alibabacloud_credentials.client import Client as CredentialClient
        from alibabacloud_pai_dlc20201203.client import Client as DLCClient
        from alibabacloud_tea_openapi.models import Config
    except ImportError as exc:  # pragma: no cover - depends on deployment extra
        raise RuntimeError("Install QuicTrain with the aliyun optional dependency") from exc

    credential = CredentialClient()
    endpoint = settings.endpoint or f"pai-dlc.{settings.region_id}.aliyuncs.com"
    config = Config(credential=credential, region_id=settings.region_id, endpoint=endpoint)
    return AliyunDLCSdkClient(DLCClient(config))
