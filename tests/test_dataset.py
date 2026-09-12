import json
from fractions import Fraction

import av
import numpy as np
import pandas as pd
import pytest

from robodata.config import CAMERA
from robodata.dataset import dataset_fingerprint, load_episode, load_metadata
from robodata.video import inspect_video, read_video_frame


def make_dataset(root):
    (root / "meta").mkdir()
    info = {"fps": 30, "features": {"observation.state": {"names": ["a", "b"], "shape": [2]}}}
    (root / "meta/info.json").write_text(json.dumps(info), encoding="utf-8")
    (root / "meta/episodes.jsonl").write_text(json.dumps({"episode_index": 0, "length": 3,
                                                         "tasks": ["sample"]}), encoding="utf-8")
    (root / "meta/tasks.jsonl").write_text(json.dumps({"task_index": 0, "task": "sample"}), encoding="utf-8")
    directory = root / "data/chunk-000"
    directory.mkdir(parents=True)
    pd.DataFrame({"frame_index": [0, 1, 2], "timestamp": [0.0, 1 / 30, 2 / 30],
                  "observation.state": [[1.0, 2.0], [np.nan, 2.0], [1.0]],
                  "episode_index": [0, 0, 0], "task_index": [0, 0, 0]}).to_parquet(
        directory / "episode_000000.parquet")


def test_reader_preserves_missing_columns_and_bad_values_for_quality_checks(tmp_path):
    make_dataset(tmp_path)
    episode = load_episode(tmp_path, 0)
    assert "action" not in episode.table
    assert np.isnan(episode.table.iloc[1]["observation.state"][0])
    assert len(episode.table.iloc[2]["observation.state"]) == 1
    assert episode.expected_length == 3
    assert episode.field_names == ["a", "b"]
    assert episode.metadata["tasks"][0]["task"] == "sample"
    assert episode.video_path == tmp_path / f"videos/chunk-000/{CAMERA}/episode_000000.mp4"


def test_invalid_metadata_fails_explicitly(tmp_path):
    make_dataset(tmp_path)
    path = tmp_path / "meta/info.json"
    info = json.loads(path.read_text())
    info["fps"] = 0
    path.write_text(json.dumps(info))
    with pytest.raises(ValueError, match="FPS"):
        load_metadata(tmp_path)


def test_fingerprint_includes_content_and_missing_markers(tmp_path):
    first = dataset_fingerprint(tmp_path)
    make_dataset(tmp_path)
    second = dataset_fingerprint(tmp_path)
    assert first != second
    path = tmp_path / "meta/tasks.jsonl"
    old = path.read_bytes()
    path.write_bytes(old.replace(b"sample", b"SAMPLE"))
    assert path.stat().st_size == len(old)
    third = dataset_fingerprint(tmp_path)
    assert third != second
    path.unlink()
    assert dataset_fingerprint(tmp_path) != third


def make_video(path):
    with av.open(str(path), "w") as container:
        stream = container.add_stream("mpeg4", rate=30)
        stream.width = 64
        stream.height = 48
        stream.pix_fmt = "yuv420p"
        for i in range(5):
            frame = av.VideoFrame.from_ndarray(np.full((48, 64, 3), i * 40, dtype=np.uint8), format="rgb24")
            frame.pts = i
            frame.time_base = Fraction(1, 30)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def test_video_nearest_pts_and_range_validation(tmp_path):
    path = tmp_path / "tiny.mp4"
    make_video(path)
    metadata = inspect_video(path)
    assert metadata["frame_count"] == 5
    assert metadata["width"] == 64
    for index in (0, 2, 4):
        result = read_video_frame(path, index / 30)
        assert result["frame_index"] == index
        assert result["pts_time"] == pytest.approx(index / 30)
        assert result["image"].shape == (48, 64, 3)
        assert result["image"].mean() == pytest.approx(index * 40, abs=5)
    nearest = read_video_frame(path, 2.4 / 30)
    assert nearest["frame_index"] == 2
    for invalid in (-1, 20, float("nan")):
        with pytest.raises(ValueError):
            read_video_frame(path, invalid)
    # Identical path with different bytes cannot silently reuse video metadata.
    path.write_bytes(b"invalid video")
    with pytest.raises(av.error.FFmpegError):
        inspect_video(path)
