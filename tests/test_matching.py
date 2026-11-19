import hashlib
import json
import os
from pathlib import Path

import pytest

from robodata.matching import (ACTION_FIELD, CAMERAS, DEPTH_CAMERAS, STATE_FIELD,
                               assert_source_unchanged, capabilities_from_fields, discover_episodes,
                               file_records, parse_episode_selection, raw_frames)
from robodata.web import RunRequest


def base_fields(cameras=(0,), depth=(), optional=()):
    fields = {ACTION_FIELD, STATE_FIELD, "timestamps"}
    fields.update(f"observations/rgb_images/camera_{index}" for index in cameras)
    fields.update(f"observations/depth_images/camera_{index}" for index in depth)
    fields.update(optional)
    return fields


def test_parse_episode_selection_accepts_ranges_and_values():
    assert parse_episode_selection("68-70,74,70") == [68, 69, 70, 74]
    assert parse_episode_selection("") is None
    with pytest.raises(ValueError):
        parse_episode_selection("70-68")


def test_discover_matching_episode_directories(tmp_path, monkeypatch):
    for episode_id in (74, 68, 70):
        directory = tmp_path / str(episode_id)
        directory.mkdir()
        for frame_index in (2, 0, 1):
            (directory / f"{frame_index}.hdf5").write_bytes(b"frame")
    (tmp_path / "notes").mkdir()
    monkeypatch.delenv("ROBODATA_MATCHING_MAX_FRAMES_PER_EPISODE", raising=False)

    episodes = discover_episodes(tmp_path)

    assert [item["source_episode_id"] for item in episodes] == [68, 70, 74]
    assert [item["episode_index"] for item in episodes] == [0, 1, 2]
    assert [path.name for path in episodes[0]["frames"]] == ["0.hdf5", "1.hdf5", "2.hdf5"]
    assert len(file_records(tmp_path, episodes)) == 9


def test_discover_matching_single_episode_and_frame_limit(tmp_path, monkeypatch):
    directory = tmp_path / "73"
    directory.mkdir()
    for frame_index in range(4):
        (directory / f"{frame_index}.hdf5").write_bytes(b"x")
    monkeypatch.setenv("ROBODATA_MATCHING_MAX_FRAMES_PER_EPISODE", "2")

    episodes = discover_episodes(directory, "73")

    assert len(episodes) == 1
    assert episodes[0]["source_episode_id"] == 73
    assert [path.name for path in episodes[0]["frames"]] == ["0.hdf5", "1.hdf5"]


def test_run_request_accepts_matching_source():
    request = RunRequest(operation="import", source_kind="matching_cable", path="/tmp/data", label="matching")
    assert request.source_kind == "matching_cable"


def test_feature_definitions_only_include_present_cameras_and_optional_fields():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "conversion"))
    from matching_preview import feature_definitions

    shapes = {"observation.images.camera_0": (24, 32, 3)}
    features = feature_definitions(shapes, ["puppet/eef", "puppet/6f"])

    assert "observation.images.camera_0" in features
    assert "observation.images.camera_1" not in features
    assert "observation.eef" in features
    assert "observation.force_torque" in features
    assert "observation.joint_effort" not in features


def test_capabilities_treat_missing_depth_and_optional_fields_as_visible_absences():
    capabilities = capabilities_from_fields(base_fields(cameras=(0,)))

    assert capabilities["cameras"] == [CAMERAS[0]]
    assert capabilities["missing_cameras"] == list(CAMERAS[1:])
    assert capabilities["depth_present"] == []
    assert capabilities["depth_missing"] == list(DEPTH_CAMERAS)
    assert capabilities["optional_present"] == []
    assert capabilities["optional_missing"] == ["puppet/eef", "master/eef", "puppet/joint_effort", "puppet/6f"]
    assert capabilities["tactile"]["status"] == "not_provided"


def test_capabilities_report_present_streams_without_requiring_all_four_cameras():
    capabilities = capabilities_from_fields(base_fields(
        cameras=(0, 2), depth=(1,), optional=("puppet/eef", "puppet/6f")))

    assert capabilities["cameras"] == [CAMERAS[0], CAMERAS[2]]
    assert capabilities["depth_present"] == [DEPTH_CAMERAS[0]]
    assert capabilities["optional_present"] == ["puppet/eef", "puppet/6f"]


def test_capabilities_refuse_a_frame_without_state_or_rgb():
    with pytest.raises(ValueError, match="至少需要一路 RGB"):
        capabilities_from_fields({ACTION_FIELD, STATE_FIELD, "timestamps"})
    with pytest.raises(ValueError, match="必填字段"):
        capabilities_from_fields({"observations/rgb_images/camera_0"})


def test_raw_frames_refuses_extra_or_missing_frames(tmp_path):
    episode = tmp_path / "68"
    episode.mkdir()
    for index in range(2):
        (episode / f"{index}.hdf5").write_bytes(b"frame")

    assert [path.name for path in raw_frames(tmp_path, 68, 2)] == ["0.hdf5", "1.hdf5"]

    (episode / "2.hdf5").write_bytes(b"frame")
    with pytest.raises(ValueError, match="帧数变化"):
        raw_frames(tmp_path, 68, 2)


def test_assert_source_unchanged_recomputes_sha256_even_when_size_and_mtime_match(tmp_path):
    frame = tmp_path / "68" / "0.hdf5"
    frame.parent.mkdir(parents=True)
    frame.write_bytes(b"original-frame")
    stat = frame.stat()
    manifest = {"files": [{"path": "68/0.hdf5", "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                           "episode_id": 68, "sha256": hashlib.sha256(b"original-frame").hexdigest()}]}
    manifest_path = tmp_path / "source_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    source = {"data_root": str(tmp_path), "manifest_path": str(manifest_path), "input_fingerprint": "fingerprint"}

    assert assert_source_unchanged(source) == "fingerprint"

    frame.write_bytes(b"modified-frame")
    os.utime(frame, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with pytest.raises(ValueError, match="已变化"):
        assert_source_unchanged(source)