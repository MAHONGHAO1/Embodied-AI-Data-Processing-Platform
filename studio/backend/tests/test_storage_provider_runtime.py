from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from data.infra.aliyun_object_storage import AliyunObjectStorage
from data.infra.object_storage import (
    ObjectStorageError,
    StorageNotReady,
    StorageObjectIntegrityError,
    StorageObjectNotFound,
    StorageObjectRef,
)
from data.infra.s3_object_storage import S3ObjectStorage
from data.infra.storage_provider import get_storage_provider
from data.services.storage_bucket_config import StorageBucketConfig


class _Body:
    def __init__(self, value: bytes) -> None:
        self.value = value
        self.closed = False

    def iter_chunks(self, chunk_size: int = 1024):
        yield self.value

    def close(self) -> None:
        self.closed = True


class _DownloadClient:
    def __init__(
        self,
        signed_url: str = "https://internal.example/raw/key?sig=x",
        body: _Body | None = None,
    ) -> None:
        self.signed_url = signed_url
        self.body = body or _Body(b"payload")

    def get_object(self, **kwargs):
        return {"Body": self.body, "ContentLength": 7, "ETag": '"etag"', "VersionId": "v1"}

    def generate_presigned_url(self, **kwargs):
        return self.signed_url


class _PreconditionDownloadClient(_DownloadClient):
    def get_object(self, **kwargs):
        raise ClientError(
            {"Error": {"Code": "PreconditionFailed", "Message": "identity changed"}},
            "GetObject",
        )


class _AliyunBody:
    def __init__(self, value: bytes) -> None:
        self.value = value

    def read(self, _size: int) -> bytes:
        value, self.value = self.value, b""
        return value

    def close(self) -> None:
        return None


class _AliyunBucket:
    def __init__(self, endpoints: list[str]) -> None:
        self.endpoints = endpoints
        self.payload = b""
        self.headers = {"x-oss-meta-sha256": hashlib.sha256(b"").hexdigest()}
        self.etag = "etag"
        self.versionid = "v1"
        self.preserve_metadata = False
        self.complete_metadata = True
        self.object_exists = False
        self.multipart_headers: dict[str, str] = {}
        self.put_headers: dict[str, str] = {}
        self.completed_headers: dict[str, str] = {}
        self.completed_parts = []

    def sign_url(self, method, key, expires, **kwargs):
        return f"{self.endpoints[-1]}/{method}/{key}?expires={expires}"

    def get_object(self, key, **kwargs):
        return _AliyunBody(self.payload)

    def head_object(self, key, **kwargs):
        if not self.object_exists:
            raise RuntimeError("404 not found")
        return SimpleNamespace(
            content_length=len(self.payload),
            headers=self.headers,
            etag=self.etag,
            versionid=self.versionid,
        )

    def put_object_from_file(self, key, filename, headers=None):
        if self.object_exists:
            raise RuntimeError("412 object already exists")
        self.put_headers = dict(headers or {})
        self.payload = Path(filename).read_bytes()
        if not self.preserve_metadata:
            self.headers = {"x-oss-meta-sha256": (headers or {})["x-oss-meta-sha256"]}
        self.object_exists = True

    def init_multipart_upload(self, key, headers=None, params=None):
        self.multipart_headers = dict(headers or {})
        return SimpleNamespace(upload_id="upload-1")

    def list_parts(self, key, upload_id):
        return SimpleNamespace(parts=[SimpleNamespace(part_number=1, etag='"part"', size=0)])

    def complete_multipart_upload(self, key, upload_id, parts, headers=None):
        self.completed_parts = list(parts)
        self.completed_headers = dict(headers or {})
        if not self.complete_metadata:
            self.headers = {}
        else:
            self.headers = dict(self.multipart_headers)
        self.object_exists = True

    def abort_multipart_upload(self, key, upload_id):
        return None

    def delete_object(self, key, params=None):
        return None


def test_download_rejects_replaced_source(tmp_path: Path):
    provider = S3ObjectStorage(
        StorageBucketConfig.for_environment("test"), client=_DownloadClient()
    )
    ref = StorageObjectRef("raw", "key", "v2", "etag", 7, None)
    with pytest.raises(ObjectStorageError):
        provider.download_file(ref, str(tmp_path / "data.mcap"))


def test_s3_download_closes_response_body(tmp_path: Path):
    body = _Body(b"payload")
    provider = S3ObjectStorage(
        StorageBucketConfig.for_environment("test"),
        client=_DownloadClient(body=body),
    )
    ref = StorageObjectRef("raw", "key", "v1", "etag", 7, None)

    provider.download_file(ref, str(tmp_path / "data.mcap"))

    assert body.closed is True


def test_s3_download_closes_response_body_on_validation_failure(tmp_path: Path):
    body = _Body(b"payload")
    provider = S3ObjectStorage(
        StorageBucketConfig.for_environment("test"),
        client=_DownloadClient(body=body),
    )
    ref = StorageObjectRef("raw", "key", "v1", "etag", 999, None)

    with pytest.raises(StorageObjectIntegrityError, match="size"):
        provider.download_file(ref, str(tmp_path / "data.mcap"))

    assert body.closed is True


def test_s3_download_classifies_conditional_identity_failure(tmp_path: Path):
    provider = S3ObjectStorage(
        StorageBucketConfig.for_environment("test"),
        client=_PreconditionDownloadClient(),
    )
    ref = StorageObjectRef("raw", "key", None, "etag", 7, None)

    with pytest.raises(StorageObjectIntegrityError, match="identity changed"):
        provider.download_file(ref, str(tmp_path / "data.mcap"))


def test_sign_get_uses_fixed_identity_and_browser_endpoint():
    provider = S3ObjectStorage(
        StorageBucketConfig.for_environment("test"),
        endpoint_url="http://worker.internal:9000",
        browser_endpoint_url="https://objects.example",
        client=_DownloadClient(),
        browser_client=_DownloadClient("https://objects.example/raw/key?sig=x"),
    )
    ref = StorageObjectRef("raw", "key", "v1", "etag", 7, None)
    assert provider.sign_get(ref).startswith("https://objects.example/")


def test_sign_get_uses_browser_client_and_uri_is_immutable_identity():
    provider = S3ObjectStorage(
        StorageBucketConfig.for_environment("test"),
        endpoint_url="http://worker.internal:9000",
        browser_endpoint_url="https://objects.example",
        client=_DownloadClient(),
        browser_client=_DownloadClient("https://objects.example/raw/key?sig=x"),
    )
    ref = StorageObjectRef("raw", "key", "v1", "etag", 7, None)
    assert provider.sign_get(ref).startswith("https://objects.example/")
    assert provider.object_uri(ref) == "oss://quicstudio-test-raw/key"


def test_storage_factory_fails_closed_for_missing_credentials_and_unknown_provider():
    base = {
        "storage_provider": "minio",
        "storage_endpoint": "",
        "storage_access_key_id": "",
        "storage_secret_access_key": "",
        "storage_browser_endpoint": "",
        "oss_endpoint": "",
        "oss_access_key_id": "",
        "oss_access_key_secret": "",
        "oss_region": "us-east-1",
        "oss_browser_endpoint": "",
        "oss_bucket_raw": "raw",
        "oss_bucket_process": "process",
        "oss_bucket_export": "export",
        "is_production": False,
        "test_mode": True,
    }
    with pytest.raises(StorageNotReady):
        get_storage_provider(SimpleNamespace(**base))
    base.update(
        storage_provider="not-a-provider",
        storage_endpoint="http://minio",
        storage_access_key_id="a",
        storage_secret_access_key="s",
    )
    with pytest.raises(StorageNotReady):
        get_storage_provider(SimpleNamespace(**base))


def test_aliyun_provider_uses_browser_endpoint_and_verifies_zero_byte_worker(
    tmp_path: Path,
):
    endpoints: list[str] = []
    bucket = _AliyunBucket(endpoints)

    def factory(endpoint: str, _name: str):
        endpoints.append(endpoint)
        return bucket

    provider = AliyunObjectStorage(
        StorageBucketConfig.for_environment("test"),
        endpoint_url="http://worker.internal",
        browser_endpoint_url="https://objects.example",
        access_key_id="access",
        access_key_secret="secret",
        bucket_factory=factory,
    )
    write_ref = StorageObjectRef("raw", "empty", None, "", 0, hashlib.sha256(b"").hexdigest())
    ref = StorageObjectRef("raw", "empty", "v1", "etag", 0, hashlib.sha256(b"").hexdigest())
    source = tmp_path / "empty.bin"
    source.write_bytes(b"")

    uploaded = provider.put_worker_object(write_ref, str(source))
    assert uploaded.size_bytes == 0
    assert uploaded.sha256 == hashlib.sha256(b"").hexdigest()
    assert bucket.put_headers == {
        "x-oss-meta-sha256": hashlib.sha256(b"").hexdigest(),
        "x-oss-forbid-overwrite": "true",
    }
    assert provider.object_uri(ref) == "oss://quicstudio-test-raw/empty"
    assert provider.sign_get(ref).startswith("https://objects.example/")
    assert endpoints[-1] == "https://objects.example"
    bucket.object_exists = False
    upload_id = provider.create_multipart(write_ref)
    assert upload_id == "upload-1"
    assert provider.sign_part(ref, upload_id, 1).startswith("https://objects.example/")
    parts = provider.list_parts(write_ref, upload_id)
    assert parts[0].etag == "part"
    assert provider.complete_multipart(write_ref, upload_id, parts).size_bytes == 0
    # These are real oss2 PartInfo instances, not the tuple shape accepted by
    # older test doubles. The production SDK sorts/accesses ``part_number``.
    assert bucket.completed_parts[0].part_number == 1
    assert bucket.completed_parts[0].etag == "part"
    assert bucket.completed_parts[0].size == 0
    assert bucket.multipart_headers == {
        "x-oss-meta-sha256": hashlib.sha256(b"").hexdigest(),
        "x-oss-forbid-overwrite": "true",
    }
    assert bucket.completed_headers == {"x-oss-forbid-overwrite": "true"}
    provider.abort_multipart(write_ref, upload_id)
    provider.delete_exact(ref)
    downloaded = provider.download_file(ref, str(tmp_path / "nested" / "empty.bin"))
    assert downloaded.sha256 == hashlib.sha256(b"").hexdigest()

    with pytest.raises(ObjectStorageError, match="leading slash"):
        provider.object_uri(StorageObjectRef("raw", "/empty", "v1", "etag", 0, None))

    bucket.object_exists = False
    bucket.headers = {"x-oss-meta-sha256": "wrong"}
    bucket.preserve_metadata = True
    with pytest.raises(ObjectStorageError, match="does not match source"):
        provider.put_worker_object(write_ref, str(source))

    bucket.object_exists = True
    bucket.preserve_metadata = False
    with pytest.raises(ObjectStorageError, match="already exists"):
        provider.put_worker_object(write_ref, str(source))

    bucket.object_exists = False
    bucket.complete_metadata = False
    with pytest.raises(ObjectStorageError, match="verified hash"):
        provider.complete_multipart(write_ref, upload_id, parts)


def test_aliyun_provider_classifies_a_missing_object():
    bucket = _AliyunBucket([])
    provider = AliyunObjectStorage(
        StorageBucketConfig.for_environment("test"),
        endpoint_url="http://worker.internal",
        access_key_id="access",
        access_key_secret="secret",
        bucket_factory=lambda _endpoint, _name: bucket,
    )

    with pytest.raises(StorageObjectNotFound):
        provider.head(StorageObjectRef("raw", "missing", "v1", "etag", 1, None))


def test_aliyun_provider_heads_the_frozen_version_identity():
    bucket = _AliyunBucket([])
    bucket.object_exists = True
    calls: list[tuple[str, dict]] = []
    original_head = bucket.head_object

    def record_head(key, **kwargs):
        calls.append((key, kwargs))
        return original_head(key, **kwargs)

    bucket.head_object = record_head
    provider = AliyunObjectStorage(
        StorageBucketConfig.for_environment("test"),
        endpoint_url="http://worker.internal",
        access_key_id="access",
        access_key_secret="secret",
        bucket_factory=lambda _endpoint, _name: bucket,
    )

    provider.head(StorageObjectRef("raw", "frozen", "v1", "etag", 0, None))

    assert calls == [("frozen", {"params": {"versionId": "v1"}})]


def test_aliyun_provider_reads_real_oss2_head_result_fields():
    """Keep identity extraction aligned with the installed oss2 response shape."""
    from oss2.models import HeadObjectResult

    result = HeadObjectResult(
        SimpleNamespace(
            status=200,
            request_id="request-id",
            headers={
                "content-length": "7",
                "etag": '"etag-1"',
                "x-oss-version-id": "version-1",
                "x-oss-meta-sha256": "a" * 64,
            },
        )
    )

    assert result.versionid == "version-1"
    assert AliyunObjectStorage._version_id(result) == "version-1"
    assert AliyunObjectStorage._metadata(result) == {"sha256": "a" * 64}


def test_aliyun_multipart_initialization_persists_declared_sha256():
    bucket = _AliyunBucket([])
    calls: list[tuple[str, dict | None]] = []

    def capture_initiate(key, headers=None, params=None):
        calls.append((key, headers))
        return SimpleNamespace(upload_id="upload-sha")

    bucket.init_multipart_upload = capture_initiate
    provider = AliyunObjectStorage(
        StorageBucketConfig.for_environment("test"),
        endpoint_url="http://worker.internal",
        access_key_id="access",
        access_key_secret="secret",
        bucket_factory=lambda _endpoint, _name: bucket,
    )
    digest = "a" * 64

    assert (
        provider.create_multipart(StorageObjectRef("raw", "frozen", None, "", 1, digest))
        == "upload-sha"
    )
    assert calls == [
        (
            "frozen",
            {"x-oss-forbid-overwrite": "true", "x-oss-meta-sha256": digest},
        )
    ]


def test_aliyun_download_classifies_conditional_identity_failure(tmp_path: Path):
    class _PreconditionFailure(Exception):
        status = 412
        code = "PreconditionFailed"

        def __str__(self):
            return "identity changed"

    bucket = _AliyunBucket([])
    bucket.get_object = lambda _key, **_kwargs: (_ for _ in ()).throw(_PreconditionFailure())
    provider = AliyunObjectStorage(
        StorageBucketConfig.for_environment("test"),
        endpoint_url="http://worker.internal",
        access_key_id="access",
        access_key_secret="secret",
        bucket_factory=lambda _endpoint, _name: bucket,
    )

    with pytest.raises(StorageObjectIntegrityError, match="identity changed"):
        provider.download_file(
            StorageObjectRef("raw", "changed", None, "etag", 1, None),
            str(tmp_path / "data.mcap"),
        )
