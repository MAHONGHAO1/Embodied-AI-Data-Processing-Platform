"""Real QRDF validator/preview admission smoke tests."""

from __future__ import annotations

import json
import tarfile
from io import BytesIO
from pathlib import Path

import pytest
from qrdf.converters.source.examples import SyntheticTeleOpImporter

from data.infra.object_storage import StorageObjectRef
from data.integrations.qrdf.admission import (
    _safe_extract_tar,
    admit_qrdf_episode,
)


class _ProcessCapture:
    """Captures each individually uploaded process object by its key."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.uploads: dict[str, bytes] = {}
        self.fail_after: int | None = None

    def put_worker_object(self, ref: StorageObjectRef, source_path: str) -> StorageObjectRef:
        if self.fail_after is not None and len(self.uploads) >= self.fail_after:
            raise RuntimeError("simulated process upload failure")
        payload = Path(source_path).read_bytes()
        self.uploads[ref.object_key] = payload
        return StorageObjectRef(
            "process",
            ref.object_key,
            "version-1",
            "etag-1",
            len(payload),
            "server-sha256",
        )


def _episode(tmp_path: Path) -> Path:
    dataset = tmp_path / "dataset"
    SyntheticTeleOpImporter().import_episode(
        dataset,
        dataset,
        episode_id="episode_000001",
        num_steps=8,
    )
    return dataset / "episodes" / "episode_000001"


def test_real_qrdf_rgb_episode_generates_verified_process_preview(
    tmp_path: Path,
) -> None:
    episode = _episode(tmp_path)
    provider = _ProcessCapture(tmp_path)

    result = admit_qrdf_episode(episode, process_provider=provider)

    assert result.ok is True
    assert result.integrity_status == "passed"
    assert result.preview_status == "ready"
    assert result.output_verification_status == "verified"
    assert provider.uploads
    kinds = {item["kind"] for item in result.objects}
    assert {
        "metadata",
        "admission_report",
        "preview_manifest",
        "preview_video",
        "preview_timeline",
    } <= kinds
    report_entry = next(item for item in result.objects if item["kind"] == "admission_report")
    stored_report = json.loads(provider.uploads[report_entry["ref"]["object_key"]])
    assert stored_report == result.report
    assert stored_report["qrdf_runtime"]["source_commit"]
    metadata_entry = next(item for item in result.objects if item["kind"] == "metadata")
    assert json.loads(provider.uploads[metadata_entry["ref"]["object_key"]])
    # No process tar is produced any more, and the local report scratch file
    # is removed once every object has been published.
    assert not (episode / ".qrdf-admission-report.json").exists()
    assert not list(episode.parent.glob("*.tar"))


def test_corrupt_mcap_isolated_as_structured_failure(tmp_path: Path) -> None:
    episode = _episode(tmp_path)
    (episode / "data.mcap").write_bytes(b"truncated mcap")

    result = admit_qrdf_episode(episode)

    assert result.ok is False
    assert result.integrity_status == "failed"
    assert any(issue["code"] == "MCAP_UNREADABLE" for issue in result.issues)
    assert result.objects == ()


def test_partial_process_upload_failure_still_cleans_report_file(tmp_path: Path) -> None:
    """Task 8 addendum: _publish_process_objects runs inside the report_path

    try/finally, so a mid-upload failure must not leak the local scratch
    report file even though the caller re-raises.
    """
    episode = _episode(tmp_path)
    provider = _ProcessCapture(tmp_path)
    provider.fail_after = 0

    with pytest.raises(RuntimeError, match="simulated process upload failure"):
        admit_qrdf_episode(episode, process_provider=provider)

    assert not provider.uploads
    assert not (episode / ".qrdf-admission-report.json").exists()


def test_worker_archive_rejects_traversal_and_links(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.tar"
    with tarfile.open(archive, "w") as output:
        info = tarfile.TarInfo("../escape.txt")
        payload = b"escape"
        info.size = len(payload)
        output.addfile(info, BytesIO(payload))

    try:
        _safe_extract_tar(archive, tmp_path / "scratch")
    except ValueError as exc:
        assert "escapes" in str(exc)
    else:  # pragma: no cover - the assertion documents the security contract
        raise AssertionError("path traversal archive was extracted")


@pytest.mark.parametrize(
    "mutation",
    ["duplicate_drop", "frame_gap", "bad_pts", "source_count", "first_timestamp"],
)
def test_preview_timeline_contract(tmp_path, mutation):
    from qrdf.reader.episode import Episode

    from data.integrations.qrdf.admission import _preview_artifacts_valid

    root = _episode(tmp_path)
    episode = Episode(root)
    episode.generate_rgb_previews()
    manifest_path = root / "media/preview/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    stream = manifest["streams"][0]
    timeline_path = manifest_path.parent / stream["timeline_path"]
    timeline = json.loads(timeline_path.read_text())
    if mutation == "duplicate_drop":
        timeline["entries"].insert(
            1,
            {
                "kind": "dropped",
                "timestamp_ns": timeline["entries"][0]["timestamp_ns"],
                "reason": "duplicate_timestamp",
            },
        )
        stream["source_frame_count"] += 1
        stream["dropped_frame_count"] += 1
        stream["status"] = "partial"
        stream["failure_reasons"] = ["duplicate_timestamp"]
    elif mutation == "frame_gap":
        timeline["entries"][-1]["frame_index"] += 1
    elif mutation == "bad_pts":
        timeline["entries"][1]["video_pts_us"] = timeline["entries"][0]["video_pts_us"]
    elif mutation == "source_count":
        stream["source_frame_count"] += 1
    else:
        stream["first_timestamp_ns"] = "1"
    timeline_path.write_text(json.dumps(timeline))
    manifest_path.write_text(json.dumps(manifest))
    valid, _ = _preview_artifacts_valid(root, Episode(root))
    assert valid is (mutation == "duplicate_drop")


def test_ego_alias_human_demonstration_is_admitted_without_rewriting_metadata(
    tmp_path: Path,
) -> None:
    """The capture app's historical ``human_demonstration`` type must take the EGO branch.

    QRDF 0.2.1 routes this alias to ``human_ego`` internally; Studio must not rewrite
    the episode's ``metadata.json`` to reflect that routing.
    """
    from qrdf.converters.source.examples import SyntheticEgoImporter

    dataset = tmp_path / "ego-dataset"
    SyntheticEgoImporter().import_episode(dataset, dataset, episode_id="episode_ego_001")
    episode = dataset / "episodes" / "episode_ego_001"
    metadata_path = episode / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.setdefault("capture", {})["episode_type"] = "human_demonstration"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    before = metadata_path.read_bytes()

    result = admit_qrdf_episode(episode)

    assert result.ok is True
    assert result.integrity_status == "passed"
    assert result.preview_status == "ready"
    assert {issue["code"] for issue in result.issues if issue["severity"] == "ERROR"} <= {
        "NO_VALID_EGO_FRAMES"
    }
    assert metadata_path.read_bytes() == before


def test_training_readiness_issue_codes_are_deferred_to_batch_qc() -> None:
    from data.integrations.qrdf.admission import TRAINING_READINESS_ISSUE_CODES

    assert {
        "NO_STATE_TOPIC",
        "NO_ACTION_TOPIC",
        "NO_VALID_TRAINING_FRAMES",
        "NO_VALID_EGO_FRAMES",
    } <= TRAINING_READINESS_ISSUE_CODES


def test_ego_alias_is_admitted_without_rewriting_metadata(tmp_path):
    # A genuine EGO dataset labeled with the `human_demonstration` alias is admitted
    # without Studio rewriting its metadata.json.
    from qrdf.converters.source.examples import SyntheticEgoImporter

    dataset = tmp_path / "ego-dataset"
    SyntheticEgoImporter().import_episode(dataset, dataset, episode_id="episode_ego_002")
    root = dataset / "episodes" / "episode_ego_002"
    metadata_path = root / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.setdefault("capture", {})["episode_type"] = "human_demonstration"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    before = metadata_path.read_bytes()

    result = admit_qrdf_episode(root, generate_preview=True)

    assert metadata_path.read_bytes() == before
    assert result.integrity_status == "passed"
    codes = {issue["code"] for issue in result.issues}
    assert "NO_ACTION_TOPIC" not in codes and "NO_STATE_TOPIC" not in codes


def test_report_extra_is_written_into_the_published_report(tmp_path):
    episode = _episode(tmp_path)
    provider = _ProcessCapture(tmp_path)

    result = admit_qrdf_episode(
        episode,
        process_provider=provider,
        report_extra={
            "integrity_source": "server",
            "client_admission_fallback": "client_preview_invalid",
        },
    )

    report_entry = next(item for item in result.objects if item["kind"] == "admission_report")
    stored = json.loads(provider.uploads[report_entry["ref"]["object_key"]])
    assert stored["client_admission_fallback"] == "client_preview_invalid"
    assert result.report["integrity_source"] == "server"
