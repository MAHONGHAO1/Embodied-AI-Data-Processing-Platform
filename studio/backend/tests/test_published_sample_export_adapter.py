import hashlib
import json
from contextlib import contextmanager

from qrdf.converters.published_sample_lerobot import (
    SliceExportItemReport,
    SliceExportOptions,
    SliceExportReport,
)


def _manifest(package_uri: str, checksum: str) -> dict[str, object]:
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
                "item_kind": "sample",
                "sample_id": "sample-1",
                "published_episode_id": 8,
                "episode_id": 11,
                "source_episode_id": "source-episode-11",
                "source_fingerprint": "a" * 64,
                "package_uri": package_uri,
                "package_checksum": checksum,
                "core_start_ns": 100,
                "core_end_ns": 200,
                "effective_start_ns": 90,
                "effective_end_ns": 210,
                "pre_roll_s": 0.0,
                "post_roll_s": 0.0,
                "task": "close the box",
                "outcome": "success",
            }
        ],
    }


def test_export_adapter_materializes_once_and_keeps_paths_out_of_zip(tmp_path, monkeypatch):
    from data.integrations.qrdf import published_sample_export as adapter

    package = tmp_path / "official"
    package.mkdir()
    checksum = "b" * 64
    manifest = _manifest("nas://official/package", checksum)
    materialized = []

    @contextmanager
    def fake_materialize(uri):
        materialized.append(uri)
        yield package

    request = object()
    report = SliceExportReport(
        lerobot_version="v3.0",
        items=(
            SliceExportItemReport(
                sample_id="sample-1",
                episode_index=0,
                split="train",
                data_chunk_index=0,
                data_file_index=0,
                dataset_from_index=0,
                dataset_to_index=1,
                frame_count=1,
                source_start_ns=90,
                source_end_ns=210,
            ),
        ),
        input_manifest_sha256="c" * 64,
        artifact_manifest_sha256="d" * 64,
    )

    monkeypatch.setattr(adapter, "materialize_for_processing", fake_materialize)
    monkeypatch.setattr(adapter, "tree_sha256", lambda _path: checksum)
    monkeypatch.setattr(adapter, "ensure_processable_dataset_path", lambda path, **_kwargs: path)
    monkeypatch.setattr(adapter, "build_slice_requests", lambda _manifest, _roots: (request,))

    def fake_export(requests, output_path, *, options):
        assert requests == (request,)
        assert isinstance(options, SliceExportOptions)
        output_path.mkdir(parents=True)
        (output_path / "meta").mkdir()
        (output_path / "meta" / "info.json").write_text("{}", encoding="utf-8")
        return report

    monkeypatch.setattr(adapter, "export_episode_slices_to_lerobot_v3", fake_export)

    result = adapter.export_published_sample_revision(manifest, tmp_path / "out")

    assert materialized == ["nas://official/package"]
    assert result.archive_path.is_file()
    with result.archive_path.open("rb") as handle:
        assert handle.read(2) == b"PK"
    import zipfile

    with zipfile.ZipFile(result.archive_path) as archive:
        names = set(archive.namelist())
        assert "quicdata_manifest.json" in names
        assert "quicdata_export_report.json" in names
        sidecar = json.loads(archive.read("quicdata_manifest.json"))
        assert "package_uri" not in json.dumps(sidecar)
        assert "nas://official/package" not in json.dumps(sidecar)


def test_export_adapter_rejects_checksum_mismatch(tmp_path, monkeypatch):
    from data.integrations.qrdf import published_sample_export as adapter

    package = tmp_path / "official"
    package.mkdir()
    manifest = _manifest("nas://official/package", "b" * 64)

    @contextmanager
    def fake_materialize(_uri):
        yield package

    monkeypatch.setattr(adapter, "materialize_for_processing", fake_materialize)
    monkeypatch.setattr(adapter, "tree_sha256", lambda _path: "c" * 64)

    try:
        adapter.export_published_sample_revision(manifest, tmp_path / "out")
    except ValueError as exc:
        assert "checksum" in str(exc)
    else:
        raise AssertionError("checksum mismatch must stop export")


def test_export_adapter_accepts_legacy_batch_publication_checksum(tmp_path, monkeypatch):
    from data.integrations.qrdf import published_sample_export as adapter

    package = tmp_path / "official"
    (package / "nested").mkdir(parents=True)
    (package / "dataset.json").write_text("{}", encoding="utf-8")
    (package / "nested" / "data.bin").write_bytes(b"published-data")
    legacy = hashlib.sha256()
    for item in sorted(path for path in package.rglob("*") if path.is_file()):
        legacy.update(item.relative_to(package).as_posix().encode())
        legacy.update(item.read_bytes())
    manifest = _manifest("nas://official/legacy-package", legacy.hexdigest())

    @contextmanager
    def fake_materialize(_uri):
        yield package

    report = SliceExportReport(
        lerobot_version="v3.0",
        items=(),
        input_manifest_sha256="c" * 64,
        artifact_manifest_sha256="d" * 64,
    )
    monkeypatch.setattr(adapter, "materialize_for_processing", fake_materialize)
    monkeypatch.setattr(adapter, "ensure_processable_dataset_path", lambda path, **_kwargs: path)
    monkeypatch.setattr(adapter, "build_slice_requests", lambda _manifest, _roots: ())

    def fake_export(_requests, output_path, *, options):
        assert isinstance(options, SliceExportOptions)
        output_path.mkdir(parents=True)
        return report

    monkeypatch.setattr(adapter, "export_episode_slices_to_lerobot_v3", fake_export)

    result = adapter.export_published_sample_revision(manifest, tmp_path / "out-legacy")

    assert result.archive_path.is_file()


def test_export_adapter_runs_real_qrdf_slice_export(tmp_storage):
    import zipfile

    from qrdf import QRDFReader
    from qrdf.converters.source.examples import SyntheticTeleOpImporter
    from qrdf.converters.validate_lerobot import validate_lerobot_dataset

    from data.integrations.qrdf.published_sample_export import export_published_sample_revision
    from data.utils.checksums import tree_sha256

    package = tmp_storage / "official" / "package"
    SyntheticTeleOpImporter().import_episode(
        package, package, episode_id="episode_000001", num_steps=20
    )
    frames = list(
        QRDFReader(package)
        .load_episode("episode_000001")
        .frames(reference_topic="/observation/eef_state", fps=10.0, as_dict=True)
    )
    manifest = _manifest("nas://storage/official/package", tree_sha256(package))
    manifest["items"][0]["modality"] = "teleop"
    manifest["items"][0]["effective_start_ns"] = frames[0]["timestamp_ns"]
    manifest["items"][0]["effective_end_ns"] = frames[10]["timestamp_ns"] + 1

    result = export_published_sample_revision(
        manifest,
        tmp_storage / "exports" / "adapter-real",
        options=SliceExportOptions(image_size=(32, 24), max_frames_per_shard=4),
    )

    assert len(result.report.items) == 1
    extracted = tmp_storage / "exports" / "adapter-unpacked"
    with zipfile.ZipFile(result.archive_path) as archive:
        archive.extractall(extracted)
    validation = validate_lerobot_dataset(extracted)
    assert validation.ok, validation.summary()
    sidecar = json.loads((extracted / "quicdata_manifest.json").read_text(encoding="utf-8"))
    assert sidecar["items"][0]["sample_id"] == "sample-1"
    assert "package_uri" not in json.dumps(sidecar)
