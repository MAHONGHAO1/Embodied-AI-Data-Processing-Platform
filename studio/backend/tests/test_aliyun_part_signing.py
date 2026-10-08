from urllib.parse import parse_qs, urlsplit

import oss2

from data.infra.aliyun_object_storage import AliyunObjectStorage
from data.infra.object_storage import StorageObjectRef
from data.services.storage_bucket_config import StorageBucketConfig


def test_multipart_signature_uses_string_query_values_and_binary_content_type(monkeypatch):
    monkeypatch.setattr("oss2.auth.time.time", lambda: 1_700_000_000)
    provider = AliyunObjectStorage(
        StorageBucketConfig.for_environment("test"),
        endpoint_url="https://oss-cn-beijing.aliyuncs.com",
        access_key_id="test-key",
        access_key_secret="test-secret",
    )
    ref = StorageObjectRef("raw", "sources/test/data.mcap", None, "", 10)
    signed = provider.sign_part(ref, "test-upload-id", 1)
    expected = oss2.Bucket(
        oss2.Auth("test-key", "test-secret"),
        "https://oss-cn-beijing.aliyuncs.com",
        "quicstudio-test-raw",
    ).sign_url(
        "PUT",
        ref.object_key,
        900,
        params={"partNumber": "1", "uploadId": "test-upload-id"},
        headers={"Content-Type": "application/octet-stream"},
    )
    assert signed == expected
    assert parse_qs(urlsplit(signed).query)["partNumber"] == ["1"]


def test_multipart_signature_covers_content_md5(monkeypatch):
    monkeypatch.setattr("oss2.auth.time.time", lambda: 1_700_000_000)
    provider = AliyunObjectStorage(
        StorageBucketConfig.for_environment("test"),
        endpoint_url="https://oss-cn-beijing.aliyuncs.com",
        access_key_id="test-key",
        access_key_secret="test-secret",
    )
    ref = StorageObjectRef("process", "process/v2/test/head_rgb.mp4", None, "", 10)
    signed = provider.sign_part(ref, "test-upload-id", 1, content_md5="1B2M2Y8AsgTpgAmY7PhCfg==")
    expected = oss2.Bucket(
        oss2.Auth("test-key", "test-secret"),
        "https://oss-cn-beijing.aliyuncs.com",
        "quicstudio-test-process",
    ).sign_url(
        "PUT",
        ref.object_key,
        900,
        params={"partNumber": "1", "uploadId": "test-upload-id"},
        headers={
            "Content-Type": "application/octet-stream",
            "Content-MD5": "1B2M2Y8AsgTpgAmY7PhCfg==",
        },
    )
    assert signed == expected

    # The digest is part of what gets signed, not just an inert header: a
    # different Content-MD5 must sign to a different URL, so a corrupted or
    # swapped part is rejected by the storage provider, not silently accepted.
    resigned_with_other_digest = provider.sign_part(
        ref, "test-upload-id", 1, content_md5="XUFAKrxLKna5cZ2REBfFkg=="
    )
    assert resigned_with_other_digest != signed
