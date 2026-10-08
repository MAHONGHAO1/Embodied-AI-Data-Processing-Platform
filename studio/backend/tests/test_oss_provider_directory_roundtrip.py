from __future__ import annotations

from pathlib import Path

import pytest

from data.infra.aliyun_object_storage import AliyunObjectStorage
from data.infra.object_storage import ObjectStorageError, StorageListPage, StorageObjectRef
from data.infra.s3_object_storage import S3ObjectStorage
from data.services.storage_bucket_config import StorageBucketConfig


class DirectoryProvider:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects

    def head(self, ref):
        if ref.object_key not in self.objects:
            raise ObjectStorageError("S3 head_object failed: 404 not found")
        payload = self.objects[ref.object_key]
        return StorageObjectRef(ref.bucket_role, ref.object_key, None, "etag", len(payload), None)

    def put_worker_object(self, ref, source_path):
        payload = Path(source_path).read_bytes()
        if ref.object_key in self.objects:
            raise ObjectStorageError("object key already exists")
        self.objects[ref.object_key] = payload
        return StorageObjectRef(ref.bucket_role, ref.object_key, None, "etag", len(payload), None)

    def list_prefix(self, role, prefix, *, continuation_token, max_keys):
        keys = sorted(key for key in self.objects if key.startswith(prefix.rstrip("/") + "/"))
        start = int(continuation_token or 0)
        page = keys[start : start + max_keys]
        token = str(start + max_keys) if start + max_keys < len(keys) else None
        return StorageListPage(
            tuple(
                StorageObjectRef(role, key, None, "etag", len(self.objects[key]), None)
                for key in page
            ),
            token,
        )

    def download_file(self, ref, destination):
        Path(destination).write_bytes(self.objects[ref.object_key])
        return ref


def test_download_to_materializes_provider_directory_pages_and_excludes_siblings(
    tmp_path, monkeypatch
):
    from data.infra import oss_client

    provider = DirectoryProvider(
        {"bundle/a/x.bin": b"x", "bundle/b/y.bin": b"y", "bundlex/no.bin": b"no"}
    )
    monkeypatch.setattr(oss_client.settings, "oss_bucket_raw", "raw-bucket")
    monkeypatch.setattr(
        oss_client,
        "_new_provider_ref",
        lambda bucket, key: (provider, StorageObjectRef("raw", key, None, "", 0)),
    )
    monkeypatch.setattr(oss_client, "_get_bucket", lambda _bucket: None)
    monkeypatch.setattr(oss_client.settings, "storage_root", str(tmp_path))
    destination = tmp_path / "out"

    oss_client.download_to(destination, "raw-bucket", "bundle")

    assert (destination / "a/x.bin").read_bytes() == b"x"
    assert (destination / "b/y.bin").read_bytes() == b"y"
    assert not (destination / "../bundlex/no.bin").exists()


def test_upload_file_directory_round_trips_through_provider_facade(tmp_path, monkeypatch):
    from data.infra import oss_client

    provider = DirectoryProvider({})
    monkeypatch.setattr(oss_client.settings, "oss_bucket_raw", "raw-bucket")
    monkeypatch.setattr(
        oss_client,
        "_new_provider_ref",
        lambda bucket, key: (provider, StorageObjectRef("raw", key, None, "", 0)),
    )
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "nested/a.bin").write_bytes(b"a-bytes")
    (source / "root.txt").write_bytes(b"root-bytes")
    uploaded = oss_client.upload_file(source, "raw-bucket", "bundle")
    assert uploaded == "oss://raw-bucket/bundle"
    destination = tmp_path / "roundtrip"
    monkeypatch.setattr(oss_client.settings, "storage_root", str(tmp_path))
    oss_client.download_to(destination, "raw-bucket", "bundle")
    assert (destination / "nested/a.bin").read_bytes() == b"a-bytes"
    assert (destination / "root.txt").read_bytes() == b"root-bytes"


def test_download_to_rejects_missing_provider_prefix(tmp_path, monkeypatch):
    from data.infra import oss_client

    provider = DirectoryProvider({})
    monkeypatch.setattr(oss_client.settings, "oss_bucket_raw", "raw-bucket")
    monkeypatch.setattr(
        oss_client,
        "_new_provider_ref",
        lambda bucket, key: (provider, StorageObjectRef("raw", key, None, "", 0)),
    )
    monkeypatch.setattr(oss_client.settings, "storage_root", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        oss_client.download_to(tmp_path / "out", "raw-bucket", "missing")


def test_download_to_rejects_unsafe_provider_relative_key_before_write(tmp_path, monkeypatch):
    from data.infra import oss_client

    provider = DirectoryProvider({"bundle/../escape.bin": b"escape"})
    monkeypatch.setattr(oss_client.settings, "oss_bucket_raw", "raw-bucket")
    monkeypatch.setattr(
        oss_client,
        "_new_provider_ref",
        lambda bucket, key: (provider, StorageObjectRef("raw", key, None, "", 0)),
    )
    monkeypatch.setattr(oss_client.settings, "storage_root", str(tmp_path))
    with pytest.raises(ValueError, match="escaped|invalid"):
        oss_client.download_to(tmp_path / "out", "raw-bucket", "bundle")


def test_s3_provider_lists_bounded_pages_with_directory_boundary():
    class Client:
        def __init__(self):
            self.calls = []

        def list_objects_v2(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("ContinuationToken"):
                return {
                    "Contents": [{"Key": "bundle/b.bin", "Size": 2, "ETag": '"b"'}],
                    "IsTruncated": False,
                }
            return {
                "Contents": [
                    {"Key": "bundle/a.bin", "Size": 1, "ETag": '"a"'},
                    {"Key": "bundlex/no.bin", "Size": 3, "ETag": '"x"'},
                ],
                "IsTruncated": True,
                "NextContinuationToken": "next",
            }

    client = Client()
    provider = S3ObjectStorage(StorageBucketConfig.for_environment("test"), client=client)
    first = provider.list_prefix("raw", "bundle", continuation_token=None, max_keys=1)
    second = provider.list_prefix("raw", "bundle", continuation_token=first.next_token, max_keys=1)
    assert [item.object_key for item in first.objects] == ["bundle/a.bin"]
    assert [item.object_key for item in second.objects] == ["bundle/b.bin"]
    assert client.calls[0]["Prefix"] == "bundle/"


def test_facade_exact_provider_object_downloads_with_identity(tmp_path, monkeypatch):
    from data.infra import oss_client

    provider = DirectoryProvider({"exact.bin": b"exact-bytes"})
    monkeypatch.setattr(oss_client.settings, "oss_bucket_raw", "raw-bucket")
    monkeypatch.setattr(
        oss_client,
        "_new_provider_ref",
        lambda bucket, key: (provider, StorageObjectRef("raw", key, None, "", 0)),
    )
    monkeypatch.setattr(oss_client.settings, "storage_root", str(tmp_path))
    oss_client.download_to(tmp_path / "out", "raw-bucket", "exact.bin")
    assert (tmp_path / "out/exact.bin").read_bytes() == b"exact-bytes"


def test_facade_prefix_delivery_error_leaves_destination_empty(tmp_path, monkeypatch):
    from data.infra import oss_client

    class FailingProvider(DirectoryProvider):
        def download_file(self, ref, destination):
            raise ObjectStorageError("delivery failed")

    provider = FailingProvider({"bundle/a.bin": b"a", "bundle/b.bin": b"b"})
    monkeypatch.setattr(oss_client.settings, "oss_bucket_raw", "raw-bucket")
    monkeypatch.setattr(
        oss_client,
        "_new_provider_ref",
        lambda bucket, key: (provider, StorageObjectRef("raw", key, None, "", 0)),
    )
    monkeypatch.setattr(oss_client.settings, "storage_root", str(tmp_path))
    destination = tmp_path / "out"
    with pytest.raises(ObjectStorageError, match="delivery failed"):
        oss_client.download_to(destination, "raw-bucket", "bundle")
    assert list(destination.rglob("*")) == []


def test_facade_prefix_rejects_existing_file_ancestor_before_download(tmp_path, monkeypatch):
    from data.infra import oss_client

    provider = DirectoryProvider({"bundle/nested/a.bin": b"a"})
    monkeypatch.setattr(oss_client.settings, "oss_bucket_raw", "raw-bucket")
    monkeypatch.setattr(
        oss_client,
        "_new_provider_ref",
        lambda bucket, key: (provider, StorageObjectRef("raw", key, None, "", 0)),
    )
    monkeypatch.setattr(oss_client.settings, "storage_root", str(tmp_path))
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / "nested").write_bytes(b"existing-file")
    with pytest.raises(ValueError, match="existing file"):
        oss_client.download_to(destination, "raw-bucket", "bundle")
    assert (destination / "nested").read_bytes() == b"existing-file"


def test_provider_factory_error_is_not_downgraded_to_legacy_mirror(tmp_path, monkeypatch):
    from data.infra import oss_client

    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    monkeypatch.setattr(oss_client.settings, "oss_bucket_raw", "raw-bucket")
    monkeypatch.setattr(oss_client.settings, "test_mode", False)
    monkeypatch.setattr(oss_client.settings, "storage_provider", "minio")
    monkeypatch.setattr(oss_client.settings, "storage_endpoint", "http://storage")
    monkeypatch.setattr(oss_client.settings, "storage_access_key_id", "access")
    monkeypatch.setattr(oss_client.settings, "storage_secret_access_key", "secret")
    monkeypatch.setattr(oss_client, "_storage_config", lambda: {"buckets": {"raw": "raw-bucket"}})
    from data.infra import storage_provider

    monkeypatch.setattr(
        storage_provider,
        "get_storage_provider",
        lambda: (_ for _ in ()).throw(RuntimeError("factory failed")),
    )
    with pytest.raises(RuntimeError, match="factory failed"):
        oss_client.upload_file(source, "raw-bucket", "exact.bin")


@pytest.mark.parametrize("operation", ["upload", "download"])
def test_external_bucket_is_rejected_even_with_incomplete_or_unknown_provider_config(
    tmp_path, monkeypatch, operation
):
    from data.infra import oss_client

    monkeypatch.setattr(oss_client.settings, "storage_endpoint", "")
    monkeypatch.setattr(oss_client.settings, "storage_access_key_id", "")
    monkeypatch.setattr(oss_client.settings, "storage_secret_access_key", "")
    monkeypatch.setattr(oss_client.settings, "storage_provider", "unknown")
    if operation == "upload":
        source = tmp_path / "source.bin"
        source.write_bytes(b"source")
        with pytest.raises(ValueError, match="provider boundary"):
            oss_client.upload_file(source, "external-bucket", "key")
    else:
        with pytest.raises(ValueError, match="provider boundary"):
            oss_client.download_to(tmp_path / "out", "external-bucket", "prefix")


def test_aliyun_provider_lists_pages_and_preserves_provider_errors():
    class Item:
        def __init__(self, key, size, etag):
            self.key, self.size, self.etag = key, size, etag

    class Result:
        object_list = [Item("bundle/a.bin", 1, '"a"'), Item("bundlex/no.bin", 2, '"x"')]
        is_truncated = False
        next_marker = None

    class Bucket:
        def list_objects(self, **kwargs):
            assert kwargs["prefix"] == "bundle/"
            return Result()

    provider = AliyunObjectStorage(
        StorageBucketConfig.for_environment("test"),
        endpoint_url="http://oss.test",
        access_key_id="access",
        access_key_secret="secret",
        bucket_factory=lambda *_args: Bucket(),
    )
    page = provider.list_prefix("raw", "bundle", continuation_token=None, max_keys=10)
    assert [item.object_key for item in page.objects] == ["bundle/a.bin"]

    class FailingBucket:
        def list_objects(self, **_kwargs):
            raise RuntimeError("delivery unavailable")

    failing = AliyunObjectStorage(
        StorageBucketConfig.for_environment("test"),
        endpoint_url="http://oss.test",
        access_key_id="access",
        access_key_secret="secret",
        bucket_factory=lambda *_args: FailingBucket(),
    )
    with pytest.raises(ObjectStorageError, match="delivery unavailable"):
        failing.list_prefix("raw", "bundle", continuation_token=None, max_keys=10)
