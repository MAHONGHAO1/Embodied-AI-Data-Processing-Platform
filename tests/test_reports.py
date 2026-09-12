import json
import re

from robodata.reports import evidence_for_display, report_html, report_json, write_report


def fixture_report():
    issue = {"episode_index": 0, "row_index": 2, "frame_index": 2, "rule": "numeric_finite",
             "category": "data_issue", "severity": "error", "message": "故障注入 <script>alert(1)</script>",
             "evidence": {"value": "nan"}}
    return {"schema_version": "1.0", "source": {"repo_id": "fixture", "revision": "fixed-fixture",
            "source_kind": "injected_test", "input_files": []}, "rule_version": "0.1.0", "params": {},
            "started_at_utc": "2026-09-10T00:00:00+00:00", "runtime_seconds": 0.1,
            "input_fingerprint": "abc", "result_digest": "def", "limitations": ["测试副本"],
            "summary": {"episode_count": 1, "row_count": 5, "issue_count": 1, "data_issue_count": 1,
                        "load_failure_count": 0, "not_checked_count": 0, "passed_episode_count": 0,
                        "coverage_performed": 1, "coverage_total": 1},
            "episodes": [{"episode_index": 0, "row_count": 5, "expected_length": 5, "status": "issues",
                          "issues": [issue], "coverage": [{"rule": "numeric_finite", "status": "performed", "detail": ""}]}],
            "issues": [issue]}


def test_html_and_json_contain_identical_report_and_counts():
    report = fixture_report()
    rendered = report_html(report)
    embedded = re.search(r'<script id="report-data" type="application/json">(.*?)</script>', rendered, re.S).group(1)
    assert json.loads(embedded) == json.loads(report_json(report))
    for key in ("row_count", "data_issue_count", "load_failure_count", "not_checked_count", "issue_count"):
        assert f'data-key="{key}">{report["summary"][key]}</strong>' in rendered
    assert "故障注入测试副本" in rendered


def test_report_escapes_html_from_source_data():
    rendered = report_html(fixture_report())
    assert "<script>alert(1)</script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert rendered.count("</script>") == 1


def test_report_writes_portable_utf8_files(tmp_path):
    report = fixture_report()
    paths = write_report(report, tmp_path / "out")
    assert json.loads(paths["json"].read_text(encoding="utf-8")) == report
    assert "具身数据质量报告" in paths["html"].read_text(encoding="utf-8")
    assert "\ufffd" not in paths["html"].read_text(encoding="utf-8")


def test_chinese_evidence_labels_and_separate_diagnostics():
    evidence = {"error": "视频无法解码", "reasons": ["outside_readable_video_range"],
                "diagnostics": {"exception_type": "ValueError", "original_message": "Injected failure"}}
    readable = evidence_for_display(evidence)
    assert readable == {"原因说明": "视频无法解码", "触发原因": ["样本时间超出视频可读取范围"]}
    report = fixture_report()
    report["issues"][0]["evidence"] = evidence
    report["source"]["input_files"] = [{"path": "missing.mp4", "status": "missing"}]
    rendered = report_html(report)
    assert "原始错误详情（排查用）" in rendered
    assert "原因说明" in rendered
    assert "文件缺失" in rendered
    embedded = re.search(r'<script id="report-data" type="application/json">(.*?)</script>', rendered, re.S).group(1)
    assert json.loads(embedded)["issues"][0]["evidence"] == evidence


def test_html_explains_injected_faults_without_claiming_original_source_failure():
    report = fixture_report()
    report["source"]["demo_manifest"] = {
        "description": "仅任务 0 主动注入问题",
        "injections": [{"episode_index": 0, "row_index": 2, "field": "action",
                        "description": "动作第 3 维注入 NaN"}],
    }
    rendered = report_html(report)
    assert "不代表原始公开数据的质量" in rendered
    assert "动作第 3 维注入 NaN" in rendered


def test_runtime_provenance_is_readable_and_does_not_change_embedded_report():
    report = fixture_report()
    report["run_id"] = "test-run-1"
    report["execution"] = {"execution_demo": "video_timeout", "events_path": "events.jsonl",
                           "nodes": [{"id": "video_decode", "label": "视频解码", "episode_index": 0,
                                      "status": "timed_out", "elapsed_seconds": 2,
                                      "message": "<script>bad()</script>"}]}
    rendered = report_html(report)
    assert "人为运行超时演示" in rendered
    assert "test-run-1" in rendered and "超时" in rendered
    assert "<script>bad()</script>" not in rendered
    embedded = re.search(r'<script id="report-data" type="application/json">(.*?)</script>', rendered, re.S)
    assert json.loads(embedded.group(1)) == report
