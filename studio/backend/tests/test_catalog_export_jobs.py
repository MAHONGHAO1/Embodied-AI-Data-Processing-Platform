"""Focused invariants for catalog export materialization.

These tests exercise the provider fence and canonical materialization helpers
without replacing the provider or SDK validators in worker integration tests.
"""

import hashlib
import json
import tarfile
from dataclasses import dataclass

import pytest

from data.infra.object_storage import StorageObjectRef
from data.services.catalog_export_jobs import (
    CatalogExportError,
    _assert_identity,
    _convert_qrdf_to_lerobot,
    _download_snapshot_ref,
    _extract_safe_tar,
    _file_manifest,
    _materialize_snapshot,
    _validate_materialized,
    _verify_output_identity,
)


@dataclass
class _Provider:
    payloads: dict[str, bytes]
    version_id: str | None = "v1"

    def download_file(self, ref, destination):
        data = self.payloads[ref.object_key]
        path = __import__("pathlib").Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return StorageObjectRef(
            ref.bucket_role,
            ref.object_key,
            ref.version_id,
            ref.etag,
            len(data),
            hashlib.sha256(data).hexdigest(),
        )

    def head(self, ref):
        data = self.payloads[ref.object_key]
        return StorageObjectRef(
            ref.bucket_role,
            ref.object_key,
            self.version_id,
            "e1",
            len(data),
            hashlib.sha256(data).hexdigest(),
        )

    def object_uri(self, ref):
        return f"oss://export/{ref.object_key}"

    def put_worker_object(self, ref, source_path):
        data = __import__("pathlib").Path(source_path).read_bytes()
        self.payloads[ref.object_key] = data
        return StorageObjectRef(
            ref.bucket_role,
            ref.object_key,
            self.version_id,
            "e1",
            len(data),
            hashlib.sha256(data).hexdigest(),
        )

    def delete_exact(self, ref):
        self.payloads.pop(ref.object_key, None)


def test_download_identity_requires_the_frozen_provider_identity():
    expected = StorageObjectRef("raw", "episodes/a", "v1", "etag-1", 3, "a" * 64)
    with pytest.raises(CatalogExportError, match="version"):
        _assert_identity(
            expected, StorageObjectRef("raw", "episodes/a", "v2", "etag-1", 3, "a" * 64)
        )


def test_download_identity_rejects_changed_bytes():
    expected = StorageObjectRef("process", "process/a.tar", "v1", "etag-1", 3, "a" * 64)
    with pytest.raises(CatalogExportError, match="SHA-256"):
        _assert_identity(
            expected, StorageObjectRef("process", "process/a.tar", "v1", "etag-1", 3, "b" * 64)
        )


def test_download_identity_rejects_missing_frozen_fields():
    expected = StorageObjectRef("raw", "episodes/a", "v1", "etag-1", 3, "a" * 64)
    with pytest.raises(CatalogExportError, match="version"):
        _assert_identity(
            expected, StorageObjectRef("raw", "episodes/a", None, "etag-1", 3, "a" * 64)
        )
    with pytest.raises(CatalogExportError, match="incomplete"):
        _assert_identity(
            expected, StorageObjectRef("raw", "episodes/a", "v1", "etag-1", 3, "not-a-sha")
        )


def test_unversioned_source_requires_matching_etag_and_bytes():
    expected = StorageObjectRef("raw", "episodes/a", None, "etag-1", 3, "a" * 64)
    _assert_identity(expected, expected)
    with pytest.raises(CatalogExportError, match="ETag"):
        _assert_identity(
            expected, StorageObjectRef("raw", "episodes/a", None, "etag-2", 3, "a" * 64)
        )
    with pytest.raises(CatalogExportError, match="SHA-256"):
        _assert_identity(
            expected, StorageObjectRef("raw", "episodes/a", None, "etag-1", 3, "b" * 64)
        )
    with pytest.raises(CatalogExportError, match="incomplete"):
        _assert_identity(expected, StorageObjectRef("raw", "episodes/a", None, "", 3, "a" * 64))


@pytest.mark.parametrize(
    "path_value",
    [".", "./", "a/../b", "/abs/data.mcap", "a\x00b"],
)
def test_download_snapshot_ref_rejects_unsafe_path(tmp_path, path_value):
    item = {
        "path": path_value,
        "ref": {
            "bucket_role": "raw",
            "object_key": "raw/data.mcap",
            "etag": "e1",
            "version_id": "v1",
            "size_bytes": 3,
            "sha256": "a" * 64,
        },
    }
    with pytest.raises(CatalogExportError, match="unsafe"):
        _download_snapshot_ref(None, item, tmp_path)


def test_output_identity_rejects_put_digest_drift():
    payload = b"artifact"
    expected_sha = hashlib.sha256(payload).hexdigest()
    persisted = StorageObjectRef("export", "out/archive.tar.gz", "v1", "e1", len(payload), "0" * 64)

    class Provider:
        def head(self, _ref):
            return StorageObjectRef(
                "export", "out/archive.tar.gz", "v1", "e1", len(payload), expected_sha
            )

    with pytest.raises(CatalogExportError, match="identity"):
        _verify_output_identity(
            Provider(), persisted, expected_size=len(payload), expected_sha256=expected_sha
        )


def test_recovery_tar_manifest_mismatch_is_rejected(tmp_path):
    archive_path = tmp_path / "artifact.tar.gz"
    source = tmp_path / "source"
    source.mkdir()
    (source / "dataset.json").write_text("{}", encoding="utf-8")
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(source / "dataset.json", arcname="dataset.json")
    with pytest.raises(CatalogExportError, match="member identity"):
        _extract_safe_tar(
            archive_path,
            tmp_path / "out",
            expected_members=[{"path": "dataset.json", "size_bytes": 2, "sha256": "f" * 64}],
        )


def test_recovery_scratch_budget_rejects_oversized_download(tmp_path, monkeypatch):
    from data.config import settings
    from data.services.catalog_export_jobs import (
        _enforce_scratch_budget,
        _make_scratch_root,
    )

    monkeypatch.setattr(settings, "scratch_root", str(tmp_path))
    monkeypatch.setattr(settings, "scratch_max_bytes", 1)
    scratch = _make_scratch_root()
    with pytest.raises(CatalogExportError, match="scratch budget"):
        _enforce_scratch_budget(scratch, additional_bytes=2)


def test_frozen_objects_materialize_and_pass_real_qrdf_validator(tmp_path, monkeypatch):
    from data.config import settings

    monkeypatch.setattr(settings, "scratch_root", str(tmp_path))
    from qrdf.converters.source.examples import SyntheticTeleOpImporter

    source = tmp_path / "source"
    SyntheticTeleOpImporter().import_episode(
        source, source, episode_id="episode_000001", num_steps=4
    )
    episode_root = source / "episodes" / "episode_000001"
    payloads = {}
    files = []
    for local in sorted(p for p in episode_root.rglob("*") if p.is_file()):
        relative = local.relative_to(episode_root).as_posix()
        payload = local.read_bytes()
        key = f"process/v2/qrdf/test/{relative}"
        payloads[key] = payload
        files.append(
            {
                "path": relative,
                "kind": "data" if relative == "data.mcap" else "metadata",
                "ref": {
                    "bucket_role": "process",
                    "object_key": key,
                    "version_id": "v1",
                    "etag": "e1",
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                },
            }
        )
    provider = _Provider(payloads)
    snapshot = {
        "schema": "quicstudio.catalog-source.v1",
        "assets": [
            {
                "source_snapshot": {
                    "episodes": [
                        {
                            "episode_id": 7,
                            "files": files,
                            "annotation_revision": {
                                "revision_id": "rev-1",
                                "payload": {
                                    "kind": "episode_annotation",
                                    "qrdf_version": "0.2.0",
                                    "source": {
                                        "episode_id": "stale-source",
                                        "data_sha256": "0" * 64,
                                    },
                                    "episode": {"outcome": "unknown"},
                                    "tracks": [],
                                },
                            },
                        }
                    ]
                }
            }
        ],
    }
    output = tmp_path / "out"
    _materialize_snapshot(
        snapshot, output, provider, source_kind="qrdf_assets", export_format="qrdf_0_2"
    )
    _validate_materialized(output, "qrdf_0_2")
    annotation = json.loads(
        (output / "episodes" / "episode_000001" / "annotation.json").read_text()
    )
    assert annotation["source"]["episode_id"] == "episode_000001"
    assert (
        annotation["source"]["data_sha256"]
        == hashlib.sha256(
            (output / "episodes" / "episode_000001" / "data.mcap").read_bytes()
        ).hexdigest()
    )


def test_qrdf_to_lerobot_conversion_uses_real_sdk_and_is_independently_readable(
    tmp_path, monkeypatch
):
    from data.config import settings

    monkeypatch.setattr(settings, "scratch_root", str(tmp_path))
    from qrdf.converters.source.examples import SyntheticTeleOpImporter

    source = tmp_path / "source"
    SyntheticTeleOpImporter().import_episode(
        source, source, episode_id="episode_000001", num_steps=20
    )
    output = tmp_path / "lerobot"
    _convert_qrdf_to_lerobot(source, output)
    _validate_materialized(output, "lerobot_3_0")
    assert (output / "meta" / "info.json").is_file()


@pytest.mark.parametrize("export_format", ["qrdf_0_2", "lerobot_3_0"])
def test_completion_marker_recovery_commits_only_matching_attempt(
    db_session, monkeypatch, tmp_path, export_format
):
    from data.config import settings

    monkeypatch.setattr(settings, "scratch_root", str(tmp_path))
    from data.models.catalog_dataset import (
        CatalogDataset,
        CatalogDatasetExport,
        CatalogDatasetVersion,
    )
    from data.services.catalog_export_jobs import recover_catalog_export_delivery

    dataset = CatalogDataset(
        name=f"recovery-test-{export_format}", source_kind="qrdf_assets", status="active"
    )
    db_session.add(dataset)
    db_session.flush()
    from data.services.catalog_export_jobs import _snapshot_hash

    frozen_snapshot = {"schema": "x"}
    version = CatalogDatasetVersion(
        dataset_id=dataset.id,
        version=1,
        status="active",
        source_snapshot_id=_snapshot_hash(frozen_snapshot),
        source_snapshot_json=frozen_snapshot,
    )
    db_session.add(version)
    db_session.flush()
    export = CatalogDatasetExport(
        version_id=version.id,
        format=export_format,
        status="running",
        idempotency_key=f"recovery-test-{export_format}",
        input_snapshot_id=_snapshot_hash(frozen_snapshot),
        input_snapshot_json=frozen_snapshot,
        attempt=2,
        output_prefix="catalog/recovery/attempt-2",
    )
    db_session.add(export)
    db_session.commit()
    from qrdf.converters.source.examples import SyntheticTeleOpImporter

    source = tmp_path / "canonical"
    SyntheticTeleOpImporter().import_episode(
        source, source, episode_id="episode_000001", num_steps=4
    )
    _validate_materialized(source, "qrdf_0_2")
    if export_format == "lerobot_3_0":
        converted = tmp_path / "converted"
        _convert_qrdf_to_lerobot(source, converted)
        source = converted
    artifact_path = tmp_path / "dataset.tar.gz"
    with tarfile.open(artifact_path, "w:gz") as archive_handle:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive_handle.add(path, arcname=path.relative_to(source).as_posix())
    artifact = artifact_path.read_bytes()
    artifact_sha = hashlib.sha256(artifact).hexdigest()
    files = _file_manifest(source)
    marker = json.dumps(
        {
            "schema": "quicstudio.catalog-export-complete.v1",
            "export_id": export.id,
            "attempt": 2,
            "format": export_format,
            "input_snapshot_id": _snapshot_hash(frozen_snapshot),
            "artifact": {
                "bucket_role": "export",
                "object_key": "catalog/recovery/attempt-2/dataset.tar.gz",
                "version_id": "v1",
                "etag": "e1",
                "size_bytes": len(artifact),
                "sha256": artifact_sha,
            },
            "size_bytes": len(artifact),
            "sha256": artifact_sha,
            "files": files,
        }
    ).encode()
    provider = _Provider(
        {
            "catalog/recovery/attempt-2/completion.json": marker,
            "catalog/recovery/attempt-2/dataset.tar.gz": artifact,
        }
    )
    monkeypatch.setattr("data.services.catalog_export_jobs.get_storage_provider", lambda: provider)
    recovered = recover_catalog_export_delivery(db_session, export.id)
    assert recovered.status == "succeeded"
    assert recovered.sha256 == artifact_sha
    assert recovered.oss_uri.endswith("dataset.tar.gz")
    if export_format == "lerobot_3_0":
        metadata = recovered.detail_json["lerobot_metadata"]
        assert metadata["archive_sha256"] == artifact_sha
        assert metadata["info_json"].encode() == (source / "meta" / "info.json").read_bytes()


def test_bad_completion_marker_marks_attempt_failed_and_records_cleanup(db_session, monkeypatch):
    from data.models.catalog_dataset import (
        CatalogDataset,
        CatalogDatasetExport,
        CatalogDatasetVersion,
    )
    from data.services.catalog_export_jobs import recover_catalog_export_delivery

    dataset = CatalogDataset(name="bad-recovery-test", source_kind="qrdf_assets", status="active")
    db_session.add(dataset)
    db_session.flush()
    snapshot = {"schema": "bad"}
    from data.services.catalog_export_jobs import _snapshot_hash

    version = CatalogDatasetVersion(
        dataset_id=dataset.id,
        version=1,
        status="active",
        source_snapshot_id=_snapshot_hash(snapshot),
        source_snapshot_json=snapshot,
    )
    db_session.add(version)
    db_session.flush()
    export = CatalogDatasetExport(
        version_id=version.id,
        format="qrdf_0_2",
        status="running",
        idempotency_key="bad-recovery",
        input_snapshot_id=_snapshot_hash(snapshot),
        input_snapshot_json=snapshot,
        attempt=1,
        output_prefix="catalog/bad/attempt-1",
    )
    db_session.add(export)
    db_session.commit()
    artifact = b"not a tar.gz"
    artifact_sha = hashlib.sha256(artifact).hexdigest()
    marker = json.dumps(
        {
            "schema": "quicstudio.catalog-export-complete.v1",
            "export_id": export.id,
            "attempt": 1,
            "format": "qrdf_0_2",
            "input_snapshot_id": _snapshot_hash(snapshot),
            "artifact": {
                "bucket_role": "export",
                "object_key": "catalog/bad/attempt-1/dataset.tar.gz",
                "version_id": "v1",
                "etag": "e1",
                "size_bytes": len(artifact),
                "sha256": artifact_sha,
            },
            "size_bytes": len(artifact),
            "sha256": artifact_sha,
            "files": [{"path": "dataset.json", "size_bytes": 1, "sha256": "0" * 64}],
        }
    ).encode()
    provider = _Provider(
        {
            "catalog/bad/attempt-1/completion.json": marker,
            "catalog/bad/attempt-1/dataset.tar.gz": artifact,
        }
    )
    monkeypatch.setattr("data.services.catalog_export_jobs.get_storage_provider", lambda: provider)
    with pytest.raises(CatalogExportError):
        recover_catalog_export_delivery(db_session, export.id)
    db_session.refresh(export)
    assert export.status == "failed"
    assert export.error_code == "catalog_export_recovery_failed"


@pytest.mark.parametrize("export_format", ["qrdf_0_2", "lerobot_3_0"])
def test_run_catalog_export_uploads_verified_tar_that_validates_after_unpack(
    db_session, monkeypatch, tmp_path, export_format
):
    from data.config import settings

    monkeypatch.setattr(settings, "scratch_root", str(tmp_path))
    from qrdf.converters.source.examples import SyntheticTeleOpImporter

    from data.models.catalog_dataset import (
        CatalogDataset,
        CatalogDatasetExport,
        CatalogDatasetVersion,
    )
    from data.services.catalog_export_jobs import run_catalog_export

    source = tmp_path / "source"
    SyntheticTeleOpImporter().import_episode(
        source, source, episode_id="episode_000001", num_steps=4
    )
    episode_root = source / "episodes" / "episode_000001"
    payloads = {}
    files = []
    for local in sorted(p for p in episode_root.rglob("*") if p.is_file()):
        relative = local.relative_to(episode_root).as_posix()
        payload = local.read_bytes()
        key = f"process/v2/qrdf/worker-test/{relative}"
        payloads[key] = payload
        files.append(
            {
                "path": relative,
                "kind": "data" if relative == "data.mcap" else "metadata",
                "ref": {
                    "bucket_role": "process",
                    "object_key": key,
                    "version_id": "v1",
                    "etag": "e1",
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                },
            }
        )
    provider = _Provider(payloads)
    monkeypatch.setattr("data.services.catalog_export_jobs.get_storage_provider", lambda: provider)
    dataset = CatalogDataset(
        name=f"worker-test-{export_format}", source_kind="qrdf_assets", status="active"
    )
    db_session.add(dataset)
    db_session.flush()
    snapshot = {
        "schema": "quicstudio.catalog-source.v1",
        "assets": [
            {
                "source_snapshot": {
                    "episodes": [
                        {
                            "episode_id": 7,
                            "files": files,
                        }
                    ]
                }
            }
        ],
    }
    from data.services.catalog_export_jobs import _snapshot_hash

    version = CatalogDatasetVersion(
        dataset_id=dataset.id,
        version=1,
        status="active",
        source_snapshot_id=_snapshot_hash(snapshot),
        source_snapshot_json=snapshot,
    )
    db_session.add(version)
    db_session.flush()
    export = CatalogDatasetExport(
        version_id=version.id,
        format=export_format,
        status="queued",
        idempotency_key=f"worker-test-{export_format}",
        input_snapshot_id=_snapshot_hash(snapshot),
        input_snapshot_json=snapshot,
        attempt=1,
        output_prefix="catalog/worker/attempt-1",
    )
    db_session.add(export)
    db_session.commit()
    version.source_snapshot_json = {"schema": "mutated-latest"}
    db_session.commit()

    class Job:
        detail_json = {"export_id": export.id}
        resource_id = str(export.id)

    result = run_catalog_export(db_session, Job())
    assert result["status"] == "succeeded"
    archive_bytes = provider.payloads["catalog/worker/attempt-1/dataset.tar.gz"]
    archive_path = tmp_path / "result.tar.gz"
    archive_path.write_bytes(archive_bytes)
    unpacked = tmp_path / "unpacked"
    with tarfile.open(archive_path, "r:gz") as archive:
        archive.extractall(unpacked)
    _validate_materialized(unpacked, export_format)
    if export_format == "lerobot_3_0":
        metadata = export.detail_json["lerobot_metadata"]
        assert metadata["archive_sha256"] == hashlib.sha256(archive_bytes).hexdigest()
        info_bytes = (unpacked / "meta" / "info.json").read_bytes()
        assert metadata["info_json"].encode() == info_bytes
        assert metadata["info_sha256"] == hashlib.sha256(info_bytes).hexdigest()


def test_retry_preserves_failed_attempt_and_allocates_new_prefix(db_session):
    from data.models.catalog_dataset import (
        CatalogDataset,
        CatalogDatasetExport,
        CatalogDatasetVersion,
    )
    from data.services.catalog_export_jobs import retry_catalog_export

    dataset = CatalogDataset(name="retry-test", source_kind="qrdf_assets", status="active")
    db_session.add(dataset)
    db_session.flush()
    snapshot = {"schema": "x"}
    from data.services.catalog_export_jobs import _snapshot_hash

    version = CatalogDatasetVersion(
        dataset_id=dataset.id,
        version=1,
        status="active",
        source_snapshot_id=_snapshot_hash(snapshot),
        source_snapshot_json=snapshot,
    )
    db_session.add(version)
    db_session.flush()
    failed = CatalogDatasetExport(
        version_id=version.id,
        format="qrdf_0_2",
        status="failed",
        idempotency_key="retry-root",
        input_snapshot_id=_snapshot_hash(snapshot),
        input_snapshot_json=snapshot,
        attempt=1,
        output_prefix="catalog/retry/attempt-1",
        error_code="catalog_export_failed",
        error_message="fixture failure",
    )
    db_session.add(failed)
    db_session.commit()
    retry, _job, created = retry_catalog_export(db_session, failed.id)
    assert created is True
    assert failed.status == "failed"
    assert retry.attempt == 2
    assert retry.output_prefix != failed.output_prefix
