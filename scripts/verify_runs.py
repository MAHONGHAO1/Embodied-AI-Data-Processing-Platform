"""Windows acceptance of real supervised runs; never modifies sample inputs."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from robodata.config import DEFAULT_DATA_ROOT, PROJECT_ROOT
from robodata.dataset import dataset_fingerprint
from robodata.demo import create_failure_demo
from robodata.runtime import get_run, read_events, start_run


def finished(run_id, runs_root, maximum=180):
    started = time.monotonic()
    while time.monotonic() - started < maximum:
        state = get_run(run_id, runs_root=runs_root)
        if state["status"] not in {"queued", "running"}:
            return state
        time.sleep(0.2)
    raise AssertionError(f"运行未在验收时间内结束：{run_id}")


def verify():
    runs_root = PROJECT_ROOT / "work" / "runs"
    before = dataset_fingerprint(DEFAULT_DATA_ROOT)
    anomaly = create_failure_demo(DEFAULT_DATA_ROOT)
    missing = create_failure_demo(DEFAULT_DATA_ROOT, scenario="missing_video")
    cases = [
        ("original", DEFAULT_DATA_ROOT, "none", 0, 0, 55),
        ("original_repeat", DEFAULT_DATA_ROOT, "none", 0, 0, 55),
        ("anomalies", anomaly, "none", 7, 0, 55),
        ("missing_video", missing, "none", 0, 1, 52),
        ("timeout_demo", DEFAULT_DATA_ROOT, "video_timeout", 0, 1, 52),
    ]
    results = {}
    for name, root, demo, issues, failures, coverage in cases:
        run_id = start_run({"kind": "quality", "data_root": str(root), "execution_demo": demo,
                            "timeouts": {"default": 60, "video_decode": 120}}, runs_root=runs_root)
        print(f"{name}: {run_id}", flush=True)
        state = finished(run_id, runs_root)
        assert state.get("report_path"), state
        report_file = Path(state["report_path"])
        report = json.loads(report_file.read_text(encoding="utf-8"))
        html_file = report_file.with_suffix(".html")
        html = html_file.read_text(encoding="utf-8")
        embedded = re.search(r'<script id="report-data" type="application/json">(.*?)</script>', html, re.S)
        assert json.loads(embedded.group(1)) == report
        summary = report["summary"]
        assert summary["data_issue_count"] == issues, summary
        assert summary["load_failure_count"] == failures, summary
        assert summary["coverage_performed"] == coverage, summary
        events = read_events(run_id, runs_root=runs_root)
        assert events and state["nodes"]
        if demo == "video_timeout":
            assert any(node["status"] == "timed_out" for node in state["nodes"])
            assert summary["passed_episode_count"] == 4
        results[name] = {"run_id": run_id, "status": state["status"], "summary": summary,
                         "result_digest": report["result_digest"], "event_count": len(events),
                         "nodes": state["nodes"], "report_path": str(report_file)}
    assert results["original"]["run_id"] != results["original_repeat"]["run_id"]
    assert results["original"]["result_digest"] == results["original_repeat"]["result_digest"]
    assert dataset_fingerprint(DEFAULT_DATA_ROOT) == before
    destination = PROJECT_ROOT / "work" / "v02-acceptance.json"
    destination.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"PASS: {destination}", flush=True)


if __name__ == "__main__":
    verify()
