from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def test_single_object_upload_with_forbid_overwrite_issues_one_put(tmp_path):
    from data.infra import oss_client

    class RecordingBucket:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, str] | None]] = []

        def put_object_from_file(
            self,
            key: str,
            path: str,
            *,
            headers: dict[str, str] | None = None,
        ) -> None:
            self.calls.append((key, path, headers))

    source = tmp_path / "official.json"
    source.write_text("official", encoding="utf-8")
    bucket = RecordingBucket()

    oss_client._put_object(
        bucket, "official/v1/episode/manifest.json", source, forbid_overwrite=True
    )

    assert len(bucket.calls) == 1
    assert bucket.calls[0][2] is not None
    assert bucket.calls[0][2]["x-oss-forbid-overwrite"] == "true"


def test_single_object_upload_accepts_only_safe_metadata(tmp_path):
    from data.infra import oss_client

    class RecordingBucket:
        def __init__(self) -> None:
            self.headers: dict[str, str] | None = None

        def put_object_from_file(self, _key: str, _path: str, *, headers=None) -> None:
            self.headers = headers

    source = tmp_path / "bundle.zip"
    source.write_bytes(b"zip")
    bucket = RecordingBucket()

    oss_client._put_object(
        bucket,
        "exports/v1/native-lerobot-bundles/demo.zip",
        source,
        forbid_overwrite=True,
        metadata={"x-oss-meta-sha256": "a" * 64},
    )

    assert bucket.headers is not None
    assert bucket.headers["x-oss-meta-sha256"] == "a" * 64
    with pytest.raises(ValueError, match="metadata"):
        oss_client._put_object(
            bucket,
            "exports/v1/native-lerobot-bundles/demo.zip",
            source,
            metadata={"x-oss-forbid-overwrite": "true"},
        )


def test_immutable_multipart_copy_keeps_source_condition_and_forbids_target_overwrite(monkeypatch):
    from data.infra import oss_client

    class PartInfo:
        def __init__(self, number: int, etag: str, size: int) -> None:
            self.part_number = number
            self.etag = etag
            self.size = size

    class SourceBucket:
        pass

    class TargetBucket:
        def __init__(self) -> None:
            self.init_headers: dict[str, str] | None = None
            self.part_calls: list[dict[str, object]] = []
            self.completed_headers: dict[str, str] | None = None

        def get_bucket_versioning(self):
            return SimpleNamespace(status="Disabled")

        def init_multipart_upload(self, _key: str, *, headers: dict[str, str]):
            self.init_headers = headers
            return SimpleNamespace(upload_id="upload-1")

        def upload_part_copy(
            self,
            source_bucket: str,
            source_key: str,
            byte_range: tuple[int, int],
            target_key: str,
            upload_id: str,
            part_number: int,
            *,
            headers: dict[str, str],
            params: dict[str, str] | None,
        ):
            self.part_calls.append(
                {
                    "source_bucket": source_bucket,
                    "source_key": source_key,
                    "byte_range": byte_range,
                    "target_key": target_key,
                    "upload_id": upload_id,
                    "part_number": part_number,
                    "headers": headers,
                    "params": params,
                }
            )
            return SimpleNamespace(etag=f'"part-{part_number}"')

        def complete_multipart_upload(self, _key, _upload_id, _parts, *, headers):
            self.completed_headers = headers

        def abort_multipart_upload(self, _key, _upload_id):
            pytest.fail("successful immutable copy must not abort")

    source = SourceBucket()
    target = TargetBucket()
    monkeypatch.setattr(
        oss_client,
        "_oss2",
        SimpleNamespace(models=SimpleNamespace(PartInfo=PartInfo)),
    )
    monkeypatch.setattr(
        oss_client,
        "_get_bucket",
        lambda bucket: source if bucket == "source" else target,
    )

    copied = oss_client.multipart_copy_immutable_object(
        "source",
        "prod/raw/robot/so101/demo/data/file.parquet",
        "target",
        "exports/v1/native-lerobot/data/file.parquet",
        source_size=128 * 1024 * 1024,
        source_etag='"source-etag"',
        source_version_id="v-1",
        copy_origin="a" * 64,
    )

    assert copied == "oss://target/exports/v1/native-lerobot/data/file.parquet"
    assert target.init_headers is not None
    assert target.init_headers["x-oss-forbid-overwrite"] == "true"
    assert target.init_headers["x-oss-meta-sha256"] == "a" * 64
    assert target.init_headers["x-oss-meta-quicdata-copy-origin"] == "a" * 64
    assert target.completed_headers == {"x-oss-forbid-overwrite": "true"}
    assert target.part_calls
    assert all(
        call["headers"] == {"x-oss-copy-source-if-match": '"source-etag"'}
        for call in target.part_calls
    )
    assert all(call["params"] == {"versionId": "v-1"} for call in target.part_calls)


def test_exact_download_rejects_missing_object_without_falling_back_to_prefix(
    monkeypatch, tmp_storage
):
    from data.infra import oss_client

    monkeypatch.setattr(oss_client, "object_info", lambda *_args: None)

    with pytest.raises(FileNotFoundError, match="object does not exist"):
        oss_client.download_object_to_file(
            tmp_storage / "tmp" / "native-lerobot" / "file.parquet",
            "target",
            "exports/v1/native-lerobot/data/file.parquet",
        )


def test_copy_object_replaces_target_metadata_when_requested(monkeypatch):
    from data.infra import oss_client

    class RecordingBucket:
        def __init__(self) -> None:
            self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def copy_object(self, *args, **kwargs) -> None:
            self.calls.append((args, kwargs))

    source = RecordingBucket()
    target = RecordingBucket()
    monkeypatch.setattr(
        oss_client,
        "_get_bucket",
        lambda bucket: source if bucket == "source" else target,
    )
    monkeypatch.setattr(oss_client, "_storage_config", lambda: {"keep_local_cache": False})

    oss_client.copy_object(
        "source",
        "prod/raw/robot/so101/demo/data/file.parquet",
        "target",
        "exports/v1/native-lerobot/data/file.parquet",
        source_etag='"source-etag"',
        forbid_overwrite=True,
        metadata={"x-oss-meta-sha256": "a" * 64},
    )

    assert len(target.calls) == 1
    headers = target.calls[0][1]["headers"]
    assert headers == {
        "x-oss-copy-source-if-match": '"source-etag"',
        "x-oss-forbid-overwrite": "true",
        "x-oss-metadata-directive": "REPLACE",
        "x-oss-meta-sha256": "a" * 64,
    }


def test_browser_direct_allows_nonraw_prefixes_when_roles_share_one_bucket(monkeypatch):
    from data.infra import oss_client

    monkeypatch.setattr(
        oss_client,
        "_storage_config",
        lambda: {
            "buckets": {
                "raw": "shared-bucket",
                "process": "shared-bucket",
                "official": "shared-bucket",
                "export": "shared-bucket",
            }
        },
    )

    assert oss_client.browser_direct_object_allowed(
        "shared-bucket", "process/v1/episode/preview.mp4"
    )
    assert oss_client.browser_direct_object_allowed(
        "shared-bucket", "process/ws1/proj1/task1/run/preview.mp4"
    )
    assert oss_client.browser_direct_object_allowed(
        "shared-bucket", "browser-previews/v1/episode/preview.mp4"
    )
    assert oss_client.browser_direct_object_allowed(
        "shared-bucket", "official/v1/episode/dataset.json"
    )
    assert oss_client.browser_direct_object_allowed(
        "shared-bucket", "datasets/ws1/proj1/dataset/archive.zip"
    )
    assert oss_client.browser_direct_object_allowed(
        "shared-bucket", "episodes/ws1/proj1/task1/job-123/dataset.json"
    )
    assert oss_client.browser_direct_object_allowed(
        "shared-bucket", "exports/v1/dataset/shard-000.mcap"
    )
    assert oss_client.browser_direct_object_allowed(
        "shared-bucket", "exports/ws1/proj1/ds1/v1/archive.zip"
    )
    assert not oss_client.browser_direct_object_allowed("shared-bucket", "raw/v1/episode/data.mcap")


def test_episode_preview_signing_failure_is_observable(monkeypatch):
    from data.infra import oss_client
    from data.services import episode_media_access

    monkeypatch.setattr(oss_client, "browser_direct_enabled", lambda: True)
    monkeypatch.setattr(oss_client, "bucket_name", lambda _role: "process-bucket")
    monkeypatch.setattr(oss_client, "browser_direct_object_allowed", lambda _bucket, _key: True)
    monkeypatch.setattr(oss_client, "object_exists", lambda _bucket, _key: True)
    monkeypatch.setattr(
        oss_client,
        "sign_browser_get_url",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("OSS unavailable")),
    )
    warning = Mock()
    monkeypatch.setattr(episode_media_access.logger, "warning", warning)

    access = episode_media_access.issue_episode_preview_url(
        "oss://process-bucket/process/v1/episode/preview.mp4",
        download_name="preview.mp4",
        media_type="video/mp4",
    )

    assert access is None
    warning.assert_called_once_with(
        "episode preview direct signing failed error_type=%s",
        "RuntimeError",
    )


def test_episode_preview_signing_timeout_degrades_without_raising(monkeypatch):
    import time

    from data.infra import oss_client
    from data.services import episode_media_access

    monkeypatch.setattr(oss_client, "browser_direct_enabled", lambda: True)
    monkeypatch.setattr(oss_client, "bucket_name", lambda _role: "process-bucket")
    monkeypatch.setattr(oss_client, "browser_direct_object_allowed", lambda _bucket, _key: True)

    def _hang(*_args, **_kwargs):
        time.sleep(2)
        return True

    monkeypatch.setattr(oss_client, "object_exists", _hang)
    warning = Mock()
    monkeypatch.setattr(episode_media_access.logger, "warning", warning)

    access = episode_media_access.issue_episode_preview_url(
        "oss://process-bucket/process/v1/episode/preview.mp4",
        download_name="preview.mp4",
        media_type="video/mp4",
        timeout_seconds=0.2,
    )

    assert access is None
    warning.assert_called_once_with("episode preview direct signing timed out")
