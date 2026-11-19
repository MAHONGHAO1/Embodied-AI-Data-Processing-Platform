"""Storage layout, GC safety, and disk preflight."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from robodata.gc import apply, scan
from robodata.layout import Layout
from robodata.process_plan import _disk_preflight
from robodata.status import status


def _run(root: Path, run_id: str, state: str, files: int = 1) -> Path:
    directory = root / run_id
    directory.mkdir(parents=True)
    (directory / "state.json").write_text(json.dumps({"run_id": run_id, "status": state}),
                                          encoding="utf-8")
    for index in range(files):
        (directory / f"file-{index}.bin").write_bytes(b"x" * 1024)
    return directory


def test_gc_only_removes_unreferenced_failed_runs(tmp_path):
    layout = Layout.current(tmp_path / "business", tmp_path / "runs").ensure()
    orphan = _run(layout.runs, "20260101T000000_orphan", "failed")
    protected = _run(layout.runs, "20260101T000001_protected", "failed")
    _run(layout.runs, "20260101T000002_completed", "completed")
    _run(layout.runs, "20260101T000003_running", "running")
    (layout.business / "batch_demo.json").write_text(json.dumps({
        "batch_id": "batch_demo",
        "outputs": [{"zip_path": str(protected / "delivery.zip")}],
    }), encoding="utf-8")

    plan = scan(layout, keep_last=0)
    assert {item["run_id"] for item in plan["candidates"]} == {orphan.name}
    result = apply(layout, keep_last=0)
    assert [item["run_id"] for item in result["deleted"]] == [orphan.name]
    assert not orphan.exists()
    assert protected.exists()
    assert (layout.runs / "20260101T000002_completed").exists()
    assert result["freed_bytes"] >= 1024


def test_gc_keep_last_keeps_newest_candidates(tmp_path):
    layout = Layout.current(tmp_path / "business", tmp_path / "runs")
    _run(layout.runs, "20260101T000000_old", "failed")
    _run(layout.runs, "20260102T000000_new", "failed")
    plan = scan(layout, keep_last=1)
    assert [item["run_id"] for item in plan["candidates"]] == ["20260101T000000_old"]
    assert any(item["run_id"] == "20260102T000000_new" for item in plan["kept"])


def test_status_reports_queue_and_disk(tmp_path):
    layout = Layout.current(tmp_path / "business", tmp_path / "runs").ensure()
    _run(layout.runs, "20260101T000000_done", "completed")
    value = status(layout.business, layout.runs)
    assert value["queue"]["total"] == 0
    assert value["runs"]["counts"]["completed"] == 1
    assert value["disk"]["free_bytes"] > 0
    assert value["gc"]["candidate_count"] == 0


def test_disk_preflight_fails_before_a_run(tmp_path, monkeypatch):
    plan = {"input": {"path": str(tmp_path / "input")}}
    (tmp_path / "input").mkdir()
    monkeypatch.setenv("ROBODATA_SKIP_DISK_CHECK", "0")

    class Usage:
        free = 1

    monkeypatch.setattr(shutil, "disk_usage", lambda path: Usage())
    message = _disk_preflight(plan, tmp_path / "business", tmp_path / "runs")
    assert message and "磁盘空间不足" in message
