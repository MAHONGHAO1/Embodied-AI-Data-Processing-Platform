from types import SimpleNamespace

import pytest
from oss2.models import HeadObjectResult

from data.infra import oss_client
from data.infra.aliyun_object_storage import AliyunObjectStorage
from data.infra.object_storage import StorageObjectRef
from data.services.storage_bucket_config import StorageBucketConfig


@pytest.mark.parametrize("crc64", ["1651923922661943303", "0", None])
def test_provider_object_info_preserves_oss_head_crc64(monkeypatch, crc64):
    headers = {"content-length": "7", "etag": '"final-etag"', "x-oss-version-id": "v1"}
    if crc64 is not None:
        headers["x-oss-hash-crc64ecma"] = crc64
    result = HeadObjectResult(SimpleNamespace(status=200, request_id="test", headers=headers))
    calls = []

    def head_object(key, params=None):
        calls.append((key, params))
        return result

    provider = AliyunObjectStorage(
        StorageBucketConfig.for_environment("test"),
        endpoint_url="https://oss-cn-beijing.aliyuncs.com",
        access_key_id="test-key",
        access_key_secret="test-secret",
        bucket_factory=lambda _endpoint, _name: SimpleNamespace(head_object=head_object),
    )
    ref = StorageObjectRef("raw", "sources/data.mcap", None, "", 0)
    monkeypatch.setattr(oss_client, "_new_provider_ref", lambda _bucket, _key: (provider, ref))
    info = oss_client.object_info("quicstudio-test-raw", ref.object_key)
    assert info.crc64 == crc64
    assert info.size == 7
    assert info.etag == "final-etag"
    assert info.version_id == "v1"
    assert calls == [(ref.object_key, None)]
