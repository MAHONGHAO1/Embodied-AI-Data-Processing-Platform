"""Raw browser uploads use exact, short-lived provider capabilities."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from data.config import settings
from data.infra import oss_client

RAW_KEY = (
    "raw/v2/workspaces/1/task-sets/2/batches/3/imports/"
    "11111111-1111-1111-1111-111111111111/original/4/capture.zip"
)


class FakeBucket:
    def __init__(self):
        self.calls = []

    def init_multipart_upload(self, key, headers=None):
        self.calls.append(("init", key, headers))
        return SimpleNamespace(upload_id="provider-upload-1")

    def sign_url(self, method, key, expires, headers=None, params=None, slash_safe=False):
        self.calls.append(("sign", method, key, expires, headers, params, slash_safe))
        return "https://raw-bucket.oss-cn-beijing.aliyuncs.com/signed?uploadId=provider-upload-1"

    def list_parts(self, key, upload_id, marker="", max_parts=1000):
        self.calls.append(("list", key, upload_id, marker, max_parts))
        return SimpleNamespace(
            parts=[SimpleNamespace(part_number=1, etag='"etag-1"', size=123)],
            is_truncated=False,
            next_marker="1",
        )

    def complete_multipart_upload(self, key, upload_id, parts, headers=None):
        self.calls.append(("complete", key, upload_id, parts, headers))

    def abort_multipart_upload(self, key, upload_id):
        self.calls.append(("abort", key, upload_id))


def _configure(monkeypatch):
    internal = FakeBucket()
    public = FakeBucket()
    monkeypatch.setattr(oss_client, "browser_direct_enabled", lambda: True)
    monkeypatch.setattr(oss_client, "browser_direct_ttl_seconds", lambda: 900)
    monkeypatch.setattr(
        oss_client, "bucket_name", lambda role: "raw-bucket" if role == "raw" else f"{role}-bucket"
    )
    monkeypatch.setattr(oss_client, "_get_bucket", lambda bucket: internal)
    monkeypatch.setattr(oss_client, "_get_browser_bucket", lambda bucket: public)
    return internal, public


def test_browser_multipart_provider_calls_are_bounded_to_one_raw_import_key(monkeypatch):
    internal, public = _configure(monkeypatch)

    upload_id = oss_client.init_browser_multipart_upload("raw-bucket", RAW_KEY)
    url, ttl, headers = oss_client.sign_browser_upload_part("raw-bucket", RAW_KEY, upload_id, 1)
    parts = oss_client.list_browser_multipart_parts("raw-bucket", RAW_KEY, upload_id)
    oss_client.complete_browser_multipart_upload("raw-bucket", RAW_KEY, upload_id, parts)
    oss_client.abort_browser_multipart_upload("raw-bucket", RAW_KEY, upload_id)

    assert upload_id == "provider-upload-1"
    assert url.startswith("https://")
    assert ttl == 900
    assert headers == {"Content-Type": "application/octet-stream"}
    assert parts == [oss_client.OSSMultipartPart(number=1, etag="etag-1", size=123)]
    assert internal.calls[0][0] == "init"
    assert internal.calls[0][2]["x-oss-forbid-overwrite"] == "true"
    assert public.calls[0][0:3] == ("sign", "PUT", RAW_KEY)
    assert public.calls[0][4] == headers
    assert public.calls[0][5] == {"uploadId": upload_id, "partNumber": "1"}
    assert internal.calls[-1] == ("abort", RAW_KEY, upload_id)


def test_browser_multipart_part_listing_uses_oss2_pagination_fields(monkeypatch):
    internal, _public = _configure(monkeypatch)

    def list_parts(key, upload_id, marker="", max_parts=1000):
        internal.calls.append(("list", key, upload_id, marker, max_parts))
        if marker == "":
            return SimpleNamespace(
                parts=[SimpleNamespace(part_number=1, etag='"etag-1"', size=100)],
                is_truncated=True,
                next_marker="1",
            )
        if marker == "1":
            return SimpleNamespace(
                parts=[SimpleNamespace(part_number=2, etag='"etag-2"', size=23)],
                is_truncated=False,
                next_marker="2",
            )
        pytest.fail(f"unexpected OSS part marker: {marker}")

    monkeypatch.setattr(internal, "list_parts", list_parts)

    parts = oss_client.list_browser_multipart_parts("raw-bucket", RAW_KEY, "provider-upload-1")

    assert parts == [
        oss_client.OSSMultipartPart(number=1, etag="etag-1", size=100),
        oss_client.OSSMultipartPart(number=2, etag="etag-2", size=23),
    ]
    assert [call[3] for call in internal.calls if call[0] == "list"] == ["", "1"]


@pytest.mark.parametrize(
    "bucket,key,upload_id,part_number",
    [
        ("other-bucket", RAW_KEY, "provider-upload-1", 1),
        ("raw-bucket", "raw/v2/workspaces/1/other.zip", "provider-upload-1", 1),
        ("raw-bucket", RAW_KEY, "bad\nupload", 1),
        ("raw-bucket", RAW_KEY, "provider-upload-1", 0),
        ("raw-bucket", RAW_KEY, "provider-upload-1", 10_001),
    ],
)
def test_browser_multipart_signing_rejects_scope_and_parameter_forgery(
    monkeypatch, bucket, key, upload_id, part_number
):
    _configure(monkeypatch)

    with pytest.raises(ValueError):
        oss_client.sign_browser_upload_part(bucket, key, upload_id, part_number)


PREVIEW_KEY = (
    "process/v2/workspaces/1/collection-uploads/"
    "11111111-1111-1111-1111-111111111111/0123456789abcdef0123456789abcdef/head_rgb.mp4"
)


def test_preview_parts_sign_content_md5_on_the_process_bucket(monkeypatch):
    _internal, public = _configure(monkeypatch)

    url, _ttl, headers = oss_client.sign_browser_upload_part(
        "process-bucket", PREVIEW_KEY, "provider-upload-1", 1, "1B2M2Y8AsgTpgAmY7PhCfg=="
    )

    assert url.startswith("https://")
    assert headers == {
        "Content-Type": "application/octet-stream",
        "Content-MD5": "1B2M2Y8AsgTpgAmY7PhCfg==",
    }
    assert public.calls[0][2] == PREVIEW_KEY
    assert public.calls[0][4] == headers


@pytest.mark.parametrize(
    "bucket,key,md5",
    [
        ("raw-bucket", PREVIEW_KEY, None),
        ("process-bucket", RAW_KEY, None),
        ("process-bucket", PREVIEW_KEY.replace("head_rgb.mp4", ".hidden"), None),
        ("process-bucket", PREVIEW_KEY, "not-base64"),
    ],
)
def test_preview_signing_rejects_wrong_bucket_key_or_digest(monkeypatch, bucket, key, md5):
    _configure(monkeypatch)
    with pytest.raises(ValueError):
        oss_client.sign_browser_upload_part(bucket, key, "provider-upload-1", 1, md5)


COLLECTION_KEY = (
    "raw/v2/workspaces/1/collection-uploads/11111111-1111-1111-1111-111111111111/upload.bin"
)
_LOOPBACK = "http://127.0.0.1:9000"
_LOOPBACK_URL = f"{_LOOPBACK}/quicstudio-e2e-raw/{COLLECTION_KEY}?X-Amz-Signature=abc"


class _LoopbackProvider:
    def sign_part(self, ref, upload_id, part_number, content_md5=None):
        assert content_md5 == "1B2M2Y8AsgTpgAmY7PhCfg=="
        return _LOOPBACK_URL


def _sign_loopback(monkeypatch, *, environment: str, test_mode: bool, endpoint: str = _LOOPBACK):
    if test_mode:
        monkeypatch.setenv("TEST_MODE", "true")
    else:
        # Assignment re-validates test_mode from the environment, so the flag
        # has to be absent before the field can stay false.
        monkeypatch.delenv("TEST_MODE", raising=False)
    monkeypatch.setattr(settings, "environment", environment)
    monkeypatch.setattr(settings, "test_mode", test_mode)
    monkeypatch.setattr(settings, "storage_endpoint", endpoint)
    monkeypatch.setattr(settings, "storage_browser_endpoint", endpoint)
    monkeypatch.setattr(
        oss_client,
        "_new_provider_ref",
        lambda bucket, key: (_LoopbackProvider(), SimpleNamespace()),
    )
    monkeypatch.setattr(
        oss_client, "bucket_name", lambda role: "raw-bucket" if role == "raw" else role
    )
    return oss_client.sign_browser_upload_part(
        "raw-bucket",
        COLLECTION_KEY,
        "upload-1",
        1,
        "1B2M2Y8AsgTpgAmY7PhCfg==",
    )


def test_development_signs_the_configured_loopback_minio_part_url(monkeypatch):
    url, ttl, headers = _sign_loopback(monkeypatch, environment="development", test_mode=False)

    assert url == _LOOPBACK_URL
    assert ttl == 900
    assert headers["Content-MD5"] == "1B2M2Y8AsgTpgAmY7PhCfg=="


@pytest.mark.parametrize(
    ("environment", "test_mode"),
    [("development", True), ("production", False), ("production", True)],
)
def test_loopback_part_url_stays_rejected_outside_development(monkeypatch, environment, test_mode):
    with pytest.raises(ValueError, match="invalid browser upload URL"):
        _sign_loopback(monkeypatch, environment=environment, test_mode=test_mode)


def test_development_rejects_a_loopback_url_for_a_different_endpoint(monkeypatch):
    with pytest.raises(ValueError, match="invalid browser upload URL"):
        _sign_loopback(
            monkeypatch,
            environment="development",
            test_mode=False,
            endpoint="http://127.0.0.1:19000",
        )
