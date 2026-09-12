"""Real supervised final checks on explicitly synthetic tiny robot fixtures."""
from pathlib import Path
import json
import shutil

import pandas as pd
import pytest

from robodata import business, runtime
from robodata.config import CAMERA
from robodata_business import ConflictError, WorkflowError
from test_runtime import tiny_dataset


@pytest.fixture
def flow_batch(tmp_path, tiny_dataset):
    for index in range(2, 5):
        table = pd.read_parquet(tiny_dataset / "data/chunk-000/episode_000000.parquet")
        table["episode_index"] = index
        table.to_parquet(tiny_dataset / f"data/chunk-000/episode_{index:06}.parquet")
        shutil.copyfile(tiny_dataset / f"videos/chunk-000/{CAMERA}/episode_000000.mp4",
                        tiny_dataset / f"videos/chunk-000/{CAMERA}/episode_{index:06}.mp4")
    (tiny_dataset / "meta/episodes.jsonl").write_text("\n".join(json.dumps({"episode_index": i, "length": 5, "tasks": ["synthetic test"]}) for i in range(5)), encoding="utf-8")
    root, runs = tmp_path / "business", tmp_path / "runs"
    run_id = business.start_import(tiny_dataset, "so100", "明确的合成流程测试", test_label="synthetic tiny flow", business_root=root, runs_root=runs)
    state = runtime.wait_run(run_id, runs, timeout=45)
    assert state["status"] == "completed", state["message"]
    store = business.get_store(root)
    batch = store.get_batch(state["batch_id"])
    assert all(e["quality"]["status"] == "passed" and e["quality"]["run_id"] == run_id for e in batch["episodes"])
    return store, batch, runs, tiny_dataset


def approve(store, batch):
    # Noncontiguous selection; a valid failure demonstration remains eligible.
    for index in (1, 3, 4):
        batch = store.set_disposition(batch["batch_id"], index, "exclude", reason="explicit fixture selection", expected_revision=batch["revision"])
    batch = store.confirm_cleaning(batch["batch_id"], expected_revision=batch["revision"])
    for index in (0, 2):
        batch = store.save_annotation(batch["batch_id"], index, task="synthetic lift", outcome="failure" if index == 0 else "uncertain", tags=["人工复核"], expected_revision=batch["revision"])
        batch = store.submit_annotation(batch["batch_id"], index, expected_revision=batch["revision"])
        batch = store.review_annotation(batch["batch_id"], index, approve=True, expected_revision=batch["revision"])
    return batch


def test_final_rejection_is_completed_and_no_conversion_started(flow_batch):
    store, batch, runs, _ = flow_batch
    run_id = business.start_final_quality(batch["batch_id"], batch["revision"], business_root=store.root, runs_root=runs)
    state = runtime.wait_run(run_id, runs, timeout=45)
    assert state["status"] == "completed", state["message"]
    assert state["outcome"] == "rejected"
    assert next(n for n in state["nodes"] if n["node_id"] == "final_quality")["status"] == "completed"
    evidence = business.get_final_quality(batch["batch_id"], store.root)
    assert evidence["current"] and not evidence["accepted"]
    report = json.loads(Path(evidence["report_path"]).read_text(encoding="utf-8"))
    assert {i["rule"] for i in report["issues"]} >= {"task_text", "outcome", "review_approved"}
    assert Path(evidence["report_path"]).with_suffix(".html").is_file()
    assert store.get_batch(batch["batch_id"])["revision"] == batch["revision"]
    assert not store.get_batch(batch["batch_id"])["outputs"]


def test_final_pass_preserves_approved_failure_and_invalidates_after_edit(flow_batch):
    store, batch, runs, _ = flow_batch
    batch = approve(store, batch)
    run_id = business.start_final_quality(batch["batch_id"], batch["revision"], auto_continue=False, business_root=store.root, runs_root=runs)
    state = runtime.wait_run(run_id, runs, timeout=45)
    assert state["status"] == "completed", state["message"]
    evidence = business.get_final_quality(batch["batch_id"], store.root)
    assert evidence["accepted"] and evidence["current"]
    assert store.get_batch(batch["batch_id"])["revision"] == batch["revision"]
    assert [e["review"]["status"] for e in store.get_batch(batch["batch_id"])["episodes"] if e["disposition"] == "keep"] == ["approved", "approved"]
    report = json.loads(Path(evidence["report_path"]).read_text(encoding="utf-8"))
    assert report["summary"]["episode_count"] == 2 and report["summary"]["row_count"] == 10
    assert report["rule_version"] == business.FINAL_RULE_VERSION
    request = runtime.get_run(run_id, runs)["request"]
    assert business._require_final_quality(runs / run_id, request)["accepted"]
    store.save_annotation(batch["batch_id"], 0, task="new description", outcome="failure", expected_revision=batch["revision"])
    assert not business.get_final_quality(batch["batch_id"], store.root)["current"]
    with pytest.raises(ConflictError):
        business._register_final_quality(runs / run_id, request)


def test_final_evidence_hash_prevents_changed_report_gate(flow_batch):
    store, batch, runs, _ = flow_batch
    batch = approve(store, batch)
    run_id = business.start_final_quality(batch["batch_id"], batch["revision"], auto_continue=False, business_root=store.root, runs_root=runs)
    state = runtime.wait_run(run_id, runs, timeout=45)
    assert state["status"] == "completed", state["message"]
    evidence = business.get_final_quality(batch["batch_id"], store.root)
    Path(evidence["report_path"]).write_text("{}", encoding="utf-8")
    assert not business.get_final_quality(batch["batch_id"], store.root)["current"]
    with pytest.raises(WorkflowError):
        business._require_final_quality(runs / run_id, state["request"])


def test_duplicate_import_keeps_approval_and_original_quality(flow_batch):
    store, batch, runs, source = flow_batch
    batch = approve(store, batch)
    # 批次名参与输入指纹：同名重复导入复用同一批次，已有审核与质检必须保留。
    run_id = business.start_import(source, "so100", batch["label"], test_label="synthetic tiny flow", business_root=store.root, runs_root=runs)
    state = runtime.wait_run(run_id, runs, timeout=45)
    assert state["status"] == "completed", state["message"]
    assert state["batch_id"] == batch["batch_id"]
    assert store.get_batch(batch["batch_id"])["revision"] == batch["revision"]
    assert next(n for n in state["nodes"] if n["node_id"] == "quality")["status"] == "skipped"
    assert len(store.list_batches()) == 1


def test_duplicate_import_with_new_label_builds_independent_batch(flow_batch):
    """异名导入建立独立批次，既有批次的审核与质检不受影响。"""
    store, batch, runs, source = flow_batch
    batch = approve(store, batch)
    run_id = business.start_import(source, "so100", "duplicate test", test_label="synthetic tiny flow", business_root=store.root, runs_root=runs)
    state = runtime.wait_run(run_id, runs, timeout=45)
    assert state["status"] == "completed", state["message"]
    assert state["batch_id"] != batch["batch_id"]
    assert len(store.list_batches()) == 2
    original = store.get_batch(batch["batch_id"])
    assert original["revision"] == batch["revision"]
    assert [e["review"]["status"] for e in original["episodes"] if e["disposition"] == "keep"] == ["approved", "approved"]
    assert not original["outputs"]
