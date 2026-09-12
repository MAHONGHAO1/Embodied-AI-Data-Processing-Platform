"""Exercise repeatable, isolated demo generation and its full quality/report path."""

import json
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
import pandas as pd
import pytest

from robodata import quality
from robodata.cli import main
from robodata.config import CAMERA, EPISODES, REPO_ID, REVISION
from robodata.dataset import dataset_fingerprint, load_episode
from robodata.demo import create_failure_demo, read_demo_manifest
from robodata.reports import report_html, report_json
from robodata.source import DEMO_MANIFEST_NAME, MANIFEST_NAME, SAMPLE_FILES, file_sha256


@pytest.fixture
def original_sample(tmp_path, monkeypatch):
    """Small explicit fixtures, never downloaded data; full video decoding is tested live."""
    root = tmp_path / "original"
    for relative in SAMPLE_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"explicitly generated test fixture")
    features = {name: {"shape": [6]} for name in ("observation.state", "action")}
    (root / "meta/info.json").write_text(json.dumps({"fps": 30, "features": features}), encoding="utf-8")
    (root / "meta/tasks.jsonl").write_text(json.dumps({"task_index": 0, "task": "injected test fixture"}), encoding="utf-8")
    (root / "meta/episodes.jsonl").write_text("\n".join(json.dumps({"episode_index": i, "length": 180}) for i in EPISODES), encoding="utf-8")
    for index in EPISODES:
        pd.DataFrame({"episode_index": [index] * 180, "frame_index": np.arange(180),
                      "timestamp": np.arange(180, dtype=np.float32) / 30,
                      "observation.state": [np.zeros(6, dtype=np.float32) for _ in range(180)],
                      "action": [np.ones(6, dtype=np.float32) for _ in range(180)]}).to_parquet(
                          root / f"data/chunk-000/episode_{index:06d}.parquet", index=False)
    manifest = {"repo_id": REPO_ID, "revision": REVISION,
                "files": [{"path": relative, "sha256": file_sha256(root / relative)} for relative in SAMPLE_FILES]}
    (root / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(quality, "inspect_video", lambda path: {
        "frame_count": 180, "width": 640, "height": 480, "codec": "mocked for unit test", "fps": 30,
        "pts_times": [index / 30 for index in range(180)]})
    return root


def test_demo_is_isolated_reusable_and_exposes_exact_injection_points(original_sample, tmp_path):
    before = dataset_fingerprint(original_sample)
    target = create_failure_demo(original_sample, tmp_path / "demo")
    marker = read_demo_manifest(target)
    assert marker["source_kind"] == "injected_test"
    assert [(entry["row_index"], entry["field"]) for entry in marker["injections"]] == [(50, "action"), (100, "timestamp"), (150, "timestamp")]
    assert dataset_fingerprint(original_sample) == before
    for relative in SAMPLE_FILES:
        if relative != "data/chunk-000/episode_000000.parquet":
            assert (target / relative).read_bytes() == (original_sample / relative).read_bytes()
    episode = load_episode(target, 0)
    assert np.isnan(episode.table.iloc[50]["action"][2])
    for entry in marker["injections"]:
        if entry["field"] == "timestamp":
            assert entry["after"] == float(episode.table.iloc[entry["row_index"]]["timestamp"])
    assert np.isfinite(load_episode(original_sample, 0).table.iloc[50]["action"][2])
    existing_fingerprint = dataset_fingerprint(target)
    assert create_failure_demo(original_sample, target) == target
    assert dataset_fingerprint(target) == existing_fingerprint
    (target / "user-notes.txt").write_text("Keep this note", encoding="utf-8")
    assert create_failure_demo(original_sample, target) == target
    assert (target / "user-notes.txt").read_text() == "Keep this note"


def test_failure_demo_completes_batch_and_both_report_formats(original_sample, tmp_path):
    target = create_failure_demo(original_sample, tmp_path / "demo")
    report = quality.run_batch(target)
    assert [item["status"] for item in report["episodes"]] == ["issues", "passed", "passed", "passed", "passed"]
    assert report["summary"]["coverage_performed"] == 55
    assert report["summary"]["data_issue_count"] == 7
    assert report["summary"]["load_failure_count"] == 0
    assert report["source"]["source_kind"] == "injected_test"
    assert report["source"]["integrity_status"] == "differs_from_manifest"
    positions = {(item["rule"], item["row_index"]) for item in report["issues"]}
    assert positions == {("numeric_finite", 50), ("timestamp_monotonic", 100),
                         ("timestamp_interval", 101), ("timestamp_interval", 150), ("timestamp_interval", 151),
                         ("video_timestamp_mapping", 100), ("video_timestamp_mapping", 150)}
    assert report["result_digest"] == quality.run_batch(target)["result_digest"]
    json_report = json.loads(report_json(report))
    html = report_html(report)
    assert 'data-key="data_issue_count">7<' in html
    assert json_report["summary"] == report["summary"]
    assert "故障注入测试副本" in html


def test_missing_video_continues_and_cannot_be_counted_as_pass(original_sample, tmp_path):
    target = create_failure_demo(original_sample, tmp_path / "no-video", scenario="missing_video")
    report = quality.run_batch(target)
    assert [item["status"] for item in report["episodes"]] == ["incomplete", "passed", "passed", "passed", "passed"]
    assert report["summary"]["load_failure_count"] == 1
    assert report["summary"]["not_checked_count"] == 2
    assert report["summary"]["coverage_performed"] == 52
    assert report["summary"]["passed_episode_count"] == 4
    assert json.loads(report_json(report))["summary"] == report["summary"]
    assert 'data-key="load_failure_count">1<' in report_html(report)
    assert (original_sample / f"videos/chunk-000/{CAMERA}/episode_000000.mp4").is_file()


@pytest.mark.parametrize("location", ["source", "child", "parent"])
def test_refuses_source_overlap(original_sample, location):
    target = {"source": original_sample, "child": original_sample / "demo", "parent": original_sample.parent}[location]
    before = dataset_fingerprint(original_sample)
    with pytest.raises(ValueError, match="分开"):
        create_failure_demo(original_sample, target)
    assert dataset_fingerprint(original_sample) == before


def test_refuses_unrelated_or_edited_target_without_deleting_content(original_sample, tmp_path):
    unrelated = tmp_path / "notes"
    unrelated.mkdir()
    note = unrelated / "important.txt"
    note.write_text("Preserve")
    with pytest.raises(ValueError, match="不会覆盖"):
        create_failure_demo(original_sample, unrelated)
    assert note.read_text() == "Preserve"
    target = create_failure_demo(original_sample, tmp_path / "demo")
    changed = target / "data/chunk-000/episode_000001.parquet"
    changed.write_bytes(b"user changes")
    with pytest.raises(ValueError, match="保留你的改动"):
        create_failure_demo(original_sample, target)
    assert changed.read_bytes() == b"user changes"


def test_refuses_missing_original_or_nested_injection(original_sample, tmp_path):
    target = create_failure_demo(original_sample, tmp_path / "demo")
    with pytest.raises(ValueError, match="不能从演示副本"):
        create_failure_demo(target, tmp_path / "nested")
    (original_sample / f"videos/chunk-000/{CAMERA}/episode_000004.mp4").unlink()
    with pytest.raises(ValueError, match="原始样本缺失"):
        create_failure_demo(original_sample, tmp_path / "partial")
    assert not (tmp_path / "partial").exists()


def test_marker_changes_are_part_of_fingerprint_and_bad_markers_fail(original_sample, tmp_path):
    assert read_demo_manifest(original_sample) is None
    target = create_failure_demo(original_sample, tmp_path / "demo")
    before = dataset_fingerprint(target)
    marker_path = target / DEMO_MANIFEST_NAME
    marker = read_demo_manifest(target)
    marker["description"] += " 明确的标记修改"
    marker_path.write_text(json.dumps(marker, ensure_ascii=False), encoding="utf-8")
    assert dataset_fingerprint(target) != before
    marker_path.write_text("not valid json", encoding="utf-8")
    with pytest.raises(ValueError, match="标记无法读取"):
        read_demo_manifest(target)


def _use_real_test_videos(root):
    """CLI workers run in fresh processes, so provide real tiny fixture videos."""
    for index in EPISODES:
        path = root / f"videos/chunk-000/{CAMERA}/episode_{index:06d}.mp4"
        with av.open(str(path), "w") as container:
            stream = container.add_stream("mpeg4", rate=30)
            stream.width = 32
            stream.height = 24
            stream.pix_fmt = "yuv420p"
            for frame_index in range(180):
                frame = av.VideoFrame.from_ndarray(np.full((24, 32, 3), index * 40, dtype=np.uint8), format="rgb24")
                frame.pts = frame_index
                frame.time_base = Fraction(1, 30)
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
    path = root / MANIFEST_NAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        entry["sha256"] = file_sha256(root / entry["path"])
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_cli_generates_marked_demo_and_check_writes_reports(original_sample, tmp_path, capsys):
    _use_real_test_videos(original_sample)
    target = tmp_path / "demo"
    reports = tmp_path / "reports"
    assert main(["make-failure-demo", "--data-root", str(original_sample), "--target", str(target)]) == 0
    assert "故障注入测试副本" in capsys.readouterr().out
    assert main(["check", "--data-root", str(target), "--output", str(reports),
                 "--runs-root", str(tmp_path / "runs")]) == 0
    assert (reports / "quality_report.html").is_file()
    result = json.loads((reports / "quality_report.json").read_text(encoding="utf-8"))
    assert result["source"]["source_kind"] == "injected_test"
    assert result["summary"]["data_issue_count"] == 7


def test_cli_incomplete_check_still_writes_both_reports(original_sample, tmp_path):
    _use_real_test_videos(original_sample)
    target = create_failure_demo(original_sample, tmp_path / "no-video", scenario="missing_video")
    reports = tmp_path / "reports"
    assert main(["check", "--data-root", str(target), "--output", str(reports),
                 "--runs-root", str(tmp_path / "runs")]) == 2
    assert (reports / "quality_report.html").is_file()
    result = json.loads((reports / "quality_report.json").read_text(encoding="utf-8"))
    assert result["summary"]["load_failure_count"] == 1
    assert result["summary"]["passed_episode_count"] == 4
