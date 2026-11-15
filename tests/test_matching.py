from pathlib import Path

import pytest

from robodata.matching import discover_episodes, file_records, parse_episode_selection
from robodata.web import RunRequest


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