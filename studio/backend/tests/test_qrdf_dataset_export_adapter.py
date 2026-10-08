import json
import zipfile
from contextlib import contextmanager

import pytest

from data.utils.checksums import tree_sha256


def _manifest(package_uri: str, checksum: str, *, item_kind: str = "episode") -> dict[str, object]:
    return {
        "revision_id": 4,
        "dataset_id": 2,
        "workspace_id": 1,
        "name": "box-closing",
        "version": 3,
        "items": [
            {
                "position": 0,
                "split": "train",
                "item_kind": item_kind,
                "sample_id": "episode:8",
                "published_episode_id": 8,
                "episode_id": 11,
                "source_episode_id": "source-episode-11",
                "source_fingerprint": "a" * 64,
                "package_uri": package_uri,
                "package_checksum": checksum,
                "core_start_ns": None,
                "core_end_ns": None,
                "effective_start_ns": None,
                "effective_end_ns": None,
                "task": "close the box",
                "outcome": "success",
            }
        ],
    }


def test_qrdf_export_builds_a_canonical_dataset_without_source_locations(tmp_storage, monkeypatch):
    from qrdf.converters.source.examples import SyntheticTeleOpImporter
    from qrdf.validator.validator import QRDFValidator

    from data.integrations.qrdf import dataset_qrdf_export as adapter

    package = tmp_storage / "official"
    SyntheticTeleOpImporter().import_episode(
        package,
        package,
        episode_id="episode_000001",
        num_steps=5,
    )
    uri = "nas://official/private-package"
    manifest = _manifest(uri, tree_sha256(package))

    @contextmanager
    def fake_materialize(requested_uri):
        assert requested_uri == uri
        yield package

    monkeypatch.setattr(adapter, "materialize_for_processing", fake_materialize)

    result = adapter.export_qrdf_revision(manifest, tmp_storage / "out")

    assert result.item_count == 1
    assert result.archive_path.is_file()
    extracted = tmp_storage / "extracted"
    with zipfile.ZipFile(result.archive_path) as archive:
        archive.extractall(extracted)
        assert "episodes/episode_000001/data.mcap" in archive.namelist()
        sidecar = json.loads(archive.read("quicdata_manifest.json"))
        assert uri not in json.dumps(sidecar)
    report = QRDFValidator().validate_canonical_dataset(extracted)
    assert report.error_count == 0, report.summary()
    exported_manifest = json.loads((extracted / "dataset.json").read_text(encoding="utf-8"))
    assert exported_manifest["splits"] == {
        "train": ["episode_000001"],
        "val": [],
        "test": [],
    }


def test_qrdf_export_accepts_an_official_single_episode_package(tmp_storage, monkeypatch):
    from qrdf.converters.source.examples import SyntheticTeleOpImporter
    from qrdf.validator.validator import QRDFValidator

    from data.integrations.qrdf import dataset_qrdf_export as adapter

    dataset = tmp_storage / "source-dataset"
    SyntheticTeleOpImporter().import_episode(
        dataset,
        dataset,
        episode_id="episode_000001",
        num_steps=3,
    )
    package = dataset / "episodes" / "episode_000001"
    manifest = _manifest("nas://official/single-episode", tree_sha256(package))

    @contextmanager
    def fake_materialize(_uri):
        yield package

    monkeypatch.setattr(adapter, "materialize_for_processing", fake_materialize)

    result = adapter.export_qrdf_revision(manifest, tmp_storage / "single-out")
    extracted = tmp_storage / "single-extracted"
    with zipfile.ZipFile(result.archive_path) as archive:
        archive.extractall(extracted)

    report = QRDFValidator().validate_canonical_dataset(extracted)
    assert report.error_count == 0, report.summary()
    assert (extracted / "episodes" / "episode_000001" / "data.mcap").is_file()


def test_qrdf_export_rejects_time_ranged_samples(tmp_path):
    from data.integrations.qrdf.dataset_qrdf_export import export_qrdf_revision

    manifest = _manifest("nas://official/package", "b" * 64, item_kind="sample")

    with pytest.raises(ValueError, match="full published Episodes"):
        export_qrdf_revision(manifest, tmp_path / "out")


def test_qrdf_export_rejects_symlinks_in_an_official_package(tmp_path, monkeypatch):
    from data.integrations.qrdf import dataset_qrdf_export as adapter

    package = tmp_path / "official"
    package.mkdir()
    outside = tmp_path / "outside.mcap"
    outside.write_bytes(b"private")
    (package / "data.mcap").symlink_to(outside)
    manifest = _manifest("nas://official/package", "b" * 64)

    @contextmanager
    def fake_materialize(_uri):
        yield package

    monkeypatch.setattr(adapter, "materialize_for_processing", fake_materialize)

    with pytest.raises(ValueError, match="symlink"):
        adapter.export_qrdf_revision(manifest, tmp_path / "out")
