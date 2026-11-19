"""Adapter Contract v1: detection, explicit rejection, and a toy plugin.

The toy adapter is the point of this file: it proves a new source can be
registered and driven through prepare/detect without editing the core.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from robodata.adapters import (ADAPTER_CONTRACT_VERSION, AdapterDescriptor, PreparedInput,
                               SourceAdapter, descriptors, detect, get_adapter, register,
                               rejected, supported, unregister)
from robodata.adapters.builtin import hdf5 as hdf5_module
from robodata.adapters.support import file_records
from robodata.cli import main
from robodata.config import APP_VERSION
from robodata.doctor import run_doctor

SCHEMA = Path(__file__).resolve().parents[1] / "robodata/adapters/schema/adapter.schema.json"


def _lerobot_dir(root: Path, *, version: str = "v3.0", repo_id: str = "lerobot/other",
                 revision: str = "deadbeef", cameras: tuple[str, ...] = ("observation.images.laptop",),
                 fps: float = 20.0) -> Path:
    (root / "meta").mkdir(parents=True)
    (root / "meta/info.json").write_text(json.dumps({
        "codebase_version": version, "fps": fps,
        "features": {camera: {"dtype": "video", "shape": [48, 64, 3]} for camera in cameras},
    }), encoding="utf-8")
    (root / "source_manifest.json").write_text(json.dumps({
        "source": {"repo_id": repo_id, "revision": revision},
    }), encoding="utf-8")
    (root / "data/chunk-000").mkdir(parents=True)
    (root / "data/chunk-000/file-000.parquet").write_bytes(b"")
    (root / "videos/chunk-000").mkdir(parents=True)
    (root / "videos/chunk-000/file-000.mp4").write_bytes(b"")
    return root


def test_registry_lists_builtins_and_matches_schema():
    items = descriptors()
    assert {item["adapter_id"] for item in items} == {
        "hdf5", "so100", "aloha", "lerobot_v21", "matching_cable", "mcap"}
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    required = set(schema["required"])
    for item in items:
        assert item["contract_version"] == ADAPTER_CONTRACT_VERSION
        assert item["source_kind"] == item["adapter_id"]
        assert required <= set(item)
        assert item["supported_versions"]


def test_hdf5_requires_pin_or_explicit_test_label(tmp_path):
    fake = tmp_path / "sample.hdf5"
    fake.write_bytes(b"not the pinned file")
    plain = get_adapter("hdf5").detect(fake)
    assert plain.supported is False
    assert "固定" in plain.unsupported_reason
    labeled = get_adapter("hdf5").detect(fake, test_label="synthetic copy")
    assert labeled.supported is True
    assert labeled.detected_version == "hdf5-test-copy"


def test_hdf5_pinned_copy_is_supported(tmp_path, monkeypatch):
    data = b"pinned bytes"
    monkeypatch.setattr(hdf5_module, "HDF5_SIZE", len(data))
    monkeypatch.setattr(hdf5_module, "HDF5_SHA256", hashlib.sha256(data).hexdigest())
    path = tmp_path / "pinned.hdf5"
    path.write_bytes(data)
    result = get_adapter("hdf5").detect(path)
    assert result.supported is True
    assert result.detected_version == "robomimic-hdf5-pinned"


def test_so100_pinned_copy_is_supported(tmp_path):
    from robodata.config import REPO_ID, REVISION
    root = _lerobot_dir(tmp_path / "so100", version="v3.0", repo_id=REPO_ID, revision=REVISION)
    result = get_adapter("so100").detect(root)
    assert result.supported is True
    assert result.detected_version == "lerobot-v3.0-so100-pinned"


def test_aloha_pinned_copy_is_supported(tmp_path):
    from robodata.aloha import ALOHA_CAMERAS, ALOHA_REPO_ID, ALOHA_REVISION
    root = _lerobot_dir(tmp_path / "aloha", version="v3.0", repo_id=ALOHA_REPO_ID,
                        revision=ALOHA_REVISION, cameras=ALOHA_CAMERAS, fps=50.0)
    result = get_adapter("aloha").detect(root)
    assert result.supported is True
    assert result.detected_version == "lerobot-v3.0-aloha-coffee-pinned"


def test_unknown_lerobot_version_is_rejected_with_version(tmp_path):
    root = _lerobot_dir(tmp_path / "v21", version="v2.1")
    result = get_adapter("so100").detect(root)
    assert result.supported is False
    assert result.detected_version == "v2.1"
    assert "v0.15" in result.unsupported_reason


def test_unpinned_lerobot_v3_is_not_silently_accepted(tmp_path):
    root = _lerobot_dir(tmp_path / "other", version="v3.0")
    result = get_adapter("so100").detect(root)
    assert result.supported is False
    assert result.detected_version == "v3.0"
    assert "repo_id" in result.unsupported_reason or "来源" in result.unsupported_reason


def test_aloha_requires_its_pinned_manifest(tmp_path):
    root = _lerobot_dir(tmp_path / "aloha", version="v3.0", repo_id="lerobot/other")
    result = get_adapter("aloha").detect(root)
    assert result.supported is False
    assert "ALOHA" in result.unsupported_reason


def test_matching_cable_detection_and_preflight(tmp_path, monkeypatch):
    monkeypatch.delenv("ROBODATA_MATCHING_EPISODES", raising=False)
    monkeypatch.delenv("ROBODATA_MATCHING_MAX_FRAMES_PER_EPISODE", raising=False)
    for episode in ("70", "68"):
        directory = tmp_path / episode
        directory.mkdir()
        for index in range(2):
            (directory / f"{index}.hdf5").write_bytes(b"frame")
    adapter = get_adapter("matching_cable")
    result = adapter.detect(tmp_path)
    assert result.supported is True
    assert result.detected_version == "matching_cable_frames_v1"
    preflight = adapter.preflight(tmp_path)
    assert preflight.supported is True
    assert preflight.summary["episodes"] == 2
    assert preflight.summary["frames"] == 4
    assert preflight.summary["episode_ids"] == [68, 70]


def test_matching_preflight_rejects_frame_gaps(tmp_path, monkeypatch):
    monkeypatch.delenv("ROBODATA_MATCHING_EPISODES", raising=False)
    monkeypatch.delenv("ROBODATA_MATCHING_MAX_FRAMES_PER_EPISODE", raising=False)
    episode = tmp_path / "68"
    episode.mkdir()
    (episode / "0.hdf5").write_bytes(b"frame")
    (episode / "2.hdf5").write_bytes(b"frame")
    adapter = get_adapter("matching_cable")
    assert adapter.detect(tmp_path).supported is True
    preflight = adapter.preflight(tmp_path)
    assert preflight.supported is False
    assert "不连续" in preflight.unsupported_reason


def test_matching_cable_is_the_best_candidate(tmp_path, monkeypatch):
    monkeypatch.delenv("ROBODATA_MATCHING_EPISODES", raising=False)
    (tmp_path / "68").mkdir()
    (tmp_path / "68/0.hdf5").write_bytes(b"frame")
    candidates = detect(tmp_path)
    assert candidates[0]["adapter_id"] == "matching_cable"
    assert candidates[0]["supported"] is True


class ToyAdapter(SourceAdapter):
    descriptor = AdapterDescriptor(
        adapter_id="toy", label="Toy directory", source_kind="toy",
        input_kind="directory", supported_versions=("toy-v1",), pinned=False,
        supports_test_label=False, description="test-only adapter",
    )

    def detect(self, path: Path, *, test_label: str | None = None):
        path = Path(path)
        if path.is_dir() and (path / "toy.txt").is_file():
            return supported("toy", "toy-v1", 0.99, ["toy.txt present"])
        return rejected("toy", "toy.txt missing")

    def prepare(self, request, raw: Path, managed: Path) -> PreparedInput:
        (raw / "toy.txt").write_text("copied", encoding="utf-8")
        return PreparedInput(files=file_records(raw, ["toy.txt"]),
                             interpretation={"kind": "toy", "batch_label": request.get("label")})

    def preview(self, request, managed: Path, run_dir: Path) -> dict:
        return {"data_root": str(managed / "source"), "episode_indices": [0]}

    def inventory(self, request, managed: Path, run_dir: Path) -> dict:
        manifest = json.loads((managed / "input_manifest.json").read_text(encoding="utf-8"))
        return {"label": "toy",
                "source": {"kind": "toy", "managed_path": str(managed),
                           "manifest_path": str(managed / "input_manifest.json"),
                           "input_fingerprint": manifest["input_fingerprint"]},
                "episodes": [{"episode_index": 0, "row_count": 1}]}


def test_toy_adapter_needs_no_core_edits(tmp_path):
    from robodata import importing
    source = tmp_path / "input"
    source.mkdir()
    (source / "toy.txt").write_text("source", encoding="utf-8")
    register(ToyAdapter())
    try:
        assert "toy" in [item["adapter_id"] for item in descriptors()]
        assert get_adapter("toy").detect(source).supported is True
        managed = tmp_path / "managed"
        request = {"source_kind": "toy", "input_path": str(source), "label": "toy batch",
                   "test_label": None, "managed_dir": str(managed)}
        copied = importing._copy(request, tmp_path / "run")
        assert copied["input_fingerprint"]
        assert (managed / "source/toy.txt").read_text(encoding="utf-8") == "copied"
        assert importing._preview(request, tmp_path / "run")["episode_indices"] == [0]
        inventory = importing._inventory(request, tmp_path / "run")
        assert inventory["episodes"][0]["row_count"] == 1
    finally:
        unregister("toy")


def test_registry_rejects_mismatched_identity():
    class Broken(SourceAdapter):
        descriptor = AdapterDescriptor(
            adapter_id="broken", label="Broken", source_kind="different",
            input_kind="file", supported_versions=("v1",), pinned=False,
            supports_test_label=False, description="broken")
    with pytest.raises(ValueError, match="source_kind"):
        register(Broken())


def test_cli_adapters_list_and_detect(tmp_path):
    assert main(["adapters", "list"]) == 0
    assert main(["adapters", "detect", "--input", str(tmp_path)]) == 3


def test_doctor_reports_version_and_adapters():
    report = run_doctor()
    assert report["app_version"] == APP_VERSION
    assert {item["name"] for item in report["checks"]} >= {
        "python", "adapters", "version_chain", "conversion_env", "work_dir"}
    assert report["ok"] is True
