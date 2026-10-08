"""PAI-DSW OpenAPI client for lifecycle / access URLs (not DLC CreateJob).

DSW instance IDs (`dsw-*`) reserve GPUs on the same workspace quotas used by DLC.
They must never be passed as CreateJob ``ResourceId`` — use ``quota*`` for DLC.
Training jobs prefer ``aliyun_dlc``; this client is for list/start/stop/verify and
exposing Jupyter / WebIDE / Terminal URLs for interactive work on Running instances.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AliyunDSWSettings:
    region_id: str
    workspace_id: str
    endpoint: str | None = None


@dataclass(frozen=True)
class DSWInstance:
    instance_id: str
    instance_name: str
    status: str
    workspace_id: str | None
    resource_id: str | None
    resource_name: str | None
    gpu_count: int | None
    jupyterlab_url: str | None
    webide_url: str | None
    terminal_url: str | None
    raw: dict[str, Any]

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "instance_name": self.instance_name,
            "status": self.status,
            "workspace_id": self.workspace_id,
            "resource_id": self.resource_id,
            "resource_name": self.resource_name,
            "gpu_count": self.gpu_count,
            "jupyterlab_url": self.jupyterlab_url,
            "webide_url": self.webide_url,
            "terminal_url": self.terminal_url,
            "holds_quota": self.status in {"Running", "Starting", "Saving", "EnvPreparing"},
            "training_mode": "interactive_dsw",
            "preferred_training_provider": "aliyun_dlc",
        }


def _from_payload(payload: dict[str, Any]) -> DSWInstance:
    requested = payload.get("RequestedResource") or payload.get("requested_resource") or {}
    gpu_raw = requested.get("GPU") or requested.get("gpu")
    try:
        gpu_count = int(gpu_raw) if gpu_raw is not None else None
    except (TypeError, ValueError):
        gpu_count = None
    return DSWInstance(
        instance_id=str(payload.get("InstanceId") or payload.get("instance_id") or ""),
        instance_name=str(payload.get("InstanceName") or payload.get("instance_name") or ""),
        status=str(payload.get("Status") or payload.get("status") or "Unknown"),
        workspace_id=(payload.get("WorkspaceId") or payload.get("workspace_id")),
        resource_id=(payload.get("ResourceId") or payload.get("resource_id")),
        resource_name=(payload.get("ResourceName") or payload.get("resource_name")),
        gpu_count=gpu_count,
        jupyterlab_url=(payload.get("JupyterlabUrl") or payload.get("jupyterlab_url")),
        webide_url=(payload.get("WebIDEUrl") or payload.get("webide_url")),
        terminal_url=(payload.get("TerminalUrl") or payload.get("terminal_url")),
        raw=payload,
    )


class AliyunDSWSdkClient:
    def __init__(self, sdk_client: Any) -> None:
        self.sdk_client = sdk_client

    def get_instance(self, instance_id: str) -> DSWInstance:
        from alibabacloud_pai_dsw20220101 import models

        response = self.sdk_client.get_instance(instance_id, models.GetInstanceRequest())
        body = response.body.to_map() if response.body is not None else {}
        return _from_payload(body)

    def list_instances(self, *, workspace_id: str | None = None) -> list[DSWInstance]:
        from alibabacloud_pai_dsw20220101 import models

        request = models.ListInstancesRequest(
            workspace_id=workspace_id,
            page_number=1,
            page_size=100,
        )
        response = self.sdk_client.list_instances(request)
        body = response.body.to_map() if response.body is not None else {}
        items = body.get("Instances") or body.get("instances") or []
        return [_from_payload(item if isinstance(item, dict) else item.to_map()) for item in items]

    def start_instance(self, instance_id: str) -> dict[str, Any]:
        response = self.sdk_client.start_instance(instance_id)
        return response.body.to_map() if response.body is not None else {"InstanceId": instance_id}

    def stop_instance(self, instance_id: str) -> dict[str, Any]:
        response = self.sdk_client.stop_instance(instance_id)
        return response.body.to_map() if response.body is not None else {"InstanceId": instance_id}


def create_dsw_sdk_client(settings: AliyunDSWSettings) -> AliyunDSWSdkClient:
    try:
        from alibabacloud_credentials.client import Client as CredentialClient
        from alibabacloud_pai_dsw20220101.client import Client as DSWClient
        from alibabacloud_tea_openapi.models import Config
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Install QuicTrain with the aliyun optional dependency") from exc

    credential = CredentialClient()
    endpoint = settings.endpoint or f"pai-dsw.{settings.region_id}.aliyuncs.com"
    config = Config(credential=credential, region_id=settings.region_id, endpoint=endpoint)
    return AliyunDSWSdkClient(DSWClient(config))
