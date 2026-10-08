from __future__ import annotations

import hashlib
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from quictrain_core import LaunchSpec, ProviderJob, ProviderState
from quictrain_core.provider import LogLine


@dataclass(frozen=True)
class AliyunDLCSettings:
    region_id: str
    workspace_id: str
    resource_id: str
    ram_role_arn: str | None = None
    ecs_spec: str | None = None
    # A dedicated quota already determines the accelerator model. Set this
    # only when DLC expects an explicit provider GPU type code; a UI label such
    # as "H20" is not necessarily a valid API value.
    gpu_type: str | None = None
    job_max_running_time_minutes: int = 60
    priority: int = 1
    # PUBLIC means visible to members of the PAI workspace, not public Internet.
    accessibility: str = "PUBLIC"
    endpoint: str | None = None


class DLCClient(Protocol):
    def create_job(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_job(self, job_id: str) -> dict[str, Any]: ...

    def list_jobs(self, **kwargs: Any) -> list[dict[str, Any]]: ...

    def stop_job(self, job_id: str) -> dict[str, Any]: ...

    def get_logs(self, job_id: str, cursor: int = 0) -> tuple[list[dict[str, Any]], int]: ...


STATE_MAP = {
    "Pending": ProviderState.PROVISIONING,
    "Creating": ProviderState.PROVISIONING,
    "Queuing": ProviderState.PROVISIONING,
    "PreAllocating": ProviderState.PROVISIONING,
    "EnvPreparing": ProviderState.PROVISIONING,
    "Running": ProviderState.RUNNING,
    "Restarting": ProviderState.RUNNING,
    "Succeeded": ProviderState.SUCCEEDED,
    "Failed": ProviderState.FAILED,
    "Stopping": ProviderState.PROVISIONING,
    "Stopped": ProviderState.CANCELLED,
}


class AliyunDLCProvider:
    """Thin adapter around an injected PAI-DLC client.

    The SDK and credentials stay optional so the control plane can run without
    cloud packages. Production wiring injects a credential-provider-backed client.
    """

    def __init__(self, settings: AliyunDLCSettings, client: DLCClient | None = None) -> None:
        self.settings = settings
        self.client = client

    def _require_client(self) -> DLCClient:
        if self.client is None:
            raise RuntimeError(
                "Aliyun DLC client is not configured. "
                "Use RAM Role/STS and install the aliyun extra."
            )
        return self.client

    def _normalize_mount_uri(self, uri: str) -> str:
        """Return the endpoint-qualified OSS URI required by DLC storage mounts."""

        if not uri.startswith("oss://"):
            return uri
        parsed = urlsplit(uri)
        authority = parsed.netloc
        if ".oss-" not in authority:
            authority = f"{authority}.oss-{self.settings.region_id}-internal.aliyuncs.com"
        path = parsed.path.rstrip("/") + "/"
        return urlunsplit((parsed.scheme, authority, path, parsed.query, parsed.fragment))

    def submit(self, spec: LaunchSpec) -> ProviderJob:
        existing = self.find_by_idempotency_key(spec.idempotency_key)
        if existing:
            return existing
        client = self._require_client()
        job_spec: dict[str, Any] = {
            "type": "Worker",
            "pod_count": int(spec.resource.get("worker_count", 1)),
            "image": f"{spec.image_uri}@{spec.image_digest}",
        }
        ecs_spec = str(spec.resource.get("ecs_spec") or self.settings.ecs_spec or "")
        if ecs_spec:
            job_spec["ecs_spec"] = ecs_spec
        else:
            resource_config = {
                "gpu": str(spec.resource.get("gpu_count", 1)),
                **(
                    {"cpu": str(spec.resource["cpu"])}
                    if spec.resource.get("cpu") is not None
                    else {}
                ),
                **(
                    {"memory": str(spec.resource["memory"])}
                    if spec.resource.get("memory") is not None
                    else {}
                ),
                **(
                    {"shared_memory": str(spec.resource["shared_memory"])}
                    if spec.resource.get("shared_memory") is not None
                    else {}
                ),
            }
            provider_gpu_type = spec.resource.get("provider_gpu_type") or self.settings.gpu_type
            if provider_gpu_type:
                resource_config["gputype"] = str(provider_gpu_type)
            job_spec["resource_config"] = resource_config

        data_sources = []
        for mount in spec.mounts:
            source: dict[str, Any] = {"mount_path": mount["target"]}
            if mount.get("data_source_id"):
                source["data_source_id"] = mount["data_source_id"]
            if mount.get("source_uri"):
                uri = str(mount["source_uri"])
                # Raw DLC OSS mounts require the bucket authority to include
                # the regional endpoint. Catalog and artifact URIs remain
                # endpoint-agnostic outside the provider boundary.
                source["uri"] = self._normalize_mount_uri(uri)
            if mount.get("options"):
                source["options"] = mount["options"]
            data_sources.append(source)

        payload = {
            "workspace_id": self.settings.workspace_id,
            "resource_id": str(spec.resource.get("resource_id") or self.settings.resource_id),
            "display_name": self._display_name(spec.job_id, spec.idempotency_key),
            "job_type": "PyTorchJob",
            "job_specs": [job_spec],
            "user_command": shlex.join(spec.command),
            "environment_variables": {
                **spec.environment,
                "QUICTRAIN_IDEMPOTENCY_KEY": spec.idempotency_key,
            },
            "data_sources": data_sources,
            "priority": int(spec.resource.get("priority", self.settings.priority)),
            "accessibility": self.settings.accessibility,
            "job_max_running_time_minutes": int(
                spec.resource.get(
                    "job_max_running_time_minutes", self.settings.job_max_running_time_minutes
                )
            ),
        }
        user_vpc = spec.resource.get("user_vpc")
        if isinstance(user_vpc, dict) and user_vpc:
            payload["user_vpc"] = user_vpc
        result = client.create_job(**payload)
        return self._from_payload(result)

    def get(self, external_id: str) -> ProviderJob:
        return self._from_payload(self._require_client().get_job(external_id))

    def find_by_idempotency_key(self, key: str) -> ProviderJob | None:
        job_id = key.split(":", 1)[0]
        jobs = self._require_client().list_jobs(
            workspace_id=self.settings.workspace_id,
            display_name=self._display_name(job_id, key),
        )
        if not jobs:
            return None
        if len(jobs) > 1:
            raise RuntimeError(f"Multiple DLC jobs found for idempotency key {key}")
        return self._from_payload(jobs[0])

    def cancel(self, external_id: str) -> ProviderJob:
        try:
            self._require_client().stop_job(external_id)
        except Exception as exc:
            message = str(exc)
            # Idempotent cancel: already-stopped DLC jobs must not block QuicTrain
            # CANCEL_REQUESTED reconciliation.
            if any(
                token in message
                for token in ("can't be stopped", "already", "Stopped", "Succeeded", "Failed")
            ):
                return self.get(external_id)
            raise
        return self.get(external_id)

    def get_logs(self, external_id: str, cursor: int = 0) -> tuple[list[LogLine], int]:
        payloads, next_cursor = self._require_client().get_logs(external_id, cursor)
        lines = [
            LogLine(
                sequence=int(item.get("sequence", cursor + index + 1)),
                timestamp=str(item.get("timestamp") or datetime.now(UTC).isoformat()),
                level=str(item.get("level") or "INFO"),
                source=str(item.get("source") or "dlc/worker-0"),
                message=str(item.get("message") or ""),
            )
            for index, item in enumerate(payloads)
        ]
        return lines, next_cursor

    def get_dashboard_url(self, external_id: str) -> str | None:
        return (
            "https://pai.console.aliyun.com/"
            f"?regionId={self.settings.region_id}&workspaceId={self.settings.workspace_id}"
            f"#/dlc/jobs/{external_id}/detail"
        )

    @staticmethod
    def _from_payload(payload: dict[str, Any]) -> ProviderJob:
        external_id = str(payload.get("job_id") or payload.get("JobId"))
        raw = str(payload.get("status") or payload.get("Status") or "Unknown")
        return ProviderJob(
            external_id=external_id,
            state=STATE_MAP.get(raw, ProviderState.UNKNOWN),
            raw_status=raw,
            message=(
                payload.get("reason_message")
                or payload.get("ReasonMessage")
                or payload.get("message")
                or payload.get("Message")
            ),
            reason_code=payload.get("reason_code") or payload.get("ReasonCode"),
        )

    @staticmethod
    def _display_name(job_id: str, idempotency_key: str) -> str:
        suffix = hashlib.sha256(idempotency_key.encode()).hexdigest()[:12]
        return f"quictrain-{job_id}-{suffix}"[:256]
