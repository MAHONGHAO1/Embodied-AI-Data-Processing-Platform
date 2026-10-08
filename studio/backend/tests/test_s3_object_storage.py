from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from data.infra.object_storage import (
    StorageNotReady,
    StorageObjectNotFound,
    StorageObjectRef,
)
from data.infra.s3_object_storage import S3ObjectStorage
from data.services.storage_bucket_config import StorageBucketConfig


class FakeS3:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.persisted_size = 42
        self.persisted_sha = "sha-1"
        self.object_exists = False

    def create_multipart_upload(self, **kwargs):
        self.calls.append(("create", kwargs))
        return {"UploadId": "upload-1"}

    def generate_presigned_url(self, **kwargs):
        self.calls.append(("sign", kwargs))
        return "http://minio.test/upload-part"

    def list_parts(self, **kwargs):
        self.calls.append(("list", kwargs))
        return {
            "Parts": [{"PartNumber": 2, "ETag": '"etag-2"', "Size": 20}],
            "IsTruncated": False,
        }

    def list_multipart_uploads(self, **kwargs):
        self.calls.append(("list_uploads", kwargs))
        return {
            "Uploads": [
                {"Key": "uploads/a.zip", "UploadId": "exact-upload"},
                {"Key": "uploads/a.zip.suffix", "UploadId": "prefix-collision"},
            ],
            "IsTruncated": False,
        }

    def complete_multipart_upload(self, **kwargs):
        self.calls.append(("complete", kwargs))
        self.object_exists = True
        return {"ETag": '"complete"'}

    def abort_multipart_upload(self, **kwargs):
        self.calls.append(("abort", kwargs))

    def head_object(self, **kwargs):
        self.calls.append(("head", kwargs))
        if not self.object_exists:
            raise ClientError(
                {"Error": {"Code": "404", "Message": "not found"}},
                "HeadObject",
            )
        return {
            "ETag": '"head-etag"',
            "ContentLength": self.persisted_size,
            "VersionId": "version-1",
            "Metadata": {"sha256": self.persisted_sha},
        }

    def upload_file(self, **kwargs):
        self.calls.append(("upload", kwargs))
        payload = Path(kwargs["Filename"]).read_bytes()
        self.persisted_size = len(payload)
        self.persisted_sha = hashlib.sha256(payload).hexdigest()
        self.object_exists = True

    def put_object(self, **kwargs):
        self.calls.append(("put", kwargs))
        if self.object_exists:
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed", "Message": "exists"}},
                "PutObject",
            )
        payload = kwargs["Body"].read()
        self.persisted_size = len(payload)
        self.persisted_sha = kwargs["Metadata"]["sha256"]
        self.object_exists = True

    def delete_object(self, **kwargs):
        self.calls.append(("delete", kwargs))


def _ref() -> StorageObjectRef:
    return StorageObjectRef("raw", "uploads/a.zip", None, "", 0, "sha-1")


def test_s3_provider_uses_logical_role_and_signs_multipart_parts():
    fake = FakeS3()
    provider = S3ObjectStorage(StorageBucketConfig.for_environment("dev"), client=fake)
    ref = _ref()

    assert provider.create_multipart(ref) == "upload-1"
    assert provider.sign_part(ref, "upload-1", 1).endswith("upload-part")
    parts = provider.list_parts(ref, "upload-1")
    assert len(parts) == 1
    assert parts[0].number == 2
    assert parts[0].etag == "etag-2"
    assert fake.calls[1][1]["Bucket"] == "quicstudio-dev-raw"
    assert fake.calls[2][1]["Params"]["PartNumber"] == 1


def test_s3_provider_lists_only_exact_key_multipart_uploads():
    fake = FakeS3()
    provider = S3ObjectStorage(StorageBucketConfig.for_environment("test"), client=fake)

    assert provider.list_multipart_upload_ids(_ref()) == ("exact-upload",)
    assert fake.calls[-1] == (
        "list_uploads",
        {"Bucket": "quicstudio-test-raw", "Prefix": "uploads/a.zip"},
    )


def test_s3_provider_rejects_same_key_worker_overwrite(tmp_path: Path):
    fake = FakeS3()
    provider = S3ObjectStorage(StorageBucketConfig.for_environment("test"), client=fake)
    source = tmp_path / "worker.bin"
    source.write_bytes(b"worker")
    ref = StorageObjectRef("raw", "uploads/same-key", None, "", 0, None)

    provider.put_worker_object(ref, str(source))
    with pytest.raises(Exception, match="already exists|PreconditionFailed"):
        provider.put_worker_object(ref, str(source))


def test_s3_provider_rejects_leading_slash_in_object_key():
    provider = S3ObjectStorage(StorageBucketConfig.for_environment("test"), client=FakeS3())
    ref = StorageObjectRef("raw", "/uploads/key", None, "", 0, None)

    with pytest.raises(Exception, match="leading slash"):
        provider.object_uri(ref)


def test_s3_provider_classifies_a_missing_object():
    provider = S3ObjectStorage(StorageBucketConfig.for_environment("test"), client=FakeS3())

    with pytest.raises(StorageObjectNotFound):
        provider.head(_ref())


def test_s3_provider_heads_the_frozen_version_identity():
    fake = FakeS3()
    fake.object_exists = True
    provider = S3ObjectStorage(StorageBucketConfig.for_environment("test"), client=fake)

    provider.head(
        StorageObjectRef("raw", "uploads/a.zip", "frozen-version", "head-etag", 42, "sha-1")
    )

    assert fake.calls[-1] == (
        "head",
        {
            "Bucket": "quicstudio-test-raw",
            "Key": "uploads/a.zip",
            "VersionId": "frozen-version",
        },
    )


def test_s3_provider_completes_heads_uploads_and_deletes_exact_object(tmp_path: Path):
    fake = FakeS3()
    provider = S3ObjectStorage(StorageBucketConfig.for_environment("test"), client=fake)
    ref = _ref()
    parts = provider.list_parts(ref, "upload-1")

    completed = provider.complete_multipart(ref, "upload-1", parts)
    assert completed.version_id == "version-1"
    assert completed.size_bytes == 42
    assert completed.sha256 == "sha-1"

    source = tmp_path / "worker.bin"
    source.write_bytes(b"worker")
    with pytest.raises(Exception, match="already exists|PreconditionFailed"):
        provider.put_worker_object(ref, str(source))
    provider.delete_exact(completed)
    assert fake.calls[-1] == (
        "delete",
        {"Bucket": "quicstudio-test-raw", "Key": "uploads/a.zip", "VersionId": "version-1"},
    )


def test_s3_provider_requires_credentials_when_not_injected():
    with pytest.raises(StorageNotReady):
        S3ObjectStorage(StorageBucketConfig.for_environment("dev"), endpoint_url="http://minio")


def test_s3_part_signature_includes_content_md5():
    fake = FakeS3()
    provider = S3ObjectStorage(StorageBucketConfig.for_environment("dev"), client=fake)

    provider.sign_part(_ref(), "upload-1", 1, content_md5="1B2M2Y8AsgTpgAmY7PhCfg==")

    assert fake.calls[-1][1]["Params"]["ContentMD5"] == "1B2M2Y8AsgTpgAmY7PhCfg=="
