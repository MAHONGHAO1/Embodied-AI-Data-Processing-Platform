"""Real runtime failure paths plus registry integrity; no training or public writes."""
import json
from pathlib import Path

import pytest

from robodata import conversion, runtime
from robodata.dataset import dataset_fingerprint
from robodata.sources import HDF5_PATH


def conversion_request(source: Path, output: Path, **options):
    return {"kind": "conversion", "input_path": str(source), "output_root": str(output), **options}


def test_bad_source_records_chinese_evidence_and_registers_nothing(tmp_path):
    if not conversion.CONVERSION_PYTHON.is_file():
        pytest.skip("requires the separately locked conversion environment")
    source = tmp_path / "explicit-invalid-input.hdf5"
    source.write_bytes(b"intentional hash mismatch fixture")
    outputs, runs = tmp_path / "exports", tmp_path / "runs"
    run_id = runtime.start_run(conversion_request(source, outputs), runs)
    state = runtime.wait_run(run_id, runs, 30)
    assert state["status"] == "failed"
    node = state["nodes"][0]
    assert node["node_id"] == "file_validation" and node["status"] == "failed"
    assert "固定公开版本不一致" in node["message"]
    assert node["diagnostics"]["code"] == "source_hash_mismatch"
    assert node["evidence"]["actual_size"] == source.stat().st_size
    assert "traceback" in node["diagnostics"]
    assert all(n["status"] == "skipped" for n in state["nodes"][1:])
    assert conversion.list_exports(outputs) == [] and state["available_path"] is None
    errors = runtime.read_events(run_id, runs, node_id="file_validation", level="ERROR")
    assert errors[-1]["evidence"]["actual_size"] == source.stat().st_size


def test_conversion_timeout_preserves_timeout_node_and_terminates_process(tmp_path):
    if not conversion.CONVERSION_PYTHON.is_file():
        pytest.skip("requires the separately locked conversion environment")
    source = tmp_path / "explicit-timeout-fixture.hdf5"
    source.write_bytes(b"this is a runtime timeout fixture, not robot data")
    runs, outputs = tmp_path / "runs", tmp_path / "exports"
    run_id = runtime.start_run(conversion_request(source, outputs, timeouts={"file_validation": 0.000001}), runs)
    state = runtime.wait_run(run_id, runs, 30)
    node = state["nodes"][0]
    assert state["status"] == "failed" and node["status"] == "timed_out"
    assert node["worker_terminated"] is True and not runtime._alive(node["worker_pid"])
    assert conversion.latest_export(outputs) is None


def test_format_write_failure_preserves_readonly_steps_and_no_registry(tmp_path):
    if not conversion.CONVERSION_PYTHON.is_file() or not HDF5_PATH.is_file():
        pytest.skip("requires the fixed downloaded HDF5 and conversion environment")
    output_blocker = tmp_path / "not-a-directory"
    output_blocker.write_text("explicit output write failure fixture", encoding="utf-8")
    runs = tmp_path / "runs"
    source_before = __import__("robodata.source", fromlist=["file_sha256"]).file_sha256(HDF5_PATH)
    run_id = runtime.start_run(conversion_request(HDF5_PATH, output_blocker), runs)
    state = runtime.wait_run(run_id, runs, 60)
    assert state["status"] == "failed"
    assert [n["status"] for n in state["nodes"][:3]] == ["completed"] * 3
    failed = next(n for n in state["nodes"] if n["status"] == "failed")
    assert failed["node_id"] == "format_write"
    assert failed["diagnostics"]["stage"] == "format_write"
    assert "写入失败" in failed["message"] and "traceback" in failed["diagnostics"]
    assert state["available_path"] is None
    assert source_before == __import__("robodata.source", fromlist=["file_sha256"]).file_sha256(HDF5_PATH)


def test_registry_ignores_unregistered_changed_and_outside_outputs(tmp_path):
    outputs = tmp_path / "exports"
    staging = outputs / "explicit-registry-fixture" / "staging"
    (staging / "meta").mkdir(parents=True)
    info = staging / "meta/info.json"
    info.write_text("explicit registry integrity fixture", encoding="utf-8")
    assert conversion.latest_export(outputs) is None
    marker = staging.parent / "available.json"
    record = {"status": "available", "output_path": str(staging), "run_id": "explicit-registry-fixture",
              "fingerprint": dataset_fingerprint(staging)}
    marker.write_text(json.dumps(record), encoding="utf-8")
    found = conversion.latest_export(outputs)
    assert found["path"] == found["output_path"] == str(staging)
    info.write_text("modified after fixture registration", encoding="utf-8")
    assert conversion.latest_export(outputs) is None
    record.update(output_path=str(tmp_path), fingerprint=dataset_fingerprint(tmp_path))
    marker.write_text(json.dumps(record), encoding="utf-8")
    assert conversion.list_exports(outputs) == []


def test_zero_exit_without_success_record_does_not_pass_stage(tmp_path):
    directory = tmp_path / "explicit-no-result-fixture"
    directory.mkdir()
    state = {"run_id": directory.name, "started_at": runtime.utc_now(), "nodes": [runtime._node("official_verify")],
             "request": {}, "artifacts": {}}
    assert conversion._read_step(directory, state, "official_verify", True) is None
    assert state["status"] == "failed"
    assert state["nodes"][0]["status"] == "failed"
    assert "有效的成功记录" in state["nodes"][0]["message"]


def test_verify_only_uses_existing_output_and_never_invokes_writer(tmp_path):
    if not conversion.CONVERSION_PYTHON.is_file() or not HDF5_PATH.is_file():
        pytest.skip("requires the fixed downloaded HDF5 and conversion environment")
    existing = tmp_path / "explicit-incomplete-existing-export"
    existing.mkdir()
    sentinel = existing / "sentinel.txt"
    sentinel.write_text("must not overwrite an existing output", encoding="utf-8")
    request = conversion_request(HDF5_PATH, tmp_path / "unused-new-exports", verify_only=True, export_path=str(existing))
    runs = tmp_path / "runs"
    run_id = runtime.start_run(request, runs)
    state = runtime.wait_run(run_id, runs, 60)
    assert [n["node_id"] for n in state["nodes"]] == ["file_validation", "official_verify", "output_quality"]
    assert [n["status"] for n in state["nodes"]] == ["completed", "failed", "skipped"]
    assert state["nodes"][1]["diagnostics"]["code"] == "manifest_missing"
    assert not (runs / run_id / "steps/format_write.json").exists()
    assert list(existing.iterdir()) == [sentinel]
    assert sentinel.read_text(encoding="utf-8") == "must not overwrite an existing output"
    assert state["available_path"] is None
