"""Evidence binding and interruption recovery, with explicitly synthetic records."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from robodata import business, importing, runtime
from robodata_business import ConflictError, WorkflowError
from test_runtime import tiny_dataset


@pytest.fixture
def pending_quality(tmp_path):
    root = tmp_path / "business"
    managed = root / "managed" / "synthetic-input"
    raw = managed / "source"
    raw.mkdir(parents=True)
    (raw / "explicit-fixture.txt").write_text("synthetic input, not robot data", encoding="utf-8")
    manifest = {"files": importing._files(raw, ["explicit-fixture.txt"]), "interpretation": {"profile": "synthetic"}}
    runtime._atomic_json(managed / "manifest.json", manifest)
    source = {"kind": "so100", "managed_path": str(managed), "manifest_path": str(managed / "manifest.json"),
              "required_rules": ["required_fields", "numeric_finite"], "data_root": str(raw)}
    source["input_fingerprint"] = importing.input_fingerprint(source)
    store = business.get_store(root)
    batch = store.create_batch("明确标注的回写测试", source, [{"episode_index": 2, "row_count": 5}])
    request = business._batch_request("quality", batch["batch_id"], batch["revision"], root)
    request.update(snapshot_id="synthetic-snapshot", rule_version="synthetic-rules")
    report = {"run_id": "test-quality-run", "rule_version": "synthetic-rules", "input_fingerprint": source["input_fingerprint"],
              "episodes": [{"episode_index": 2, "row_count": 5, "status": "passed", "issues": [],
                            "coverage": [{"rule": r, "status": "performed"} for r in source["required_rules"]]}]}
    run = tmp_path / "runs" / report["run_id"]
    run.mkdir(parents=True)
    runtime._atomic_json(run / "request.json", request)
    runtime._atomic_json(run / "reports/quality_report.json", report)
    runtime._atomic_json(run / "steps/quality.json", {"stage": "quality", "status": "passed", "details": {"report_path": str(run / "reports/quality_report.json")}})
    runtime._atomic_json(run / "registration.json", {"status": "pending", "operation": "quality", "run_id": run.name,
                        "batch_id": batch["batch_id"], "evidence_sha256": business._evidence_hashes(run, "quality")})
    runtime._atomic_json(run / "state.json", {"run_id": run.name, "request": request, "status": "interrupted",
                        "started_at": runtime.utc_now(), "nodes": [], "artifacts": {}})
    return store, batch, request, report, run


@pytest.mark.parametrize("change", ["missing_rule", "duplicate_rule", "wrong_run", "wrong_version", "wrong_input", "false_pass"])
def test_quality_adapter_rejects_unbound_or_incomplete_evidence(pending_quality, change):
    store, batch, request, report, run = pending_quality
    report = deepcopy(report)
    if change == "missing_rule": report["episodes"][0]["coverage"].pop()
    elif change == "duplicate_rule": report["episodes"][0]["coverage"].append(report["episodes"][0]["coverage"][0])
    elif change == "wrong_run": report["run_id"] = "another-run"
    elif change == "wrong_version": report["rule_version"] = "older-rules"
    elif change == "wrong_input": report["input_fingerprint"] = "another-input"
    else: report["episodes"][0]["coverage"][0]["status"] = "unavailable"
    with pytest.raises(WorkflowError):
        business.validate_quality_report(batch, report, request, run.name)
    assert store.get_batch(batch["batch_id"])["revision"] == 1


def test_pending_registration_recovers_once_after_process_exit(pending_quality):
    store, batch, request, report, run = pending_quality
    result = business.reconcile_run(run.name, run.parent)
    assert result["status"] == "registered"
    after = store.get_batch(batch["batch_id"])
    assert after["revision"] == 2 and after["episodes"][0]["quality"]["run_id"] == run.name
    assert business.reconcile_run(run.name, run.parent) == result
    assert store.get_batch(batch["batch_id"])["revision"] == 2
    assert runtime.get_run(run.name, run.parent)["status"] == "completed"


def test_callback_repeated_after_store_commit_is_idempotent(pending_quality):
    store, batch, request, report, run = pending_quality
    first = business._register(run)
    # Simulate successful atomic business write followed by crash before the
    # registration marker reached stable storage.
    first["status"] = "pending"
    runtime._atomic_json(run / "registration.json", first)
    assert business.reconcile_run(run.name, run.parent)["status"] == "registered"
    assert store.get_batch(batch["batch_id"])["revision"] == 2


def test_stale_pending_result_is_retained_without_overwriting_edit(pending_quality):
    store, batch, request, report, run = pending_quality
    store.save_annotation(batch["batch_id"], 2, task="newer local edit", outcome="uncertain", expected_revision=1)
    result = business.reconcile_run(run.name, run.parent)
    assert result["status"] == "stale"
    assert store.get_batch(batch["batch_id"])["episodes"][0]["annotation"]["task"] == "newer local edit"
    assert (run / "reports/quality_report.json").is_file()


@pytest.mark.parametrize("target", ["report", "source"])
def test_modified_evidence_or_source_cannot_be_registered(pending_quality, target):
    store, batch, request, report, run = pending_quality
    if target == "report":
        report["episodes"][0]["row_count"] = 99
        runtime._atomic_json(run / "reports/quality_report.json", report)
    else:
        (Path(batch["source"]["managed_path"]) / "source/explicit-fixture.txt").write_text("modified synthetic input", encoding="utf-8")
    result = business.reconcile_run(run.name, run.parent)
    assert result["status"] == "failed" and ("证据" if target == "report" else "已变化") in result["message"]
    assert store.get_batch(batch["batch_id"])["revision"] == 1


def test_damaged_batch_does_not_block_other_batches(pending_quality):
    store, batch, *_ = pending_quality
    (store.root / ("batch_" + "f" * 32 + ".json")).write_text("{broken", encoding="utf-8")
    listed = business.safe_list_batches(store.root)
    assert [b["batch_id"] for b in listed["batches"]] == [batch["batch_id"]]
    assert len(listed["errors"]) == 1


def test_missing_import_has_supervised_run_and_local_diagnostic(tmp_path):
    run_id = business.start_import(tmp_path / "does-not-exist.hdf5", "hdf5", "明确的缺文件测试",
                                   runs_root=tmp_path / "runs", business_root=tmp_path / "business")
    state = runtime.wait_run(run_id, tmp_path / "runs", timeout=30)
    assert state["status"] == "failed"
    assert state["nodes"][0]["node_id"] == "copy" and state["nodes"][0]["status"] == "failed"
    assert state["nodes"][0]["diagnostics"]["traceback"]
    assert business.safe_list_batches(tmp_path / "business")["batches"] == []
    assert any(e["level"] == "ERROR" for e in runtime.read_events(run_id, tmp_path / "runs"))


def test_real_background_business_run_rejects_edit_and_shares_quality_lock(tmp_path, tiny_dataset):
    # SO-100's registered v2 profile always inventories tasks 0 through 4.
    import shutil
    import pandas as pd
    from robodata.config import CAMERA
    for index in range(2, 5):
        table = pd.read_parquet(tiny_dataset / "data/chunk-000/episode_000000.parquet")
        table["episode_index"] = index
        table.to_parquet(tiny_dataset / f"data/chunk-000/episode_{index:06}.parquet")
        shutil.copyfile(tiny_dataset / f"videos/chunk-000/{CAMERA}/episode_000000.mp4",
                        tiny_dataset / f"videos/chunk-000/{CAMERA}/episode_{index:06}.mp4")
    (tiny_dataset / "meta/episodes.jsonl").write_text("\n".join(json.dumps({"episode_index": i, "length": 5, "tasks": ["synthetic test"]}) for i in range(5)), encoding="utf-8")
    runs, root = tmp_path / "business-runs", tmp_path / "business-state"
    imported_id = business.start_import(tiny_dataset, "so100", "明确合成数据的后台集成测试",
                                        test_label="synthetic tiny video fixture", runs_root=runs, business_root=root)
    imported = runtime.wait_run(imported_id, runs, timeout=30)
    assert imported["status"] == "completed", imported["message"]
    store = business.get_store(root)
    batch = store.get_batch(imported["batch_id"])
    quality_id = business.start_batch_quality(batch["batch_id"], batch["revision"], runs_root=runs, business_root=root)
    # The existing quality endpoint cannot bypass the business run's lock.
    with pytest.raises(RuntimeError, match="已有处理运行"):
        runtime.start_run({"data_root": str(tiny_dataset)}, runs)
    store.save_annotation(batch["batch_id"], 0, task="后台处理期间保存的新描述", outcome="uncertain", expected_revision=batch["revision"])
    state = runtime.wait_run(quality_id, runs, timeout=30)
    assert state["status"] == "failed"
    assert state["business_registration"]["status"] == "stale"
    assert state["nodes"][0]["status"] == "completed"
    assert Path(state["report_path"]).is_file()
    current = store.get_batch(batch["batch_id"])
    assert current["episodes"][0]["annotation"]["task"] == "后台处理期间保存的新描述"
    assert current["episodes"][0]["quality"]["status"] == "passed"
    assert current["episodes"][0]["quality"]["run_id"] == imported_id
