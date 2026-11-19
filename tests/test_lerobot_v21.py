"""Generic LeRobot v2.1 input: adapter, metadata, subset guard, and conversion."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import av
import numpy as np
import pandas as pd
import pytest

from robodata.adapters import get_adapter
from robodata.conversion import CONVERSION_PYTHON
from robodata.dataset import load_episode, load_metadata

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FEATURES = {
    "action": {"dtype": "float32", "shape": [6], "names": [f"a{i}" for i in range(6)]},
    "observation.state": {"dtype": "float32", "shape": [6], "names": [f"s{i}" for i in range(6)]},
    "observation.images.camera_0": {"dtype": "video", "shape": [32, 32, 3],
                                     "names": ["height", "width", "channels"]},
    "timestamp": {"dtype": "float32", "shape": [1], "names": None},
    "frame_index": {"dtype": "int64", "shape": [1], "names": None},
    "episode_index": {"dtype": "int64", "shape": [1], "names": None},
    "index": {"dtype": "int64", "shape": [1], "names": None},
    "task_index": {"dtype": "int64", "shape": [1], "names": None},
}


def _vector(value: float) -> dict:
    return {"min": [value] * 6, "max": [value + 1] * 6, "mean": [value + 0.5] * 6,
            "std": [0.1] * 6, "count": [5], "q01": [value] * 6, "q10": [value] * 6,
            "q50": [value + 0.5] * 6, "q90": [value + 1] * 6, "q99": [value + 1] * 6}


def _scalar(value: float) -> dict:
    return {"min": [value], "max": [value + 1], "mean": [value + 0.5], "std": [0.1],
            "count": [5], "q01": [value], "q10": [value], "q50": [value + 0.5],
            "q90": [value + 1], "q99": [value + 1]}


def _video() -> dict:
    def channel(value: float):
        return [[[value]], [[value]], [[value]]]
    return {"min": channel(0.0), "max": channel(1.0), "mean": channel(0.5),
            "std": channel(0.1), "count": [5], "q01": channel(0.0), "q10": channel(0.0),
            "q50": channel(0.5), "q90": channel(1.0), "q99": channel(1.0)}


def make_v21(root: Path) -> Path:
    (root / "meta").mkdir(parents=True)
    (root / "data/chunk-000").mkdir(parents=True)
    (root / "videos/chunk-000/observation.images.camera_0").mkdir(parents=True)
    info = {"codebase_version": "v2.1", "robot_type": "synthetic_v21", "total_episodes": 2,
            "total_frames": 10, "total_tasks": 1, "total_videos": 2, "total_chunks": 1,
            "chunks_size": 1000, "fps": 30, "splits": {"train": "0:2"},
            "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
            "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
            "features": FEATURES}
    (root / "meta/info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    (root / "meta/tasks.jsonl").write_text(json.dumps({"task_index": 0, "task": "synthetic v21"}) + "\n", encoding="utf-8")
    (root / "meta/episodes.jsonl").write_text("\n".join(json.dumps(
        {"episode_index": i, "tasks": ["synthetic v21"], "length": 5}) for i in range(2)) + "\n", encoding="utf-8")
    for episode in range(2):
        table = pd.DataFrame({
            "episode_index": [episode] * 5, "frame_index": list(range(5)),
            "timestamp": np.arange(5, dtype=np.float32) / 30,
            "action": [np.arange(6, dtype=np.float32) + episode + i * 0.1 for i in range(5)],
            "observation.state": [np.arange(6, dtype=np.float32) + episode + i * 0.2 for i in range(5)],
            "index": [episode * 5 + i for i in range(5)], "task_index": [0] * 5,
        })
        table.to_parquet(root / f"data/chunk-000/episode_{episode:06d}.parquet")
        with av.open(str(root / f"videos/chunk-000/observation.images.camera_0/episode_{episode:06d}.mp4"), "w") as container:
            stream = container.add_stream("libx264", rate=30)
            stream.width, stream.height, stream.pix_fmt = 32, 32, "yuv420p"
            for frame_index in range(5):
                image = np.full((32, 32, 3), (episode * 40 + frame_index * 30) % 255, dtype=np.uint8)
                for packet in stream.encode(av.VideoFrame.from_ndarray(image, format="rgb24")):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
    stats = {"action": _vector(0.0), "observation.state": _vector(0.0),
             "observation.images.camera_0": _video(), "timestamp": _scalar(0.0),
             "frame_index": _scalar(0.0), "episode_index": _scalar(0.0),
             "index": _scalar(0.0), "task_index": _scalar(0.0)}
    (root / "meta/stats.json").write_text(json.dumps(stats), encoding="utf-8")
    (root / "meta/episodes_stats.jsonl").write_text("\n".join(json.dumps(
        {"episode_index": i, "stats": stats}) for i in range(2)) + "\n", encoding="utf-8")
    (root / "robodata_source.json").write_text(json.dumps(
        {"schema_version": 1, "kind": "lerobot_v21", "source_format": "v2.1",
         "original_root": str(root)}), encoding="utf-8")
    return root


def test_adapter_detects_and_preflights_generic_v21(tmp_path):
    source = make_v21(tmp_path / "v21")
    adapter = get_adapter("lerobot_v21")
    assert adapter.detect(source).supported is True
    preflight = adapter.preflight(source)
    assert preflight.summary["episodes"] == 2
    assert preflight.summary["frames"] == 10
    assert preflight.summary["cameras"] == ["observation.images.camera_0"]


def test_importing_copy_adds_marker_and_inventory(tmp_path):
    from robodata import importing
    source = make_v21(tmp_path / "v21")
    managed = tmp_path / "managed"
    request = {"source_kind": "lerobot_v21", "input_path": str(source), "label": "v21 test",
               "test_label": None, "managed_dir": str(managed)}
    copied = importing._copy(request, tmp_path / "run")
    assert copied["input_fingerprint"]
    assert (managed / "source/robodata_source.json").is_file()
    metadata = load_metadata(managed / "source")
    assert metadata["generic"] is True
    assert metadata["episode_indices"] == [0, 1]
    assert len(load_episode(managed / "source", 0).camera_paths) == 1
    inventory = importing._inventory(request, tmp_path / "run")
    assert inventory["source"]["kind"] == "lerobot_v21"
    assert len(inventory["episodes"]) == 2


def test_v21_subset_is_rejected_explicitly(tmp_path):
    from tools.conversion.lerobot_v21_worker import ConversionError, validate_source
    source = make_v21(tmp_path / "v21")
    spec = {"snapshot_id": "subset", "episodes": [
        {"source_episode_index": 0, "output_episode_index": 0, "row_count": 5,
         "annotation": {"task": "one"}}]}
    with pytest.raises(ConversionError) as error:
        validate_source(source, spec)
    assert error.value.code == "subset_unsupported"


@pytest.mark.skipif(not CONVERSION_PYTHON.is_file(), reason="requires isolated conversion environment")
def test_official_v21_converter_and_loader(tmp_path):
    source = make_v21(tmp_path / "v21")
    output = tmp_path / "output"
    artifacts = tmp_path / "artifacts"
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"snapshot_id": "v21-fixture", "episodes": [
        {"source_episode_index": i, "output_episode_index": i, "row_count": 5,
         "annotation": {"task": "synthetic v21"}} for i in range(2)]}), encoding="utf-8")
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1"}
    for stage in ("format_write", "official_verify"):
        result = subprocess.run([str(CONVERSION_PYTHON), str(PROJECT_ROOT / "tools/conversion/lerobot_v21_worker.py"),
                                 "--stage", stage, "--input", str(source), "--output", str(output),
                                 "--artifacts", str(artifacts), "--spec", str(spec)],
                                cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=300)
        assert result.returncode == 0, result.stdout + result.stderr
    details = json.loads((artifacts / "steps/official_verify.json").read_text(encoding="utf-8"))["details"]
    assert details["episode_count"] == 2
    assert details["row_count"] == 10
    assert details["source_numeric_consistency_rechecked"] is True
    assert details["snapshot_id"] == "v21-fixture"
    assert json.loads((output / "meta/info.json").read_text(encoding="utf-8"))["codebase_version"] == "v3.0"
