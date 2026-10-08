from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class OSSLocation:
    bucket: str
    key: str
    endpoint: str | None = None


def parse_oss_uri(uri: str) -> OSSLocation:
    parsed = urlparse(uri)
    if parsed.scheme != "oss" or not parsed.netloc or not parsed.path.strip("/"):
        raise ValueError(f"Invalid OSS object URI: {uri}")
    host = parsed.netloc
    endpoint = None
    bucket = host
    if ".oss-" in host:
        bucket, endpoint_host = host.split(".", 1)
        endpoint = f"https://{endpoint_host}"
    return OSSLocation(bucket=bucket, key=parsed.path.lstrip("/"), endpoint=endpoint)


class OSSArtifactClient:
    def __init__(self, region: str, endpoint: str | None = None) -> None:
        self.region = region
        self.endpoint = endpoint
        self._clients: dict[str | None, Any] = {}

    def read_json(self, uri: str) -> dict[str, Any]:
        payload = self.read_bytes(uri)
        return json.loads(payload.decode("utf-8"))

    def exists(self, uri: str) -> bool:
        try:
            self.read_bytes(uri)
        except FileNotFoundError:
            return False
        return True

    def read_bytes(self, uri: str, max_bytes: int | None = None) -> bytes:
        try:
            import alibabacloud_oss_v2 as oss

            get_request_cls = oss.GetObjectRequest
        except ImportError:

            class _GetObjectRequest:
                def __init__(self, bucket: str, key: str) -> None:
                    self.bucket = bucket
                    self.key = key

            get_request_cls = _GetObjectRequest

        location = parse_oss_uri(uri)
        client = self._client(location.endpoint or self.endpoint)
        try:
            result = client.get_object(get_request_cls(bucket=location.bucket, key=location.key))
            # OSS SDK v2's StreamBodyReader exposes read() without a size
            # argument. Artifact metadata is checked by the API before preview;
            # enforce the byte limit again after reading for defense in depth.
            payload = result.body.read()
            if max_bytes is not None and len(payload) > max_bytes:
                raise ValueError(f"OSS object exceeds preview limit ({max_bytes} bytes): {uri}")
            return payload
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                raise FileNotFoundError(uri) from exc
            raise

    def list_keys(self, uri: str, *, max_keys: int = 10_000) -> list[str]:
        """List object keys under an oss://bucket/prefix (paginated, CPU-only)."""

        import alibabacloud_oss_v2 as oss

        location = parse_oss_uri(uri if uri.endswith("/") else f"{uri}/")
        client = self._client(location.endpoint or self.endpoint)
        prefix = location.key
        keys: list[str] = []
        continuation: str | None = None
        while len(keys) < max_keys:
            kwargs: dict[str, Any] = {
                "bucket": location.bucket,
                "prefix": prefix,
                "max_keys": min(1000, max_keys - len(keys)),
            }
            if continuation:
                kwargs["continuation_token"] = continuation
            result = client.list_objects_v2(oss.ListObjectsV2Request(**kwargs))
            for item in getattr(result, "contents", None) or []:
                key = getattr(item, "key", None)
                if key and not str(key).endswith("/"):
                    keys.append(str(key))
            if not getattr(result, "is_truncated", False):
                break
            continuation = getattr(result, "next_continuation_token", None)
            if not continuation:
                break
        return keys

    def presign_get(
        self,
        uri: str,
        expires_seconds: int = 900,
        *,
        download_name: str | None = None,
        content_type: str | None = None,
    ) -> str:
        import alibabacloud_oss_v2 as oss

        location = parse_oss_uri(uri)
        client = self._client(location.endpoint or self.endpoint)
        request = oss.GetObjectRequest(
            bucket=location.bucket,
            key=location.key,
            response_content_disposition=(
                f'attachment; filename="{download_name.replace(chr(34), "")}"'
                if download_name
                else None
            ),
            response_content_type=content_type,
        )
        result = client.presign(
            request,
            expires=timedelta(seconds=expires_seconds),
        )
        return result.url

    def _client(self, endpoint: str | None) -> Any:
        if endpoint not in self._clients:
            self._clients[endpoint] = _create_client(self.region, endpoint)
        return self._clients[endpoint]


def _create_client(region: str, endpoint: str | None) -> Any:
    import alibabacloud_oss_v2 as oss
    from alibabacloud_credentials.client import Client as CredentialClient

    credential_client = CredentialClient()

    class CredentialProvider(oss.credentials.CredentialsProvider):
        def get_credentials(self) -> Any:
            credential = credential_client.get_credential()
            return oss.credentials.Credentials(
                credential.access_key_id,
                credential.access_key_secret,
                credential.security_token,
            )

    config = oss.config.load_default()
    config.region = region
    config.endpoint = endpoint
    config.credentials_provider = CredentialProvider()
    return oss.Client(config)
