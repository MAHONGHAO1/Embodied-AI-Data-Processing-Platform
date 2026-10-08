"""Real QRDF/LeRobot export of reviewed ranges, including gaps and invalid Episodes."""

from __future__ import annotations

import hashlib
import json
import tarfile
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from tests.test_catalog_export_jobs import _Provider
from tests.test_package_annotation_workbench_api import (
    draft,
    request,
    review_body,
    save,
    submit,
)
from tests.test_package_annotation_workbench_api import (
    package_work as _package_work_fixture,
)

from data.integrations.qrdf.annotated_segment_export import output_intervals
from data.models.episode_admission import EpisodeAdmissionFact
from data.services.catalog_datasets import (
    create_catalog_dataset,
    create_catalog_version,
    export_catalog_version,
)
from data.services.catalog_export_jobs import run_catalog_export
from data.services.data_assets import publish_data_asset

package_work = _package_work_fixture


def _source_archive(tmp_path, capture_kind):
    from qrdf.converters.source.examples import SyntheticEgoImporter, SyntheticTeleOpImporter
    from qrdf.models.episode import EpisodeMetadata
    from qrdf.reader.episode import Episode
    from qrdf.registry.topics import list_rgb_topics

    source = tmp_path / "source"
    importer = SyntheticTeleOpImporter() if capture_kind == "teleop" else SyntheticEgoImporter()
    importer.import_episode(source, source, episode_id="episode_000001", num_steps=30)
    path = source / "episodes" / "episode_000001"
    episode = Episode(path)
    rgb_topic = list_rgb_topics(episode.list_topics())[0]
    stamps = [item.log_time for item in episode.iter_topic(rgb_topic)]
    metadata = EpisodeMetadata.load(path / "metadata.json")
    metadata.timing.start_timestamp_ns = stamps[0]
    metadata.timing.end_timestamp_ns = stamps[-1] + 100_000_000
    metadata.timing.duration_s = 3.0
    metadata.save(path / "metadata.json")
    # A full-source preview sits in the source working directory next to the
    # selected process objects, but it must never leak into a selected-range
    # training delivery.
    preview = path / "media" / "preview" / "full-source.mp4"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"unselected-full-source-preview")
    return path, metadata, stamps, rgb_topic


def _ref(role, key, payload, *, version_id="v1"):
    return {
        "bucket_role": role,
        "object_key": key,
        "version_id": version_id,
        "etag": "e1",
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _publish_reviewed_source(
    work, tmp_path, monkeypatch, *, capture_kind="teleop", version_id="v1"
):
    source, metadata, stamps, rgb_topic = _source_archive(tmp_path, capture_kind)
    db = work["db"]
    episode = work["episodes"][0]
    fact = db.query(EpisodeAdmissionFact).filter_by(episode_id=episode.id, attempt=1).one()
    episode.metadata_json = {
        "collection_upload": {"external_episode_id": metadata.episode_id},
        "timing": {
            "start_timestamp_ns": str(metadata.timing.start_timestamp_ns),
            "end_timestamp_ns": str(metadata.timing.end_timestamp_ns),
            "duration_s": 3.0,
        },
    }
    # The exact source identity is taken from persisted admission evidence.
    raw_bytes = (source / metadata.data_file).read_bytes()
    episode.source_fingerprint = hashlib.sha256(raw_bytes + b"metadata").hexdigest()
    fact.source_fingerprint = episode.source_fingerprint
    # Admission normally archives this report next to the source metadata.
    report = source / "admission-report.json"
    report.write_text('{"ok":true}')
    timeline = json.dumps(
        {
            "topic": rgb_topic,
            "entries": [
                {
                    "kind": "frame",
                    "frame_index": index,
                    "timestamp_ns": str(stamp),
                    "video_pts_us": str(index * 100_000),
                }
                for index, stamp in enumerate(stamps)
            ],
        }
    ).encode()
    # source_binding()/_preview_descriptor() now read fact.objects_json
    # exclusively, so the manifest's "data"/"preview_video"/"preview_timeline"
    # entries must describe this same real synthetic source and preview
    # (topic, refs, sha256) rather than the generic package_work fixture
    # manifest they started as.
    from data.services.episode_objects import object_entry

    metadata_bytes = (source / "metadata.json").read_bytes()
    report_bytes = report.read_bytes()
    manifest_bytes = json.dumps({"topics": [rgb_topic]}).encode()
    objects = [dict(entry) for entry in (fact.objects_json or [])]
    for entry in objects:
        if entry.get("kind") == "data":
            entry.update(
                object_entry(
                    path=entry["path"],
                    kind="data",
                    ref=_ref("raw", "source/data.mcap", raw_bytes, version_id=version_id),
                )
            )
        elif entry.get("kind") == "metadata":
            entry.update(
                object_entry(
                    path=entry["path"],
                    kind="metadata",
                    ref=_ref(
                        "process", "source/metadata.json", metadata_bytes, version_id=version_id
                    ),
                )
            )
        elif entry.get("kind") == "admission_report":
            entry.update(
                object_entry(
                    path=entry["path"],
                    kind="admission_report",
                    ref=_ref(
                        "process",
                        "source/admission-report.json",
                        report_bytes,
                        version_id=version_id,
                    ),
                )
            )
        elif entry.get("kind") == "preview_manifest":
            entry.update(
                object_entry(
                    path=entry["path"],
                    kind="preview_manifest",
                    ref=_ref(
                        "process", "source/manifest.json", manifest_bytes, version_id=version_id
                    ),
                )
            )
        elif entry.get("kind") == "preview_video":
            entry.update(
                object_entry(
                    path=entry["path"],
                    kind="preview_video",
                    topic=rgb_topic,
                    ref=_ref("process", "source/video.mp4", b"preview", version_id=version_id),
                )
            )
        elif entry.get("kind") == "preview_timeline":
            entry.update(
                object_entry(
                    path=entry["path"],
                    kind="preview_timeline",
                    topic=rgb_topic,
                    ref=_ref("process", "source/timeline.json", timeline, version_id=version_id),
                )
            )
    fact.objects_json = objects
    provider = _Provider(
        {
            **work["store"].objects,
            "source/timeline.json": timeline,
            "source/data.mcap": raw_bytes,
            "source/metadata.json": metadata_bytes,
            "source/admission-report.json": report_bytes,
            "source/manifest.json": manifest_bytes,
            "source/video.mp4": b"preview",
        },
        version_id=version_id,
    )
    monkeypatch.setattr(
        "data.services.package_annotation_workbench.get_storage_provider", lambda: provider
    )
    monkeypatch.setattr("data.services.catalog_export_jobs.get_storage_provider", lambda: provider)
    db.commit()
    entry = draft(work, all_invalid=True)
    segments = [
        {
            "id": "first",
            "start_ns": str(stamps[5]),
            "end_ns": str(stamps[10]),
            "description": "Lift the blue box",
        },
        {
            "id": "second",
            "start_ns": str(stamps[20]),
            "end_ns": str(stamps[25]),
            "description": "Put the box on the shelf",
        },
    ]
    entry["episodes"][str(episode.id)] = {"conclusion": "segments", "segments": segments}
    save(work, entry)
    submitted = submit(work)
    assert submitted.status_code == 200, submitted.text
    approved = request(work, "post", "/approve", review_body(work), review=True)
    assert approved.status_code == 200, approved.text
    asset = publish_data_asset(db, batch_id=work["batch"].id)
    db.commit()
    return asset, provider, segments, source, raw_bytes


@pytest.mark.parametrize("export_format", ["qrdf_0_2", "lerobot_3_0"])
@pytest.mark.parametrize("capture_kind", ["teleop", "ego"])
@pytest.mark.parametrize("version_id", ["v1", None], ids=["versioned", "etag-only"])
def test_real_reviewed_ranges_export_without_gap_or_invalid_episode(
    package_work, tmp_path, monkeypatch, export_format, capture_kind, version_id
):
    from data.config import settings
    from data.database import JobRun
    from data.services.catalog_export_jobs import _validate_materialized

    monkeypatch.setattr(settings, "scratch_root", str(tmp_path))
    work = package_work
    db = work["db"]
    asset, provider, ranges, source, original_raw = _publish_reviewed_source(
        work, tmp_path, monkeypatch, capture_kind=capture_kind, version_id=version_id
    )
    dataset = create_catalog_dataset(db, name=f"Segments-{uuid4().hex}", source_kind="qrdf_assets")
    version = create_catalog_version(db, dataset_id=dataset.id, data_asset_ids=[asset.id])
    export = export_catalog_version(db, version_id=version.id, export_format=export_format)
    db.commit()
    job = (
        db.query(JobRun)
        .filter_by(kind="catalog_export", resource_type="artifact", resource_id=str(export.id))
        .one()
    )
    result = run_catalog_export(db, job)
    db.commit()
    assert result["status"] == "succeeded"
    assert export.manifest_json["artifact"]["version_id"] == version_id
    delivered = tmp_path / "delivered"
    delivered.mkdir()
    archive = tmp_path / "delivered.tar.gz"
    archive.write_bytes(provider.payloads[export.manifest_json["artifact"]["object_key"]])
    with tarfile.open(archive, "r:gz") as handle:
        names = handle.getnames()
        assert not any(
            "full-source.mp4" in name or "annotation-source-" in name or "process.tar" in name
            for name in names
        )
        handle.extractall(delivered, filter="data")
    _validate_materialized(delivered, export_format)
    manifest = json.loads((delivered / "quicdata_manifest.json").read_text())
    assert len(manifest["episodes"]) == 2
    assert [row["description"] for row in manifest["episodes"]] == [
        row["description"] for row in ranges
    ]
    assert {row["annotation_submission_id"] for row in manifest["episodes"]} == {
        work["item"].current_submission_id
    }
    assert {row["source_episode_id"] for row in manifest["episodes"]} == {work["episodes"][0].id}
    assert (source / "data.mcap").read_bytes() == original_raw
    if export_format == "qrdf_0_2":
        from qrdf.reader.reader import QRDFReader
        from qrdf.registry.topics import list_rgb_topics

        reader = QRDFReader(delivered)
        assert len(reader.list_episodes()) == 2
        for output_id, selected in zip(reader.list_episodes(), ranges, strict=True):
            episode = reader.load_episode(output_id)
            rgb_topic = list_rgb_topics(episode.list_topics())[0]
            times = [item.log_time for item in episode.iter_topic(rgb_topic)]
            assert len(times) == 5
            assert all(
                int(selected["start_ns"]) <= stamp < int(selected["end_ns"]) for stamp in times
            )
            annotation = json.loads((episode.path / "annotation.json").read_text())
            items = annotation["tracks"][0]["items"]
            assert len(items) == 1
            assert items[0]["high_level_subtask"] == selected["description"]
            assert items[0]["target"]["start_ns"] == selected["start_ns"]
            assert items[0]["target"]["end_ns"] == selected["end_ns"]
    else:
        import pyarrow.parquet as pq

        info = json.loads((delivered / "meta" / "info.json").read_text())
        assert info["total_episodes"] == 2
        assert info["total_frames"] == 10
        tasks = pq.read_table(delivered / "meta" / "tasks.parquet").to_pydict()
        assert set(tasks["task"]) == {row["description"] for row in ranges}
        samples = [
            row
            for file in sorted((delivered / "data").rglob("*.parquet"))
            for row in pq.read_table(file).to_pylist()
        ]
        # Synthetic TeleOp position encodes the original frame index. No frame
        # from [10,20) or the unselected ends may reappear after conversion.
        if capture_kind == "teleop":
            indices = [round((row["observation.state"][0] - 0.1) / 0.01) for row in samples]
            assert indices == [*range(5, 10), *range(20, 25)]
        else:
            assert info["export_mode"] == "ego_rgb"
            assert "observation.images.head" in info["features"]


@pytest.mark.parametrize("broken_contract", ["range", "historical"])
def test_catalog_version_rejects_tampered_approved_ranges(
    package_work, tmp_path, monkeypatch, broken_contract
):
    from data.services.catalog_datasets import CatalogDatasetError
    from data.services.data_assets import _snapshot_id

    asset, _provider, _ranges, _source, _raw = _publish_reviewed_source(
        package_work, tmp_path, monkeypatch
    )
    db = package_work["db"]
    dataset = create_catalog_dataset(db, name=f"Frozen-{uuid4().hex}")
    version = create_catalog_version(db, dataset_id=dataset.id, data_asset_ids=[asset.id])
    db.commit()
    original = deepcopy(version.source_snapshot_json)
    changed = deepcopy(asset.source_snapshot_json)
    if broken_contract == "range":
        changed["episodes"][0]["effective_segments"][0]["end_ns"] = changed["episodes"][0][
            "source_end_ns"
        ]
    else:
        changed.pop("annotation_enabled")
    asset.source_snapshot_json = changed
    asset.source_snapshot_id = _snapshot_id(changed)
    db.commit()
    with pytest.raises(CatalogDatasetError, match="approved annotation"):
        create_catalog_version(db, dataset_id=dataset.id, data_asset_ids=[asset.id])
    db.rollback()
    db.refresh(version)
    assert version.source_snapshot_json == original


@pytest.mark.parametrize("version_id", ["v1", None], ids=["versioned", "etag-only"])
def test_version_detail_restores_real_exports_and_download_signs_verified_artifact(
    package_work, tmp_path, monkeypatch, version_id
):
    from tests.test_annotation_work_items_api import _headers_for

    from data.config import settings
    from data.database import JobRun

    monkeypatch.setattr(settings, "scratch_root", str(tmp_path))
    work, db = package_work, package_work["db"]
    asset, provider, _ranges, _source, _raw = _publish_reviewed_source(
        work, tmp_path, monkeypatch, version_id=version_id
    )
    dataset = create_catalog_dataset(db, name=f"Download-{uuid4().hex}")
    version = create_catalog_version(db, dataset_id=dataset.id, data_asset_ids=[asset.id])
    completed = export_catalog_version(db, version_id=version.id, export_format="qrdf_0_2")
    queued = export_catalog_version(db, version_id=version.id, export_format="lerobot_3_0")
    db.commit()
    job = db.query(JobRun).filter_by(kind="catalog_export", resource_id=str(completed.id)).one()
    assert run_catalog_export(db, job)["status"] == "succeeded"
    db.commit()
    client, headers = work["client"], _headers_for(work["annotator"])
    detail_url = f"/api/v1/catalog-datasets/versions/{version.id}"
    assert client.get(detail_url).status_code == 401
    detail = client.get(detail_url, headers=headers)
    assert detail.status_code == 200, detail.text
    data = detail.json()["data"]
    assert data["source_snapshot_id"] == version.source_snapshot_id
    assert [(row["id"], row["status"]) for row in data["exports"]] == [
        (queued.id, "queued"),
        (completed.id, "succeeded"),
    ]
    assert all("url" not in row for row in data["exports"])

    signed, networks = [], []

    def factory(config=None):
        networks.append(config.storage_browser_endpoint)
        return provider

    def sign(ref, *, expires):
        signed.append((ref, expires))
        return f"https://downloads.invalid/{ref.object_key}?ttl={expires}"

    monkeypatch.setattr(provider, "sign_get", sign, raising=False)
    monkeypatch.setattr("data.services.catalog_export_jobs.get_storage_provider", factory)
    monkeypatch.setattr(settings, "storage_endpoint", "https://oss.internal.invalid")
    monkeypatch.setattr(settings, "storage_browser_endpoint", "https://oss.public.invalid")
    download_url = f"/api/v1/catalog-datasets/exports/{completed.id}/download"
    assert client.get(download_url).status_code == 401
    pending = client.get(f"/api/v1/catalog-datasets/exports/{queued.id}/download", headers=headers)
    assert pending.status_code == 409 and signed == [] and networks == []

    before = datetime.now(timezone.utc)
    download = client.get(download_url, headers=headers)
    assert download.status_code == 200, download.text
    result = download.json()["data"]
    assert result["oss_network"] == "internal" and result["url_ttl_seconds"] == 3600
    assert networks == ["https://oss.internal.invalid"]
    assert signed[0][0].version_id == completed.manifest_json["artifact"]["version_id"]
    assert signed[0][1] == 3600
    assert 3599 <= (datetime.fromisoformat(result["expires_at"]) - before).total_seconds() <= 3602
    assert result["size_bytes"] == completed.size_bytes and result["sha256"] == completed.sha256
    assert provider.payloads[signed[0][0].object_key]
    public = client.get(
        download_url, headers=headers, params={"oss_network": "public", "url_ttl_seconds": 120}
    )
    assert public.status_code == 200 and signed[-1][1] == 120
    assert networks[-1] == "https://oss.public.invalid"

    for params in [
        {"url_ttl_seconds": 0},
        {"url_ttl_seconds": 604801},
        {"oss_network": "external"},
    ]:
        assert client.get(download_url, headers=headers, params=params).status_code == 422
    monkeypatch.setattr(settings, "storage_browser_endpoint", "")
    monkeypatch.setattr(settings, "oss_browser_endpoint", "")
    assert (
        client.get(download_url, headers=headers, params={"oss_network": "public"}).status_code
        == 503
    )
    assert len(signed) == 2
    head = provider.head
    monkeypatch.setattr(provider, "head", lambda ref: replace(head(ref), version_id="drifted"))
    assert client.get(download_url, headers=headers).status_code == 409
    assert len(signed) == 2


def test_long_segment_partition_preserves_exact_union():
    segments = [
        {
            "id": "long",
            "start_ns": "9007199254740993123",
            "end_ns": str(9_007_199_254_740_993_123 + 301_000_000_000),
            "description": "Move the object",
        }
    ]
    intervals = list(output_intervals(segments))
    assert len(intervals) == 2
    assert intervals[0]["end_ns"] == intervals[1]["start_ns"]
    assert intervals[0]["start_ns"] == segments[0]["start_ns"]
    assert intervals[1]["end_ns"] == segments[0]["end_ns"]
    assert all(row["description"] == "Move the object" for row in intervals)
