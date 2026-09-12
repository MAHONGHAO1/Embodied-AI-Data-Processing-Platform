"""Explicitly injected fixtures; no changes to downloaded public samples."""

from pathlib import Path
import json

import numpy as np
import pandas as pd
import pytest

from robodata.models import Episode
from robodata import quality


def make_episode(tmp_path: Path, index: int = 0, rows: int = 5) -> Episode:
    table = pd.DataFrame({
        "episode_index": [index] * rows, "frame_index": np.arange(rows),
        "timestamp": np.arange(rows) / 30,
        "observation.state": [np.zeros(6) for _ in range(rows)],
        "action": [np.ones(6) for _ in range(rows)],
    })
    video = tmp_path / f"episode_{index:06d}.mp4"
    video.write_bytes(b"explicit injected fixture; decoder is mocked")
    return Episode(episode_index=index, table=table,
                   metadata={"info": {"features": {"observation.state": {"shape": [6]}, "action": {"shape": [6]}}}},
                   video_path=video, data_path=tmp_path / f"episode_{index:06d}.parquet",
                   field_names=[f"joint_{i}" for i in range(6)], fps=30.0,
                   source={"source_kind": "injected_test"}, expected_length=rows)


def decoded(rows=5):
    return {"frame_count": rows, "width": 640, "height": 480, "codec": "av1", "fps": 30.0,
            "pts_times": [index / 30 for index in range(rows)]}


@pytest.fixture
def valid_episode(tmp_path, monkeypatch):
    episode = make_episode(tmp_path)
    monkeypatch.setattr(quality, "inspect_video", lambda path: decoded())
    return episode


def find_issue(result, rule, category="data_issue"):
    return [item for item in result["issues"] if item["rule"] == rule and item["category"] == category]


def test_good_data_checks_all_rules_and_exposes_sample_references(valid_episode):
    result = quality.run_checks(valid_episode)
    assert result["status"] == "passed"
    assert result["issues"] == []
    assert {item["rule"] for item in result["coverage"]} == set(quality.RULES)
    assert all(item["status"] == "performed" for item in result["coverage"])
    assert [item["row_index"] for item in result["video"]["sample_references"]] == [0, 2, 4]
    assert result["video"]["mapping_max_error_seconds"] == 0


def test_injected_missing_field_is_detected_without_crashing(valid_episode):
    valid_episode.table = valid_episode.table.drop(columns=["frame_index"])
    result = quality.run_checks(valid_episode)
    assert find_issue(result, "required_fields")[0]["evidence"]["missing_fields"] == ["frame_index"]
    assert find_issue(result, "frame_index", "not_checked")
    assert result["status"] == "incomplete"
    assert all(item["frame_index"] is None for item in result["issues"])


def test_injected_nan_and_infinity_remain_strict_json(valid_episode):
    valid_episode.table.at[2, "action"] = np.array([0, 0, np.nan, 0, np.inf, 0])
    valid_episode.table.at[1, "timestamp"] = np.nan
    result = quality.run_checks(valid_episode)
    issues = find_issue(result, "numeric_finite")
    assert {(item["row_index"], item["evidence"]["field"]) for item in issues} == {(2, "action"), (1, "timestamp")}
    assert issues[1]["evidence"]["value"][2] == "nan"
    json.dumps(result, allow_nan=False)
    assert find_issue(result, "timestamp_monotonic", "not_checked")


def test_injected_reversal_is_localized(valid_episode):
    valid_episode.table.loc[2, "timestamp"] = 0.01
    result = quality.run_checks(valid_episode)
    assert [item["row_index"] for item in find_issue(result, "timestamp_monotonic")] == [2]


def test_injected_time_gap_and_rule_parameter_change(valid_episode):
    valid_episode.table.loc[2:, "timestamp"] += 0.03
    strict = quality.run_checks(valid_episode, check_video=False)
    permissive = quality.run_checks(valid_episode, {"interval_relative_tolerance": 1}, check_video=False)
    assert [item["row_index"] for item in find_issue(strict, "timestamp_interval")] == [2]
    assert not find_issue(permissive, "timestamp_interval")


def test_injected_shape_and_frame_index_errors(valid_episode):
    valid_episode.table.at[3, "action"] = np.ones(5)
    valid_episode.table.loc[4, "frame_index"] = 3
    result = quality.run_checks(valid_episode)
    assert find_issue(result, "vector_shape")[0]["row_index"] == 3
    assert find_issue(result, "frame_index")[0]["row_index"] == 4
    assert result["status"] == "issues"


def test_timestamp_resets_between_episodes_are_valid(tmp_path, monkeypatch):
    monkeypatch.setattr(quality, "inspect_video", lambda path: decoded())
    for index in (0, 1):
        assert quality.run_checks(make_episode(tmp_path, index))["status"] == "passed"


@pytest.mark.parametrize("failure", ["missing", "decode"])
def test_video_failures_are_distinct_from_data_issues(valid_episode, monkeypatch, failure):
    if failure == "missing":
        valid_episode.video_path.unlink()
    else:
        def fail(path):
            raise ValueError("Injected decode failure")
        monkeypatch.setattr(quality, "inspect_video", fail)
    result = quality.run_checks(valid_episode)
    assert result["status"] == "incomplete"
    assert find_issue(result, "video_decode", "load_failure")
    failure_issue = find_issue(result, "video_decode", "load_failure")[0]
    assert "读取主相机视频失败" in failure_issue["message"]
    assert "Injected decode failure" not in failure_issue["message"]
    assert "diagnostics" in failure_issue["evidence"]
    assert find_issue(result, "video_timestamp_mapping", "not_checked")
    assert not any(item["category"] == "data_issue" for item in result["issues"])
    assert all(item["status"] == "unavailable" for item in result["coverage"] if item["rule"].startswith("video_"))


def test_frame_count_mismatch_never_claims_mapping_valid(valid_episode, monkeypatch):
    monkeypatch.setattr(quality, "inspect_video", lambda path: decoded(rows=4))
    result = quality.run_checks(valid_episode)
    assert find_issue(result, "video_frame_count")
    assert find_issue(result, "video_timestamp_mapping", "not_checked")
    assert result["video"]["status"] == "failed"


def test_injected_video_time_shift_is_detected(valid_episode, monkeypatch):
    shifted = decoded()
    shifted["pts_times"] = [item + 0.05 for item in shifted["pts_times"]]
    monkeypatch.setattr(quality, "inspect_video", lambda path: shifted)
    result = quality.run_checks(valid_episode)
    assert len(find_issue(result, "video_timestamp_mapping")) == 5
    assert result["video"]["status"] == "failed"


def test_small_uniform_time_shift_cannot_pass_with_unreadable_last_frame(valid_episode):
    valid_episode.table["timestamp"] += 0.005
    result = quality.run_checks(valid_episode)
    assert not find_issue(result, "timestamp_interval")
    assert not find_issue(result, "timestamp_monotonic")
    mapping = find_issue(result, "video_timestamp_mapping")
    assert [item["row_index"] for item in mapping] == [4]
    assert mapping[0]["evidence"]["reasons"] == ["outside_readable_video_range"]
    assert result["video"]["status"] == "failed"


def test_float32_scale_timestamp_drift_stays_within_reader_boundary(valid_episode):
    valid_episode.table["timestamp"] += 0.000001
    result = quality.run_checks(valid_episode)
    assert result["status"] == "passed"


def test_negative_timestamp_is_explicit_even_inside_video_epsilon(valid_episode):
    valid_episode.table.loc[0, "timestamp"] = -0.000001
    result = quality.run_checks(valid_episode)
    violations = find_issue(result, "timestamp_monotonic")
    assert [item["row_index"] for item in violations] == [0]
    assert "为负" in violations[0]["message"]
    assert result["status"] == "issues"


def test_skipped_video_check_is_not_counted_as_pass(valid_episode):
    result = quality.run_checks(valid_episode, check_video=False)
    assert result["status"] == "incomplete"
    assert len([item for item in result["issues"] if item["category"] == "not_checked"]) == 3


@pytest.mark.parametrize("params", [{"timestamp_absolute_tolerance": -1},
                                    {"interval_relative_tolerance": float("nan")}, {"unknown": 2}])
def test_bad_parameters_fail_early(valid_episode, params):
    with pytest.raises(ValueError):
        quality.run_checks(valid_episode, params)


def setup_batch(tmp_path, monkeypatch):
    from robodata import dataset, source
    episodes = {index: make_episode(tmp_path, index) for index in (0, 1)}
    monkeypatch.setattr(quality, "inspect_video", lambda path: decoded())
    monkeypatch.setattr(dataset, "load_episode", lambda root, index: episodes[index])
    monkeypatch.setattr(dataset, "load_metadata", lambda root: {"manifest": {"fixture": "injected_test"}})
    monkeypatch.setattr(source, "SAMPLE_FILES", tuple(item.video_path.name for item in episodes.values()))
    monkeypatch.setattr(source, "MANIFEST_NAME", "fixture-manifest.json")
    monkeypatch.setattr(dataset, "SAMPLE_FILES", source.SAMPLE_FILES)
    monkeypatch.setattr(dataset, "MANIFEST_NAME", source.MANIFEST_NAME)
    return episodes


def test_batch_continues_after_missing_video_and_is_deterministic(tmp_path, monkeypatch):
    episodes = setup_batch(tmp_path, monkeypatch)
    episodes[0].video_path.unlink()
    first = quality.run_batch(tmp_path, (0, 1), source_kind="injected_test")
    second = quality.run_batch(tmp_path, (0, 1), source_kind="injected_test")
    assert [item["status"] for item in first["episodes"]] == ["incomplete", "passed"]
    assert first["summary"]["passed_episode_count"] == 1
    assert first["summary"]["load_failure_count"] == 1
    assert first["result_digest"] == second["result_digest"]
    assert first["source"]["source_kind"] == "injected_test"


def test_batch_continues_after_one_table_fails(tmp_path, monkeypatch):
    from robodata import dataset
    episodes = setup_batch(tmp_path, monkeypatch)
    def load(root, index):
        if index == 0:
            raise ValueError("Injected unreadable parquet")
        return episodes[index]
    monkeypatch.setattr(dataset, "load_episode", load)
    result = quality.run_batch(tmp_path, (0, 1), source_kind="injected_test")
    assert [item["status"] for item in result["episodes"]] == ["load_failed", "passed"]
    assert result["summary"]["row_count"] == 5
    assert result["summary"]["coverage_performed"] == len(quality.RULES)
    assert result["summary"]["coverage_total"] == 2 * len(quality.RULES)
    failure = result["episodes"][0]["issues"][0]
    assert "读取任务数据失败" in failure["message"]
    assert "Injected unreadable parquet" not in failure["message"]
    assert "Injected unreadable parquet" not in failure["evidence"]["error"]
    assert failure["evidence"]["diagnostics"]["original_message"] == "Injected unreadable parquet"


def test_source_and_rule_changes_invalidate_result(tmp_path, monkeypatch):
    episodes = setup_batch(tmp_path, monkeypatch)
    original = quality.run_batch(tmp_path, (0, 1), source_kind="injected_test")
    changed_rules = quality.run_batch(tmp_path, (0, 1), params={"interval_relative_tolerance": 0.2}, source_kind="injected_test")
    assert original["input_fingerprint"] == changed_rules["input_fingerprint"]
    assert original["result_digest"] != changed_rules["result_digest"]
    episodes[1].video_path.write_bytes(b"changed fixture content")
    changed_input = quality.run_batch(tmp_path, (0, 1), source_kind="injected_test")
    assert original["input_fingerprint"] != changed_input["input_fingerprint"]
    assert original["result_digest"] != changed_input["result_digest"]


def test_modified_source_is_visible_against_download_manifest(tmp_path, monkeypatch):
    from robodata import dataset
    from robodata.config import REPO_ID, REVISION
    episodes = setup_batch(tmp_path, monkeypatch)
    manifest = {"repo_id": REPO_ID, "revision": REVISION,
                "files": [{"path": item.video_path.name, "sha256": quality._sha256(item.video_path)}
                          for item in episodes.values()]}
    monkeypatch.setattr(dataset, "load_metadata", lambda root: {"manifest": manifest})
    before = quality.run_batch(tmp_path, (0, 1), source_kind="injected_test")
    assert before["source"]["integrity_status"] == "matches_manifest"
    episodes[1].video_path.write_bytes(b"modified bytes after download")
    after = quality.run_batch(tmp_path, (0, 1), source_kind="injected_test")
    assert after["source"]["integrity_status"] == "differs_from_manifest"
    entry = next(item for item in after["source"]["input_files"] if item["path"] == episodes[1].video_path.name)
    assert entry["hash_matches_manifest"] is False
