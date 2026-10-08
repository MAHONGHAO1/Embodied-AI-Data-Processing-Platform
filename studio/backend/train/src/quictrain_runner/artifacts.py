from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ObjectStoreClient(Protocol):
    def upload_file(self, local_path: str, object_key: str) -> None: ...


class ArtifactStore:
    """Writes artifacts directly to object storage through an injected OSS client."""

    def __init__(self, client: ObjectStoreClient, bucket: str, allowed_prefix: str) -> None:
        self.client = client
        self.bucket = bucket
        self.allowed_prefix = allowed_prefix.strip("/")

    def upload(self, job_id: str, attempt_id: str, path: Path) -> str:
        safe_name = path.name
        if safe_name in {"", ".", ".."} or "/" in safe_name or "\\" in safe_name:
            raise ValueError("Invalid artifact name")
        key = f"{self.allowed_prefix}/{job_id}/{attempt_id}/{safe_name}"
        if not key.startswith(f"{self.allowed_prefix}/{job_id}/{attempt_id}/"):
            raise ValueError("Artifact URI is outside the allowed prefix")
        self.client.upload_file(str(path), key)
        return f"oss://{self.bucket}/{key}"
