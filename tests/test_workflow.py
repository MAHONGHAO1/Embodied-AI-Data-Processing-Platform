"""Pure-file workflow projection tests; no robot conversion or default lock."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from robodata_business import BatchStore, ConflictError, WorkflowError
from robodata.importing import _digest
from robodata.workflow import get_flow, download_artifact, read_flow_events, events_jsonl


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "business"
        self.runs = Path(self.tmp.name) / "runs"
        self.runs.mkdir()
        self.store = BatchStore(self.root)
        self.raw = self.root / "managed/fixture/source/test.hdf5"
        self.raw.parent.mkdir(parents=True)
        self.raw.write_bytes(b"explicit synthetic workflow evidence; not real HDF5")
        files = [{"path": "test.hdf5", "exists": True, "size": self.raw.stat().st_size,
                  "sha256": hashlib.sha256(self.raw.read_bytes()).hexdigest()}]
        manifest = {"files": files, "interpretation": {"synthetic_test": True}}
        self.manifest = self.raw.parent.parent / "input_manifest.json"
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        self.batch = self.store.create_batch("合成流程契约测试", {
            "kind": "hdf5", "input_fingerprint": _digest(manifest), "managed_path": str(self.raw.parent.parent),
            "manifest_path": str(self.manifest), "required_rules": ["fixture"], "import_run_id": "20260911T000001_abcdefabcdef"},
            [{"episode_index": 0, "row_count": 3}, {"episode_index": 2, "row_count": 4}])

    def call(self, method, *args, **kwargs):
        self.batch = getattr(self.store, method)(self.batch["batch_id"], *args,
                                                 expected_revision=self.batch["revision"], **kwargs)
        return self.batch

    def flow(self):
        return get_flow(self.batch, runs_root=self.runs, business_root=self.root)

    def nodes(self):
        return {n["node_id"]: n for n in self.flow()["nodes"]}

    def quality(self, bad=False):
        report = {"input_fingerprint": self.batch["source"]["input_fingerprint"], "episodes": [
            {"episode_index": e["episode_index"], "row_count": e["row_count"],
             "status": "issues" if bad else "passed", "coverage": [{"rule": "fixture", "status": "performed"}],
             "issues": [{"message": "合成数值问题"}] if bad else []} for e in self.batch["episodes"]]}
        self.call("apply_quality_report", report, run_id="synthetic-quality")

    def approve(self, index):
        self.call("save_annotation", index, task="测试抬升", outcome="failure")
        self.call("submit_annotation", index)
        self.call("review_annotation", index, approve=True)

    def prepare(self):
        self.quality()
        self.call("confirm_cleaning")
        self.approve(0)
        self.approve(2)

    def test_all_eight_nodes_visible_and_no_fictional_progress(self):
        nodes = self.nodes()
        self.assertEqual(list(nodes), ["import", "clean", "select", "annotate", "review", "quality", "convert", "deliver"])
        self.assertEqual(nodes["import"]["status"], "completed")
        self.assertEqual(nodes["clean"]["status"], "ready")
        self.assertEqual(nodes["deliver"]["status"], "waiting_upstream")
        self.assertIsNone(nodes["deliver"]["progress"]["total"])
        self.assertFalse(nodes["deliver"]["outputs"])

    def test_refresh_materializes_immutable_snapshots_without_business_audit(self):
        first = self.nodes()
        record = self.root / f"{self.batch['batch_id']}.json"
        before = record.read_bytes()
        ref = first["annotate"]["intermediates"][0]
        path = Path(ref["path"])
        timestamp = path.stat().st_mtime_ns
        second = self.nodes()
        self.assertEqual(before, record.read_bytes())
        self.assertEqual(timestamp, path.stat().st_mtime_ns)
        self.assertEqual(ref, second["annotate"]["intermediates"][0])
        value = json.loads(download_artifact(self.batch["batch_id"], ref, business_root=self.root))
        self.assertIn("非完整数据集", value["artifact_kind"])
        self.assertEqual(value["source_mapping"][1]["source_episode_index"], 2)

    def test_anomalies_complete_rejected_and_block_downstream(self):
        self.quality(bad=True)
        nodes = self.nodes()
        self.assertEqual((nodes["clean"]["status"], nodes["clean"]["outcome"]), ("completed", "rejected"))
        self.assertEqual(nodes["select"]["status"], "waiting_upstream")
        self.assertEqual(nodes["quality"]["status"], "waiting_upstream")

    def test_review_return_is_rejected_not_execution_failure(self):
        self.quality()
        self.call("confirm_cleaning")
        self.call("save_annotation", 0, task="测试", outcome="uncertain")
        self.call("submit_annotation", 0)
        self.call("review_annotation", 0, approve=False, reason="补充动作阶段")
        nodes = self.nodes()
        self.assertEqual((nodes["review"]["status"], nodes["review"]["outcome"]), ("completed", "rejected"))
        self.assertEqual(nodes["annotate"]["status"], "waiting_human")
        self.assertEqual(nodes["quality"]["status"], "waiting_upstream")

    def test_robot_failure_annotation_can_reach_final_quality_ready(self):
        self.prepare()
        nodes = self.nodes()
        self.assertEqual(nodes["review"]["outcome"], "passed")
        self.assertEqual(nodes["quality"]["status"], "ready")
        ref = nodes["annotate"]["outputs"][0]
        self.assertEqual(json.loads(download_artifact(self.batch["batch_id"], ref, business_root=self.root))["episodes"][0]["annotation"]["outcome"], "failure")

    def test_reselect_invalidates_all_approvals_and_stale_download(self):
        self.prepare()
        previous = self.nodes()["annotate"]["outputs"][0]
        old_path = Path(previous["path"])
        old_bytes = old_path.read_bytes()
        self.call("set_disposition", 2, "exclude", reason="本轮选择任务 0")
        self.call("confirm_cleaning")
        self.assertEqual(self.batch["episodes"][0]["review"]["status"], "draft")
        self.assertEqual(self.batch["episodes"][0]["annotation"]["task"], "测试抬升")
        with self.assertRaises(WorkflowError):
            self.store.conversion_spec(self.batch["batch_id"])
        self.assertEqual(self.nodes()["review"]["status"], "waiting_upstream")
        with self.assertRaises(ConflictError):
            download_artifact(self.batch["batch_id"], previous, business_root=self.root)
        self.assertEqual(old_path.read_bytes(), old_bytes)

    def test_tampered_snapshot_is_preserved_and_download_rejected(self):
        ref = self.nodes()["select"]["intermediates"][0]
        Path(ref["path"]).write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "发生变化"):
            download_artifact(self.batch["batch_id"], ref, business_root=self.root)
        latest = self.nodes()["select"]
        self.assertEqual(latest["status"], "failed")
        self.assertEqual(Path(ref["path"]).read_text(), "tampered")

    def test_source_changed_invalidates_import_without_repair(self):
        self.raw.write_bytes(b"changed original")
        nodes = self.nodes()
        self.assertEqual(nodes["import"]["status"], "failed")
        self.assertFalse(nodes["import"]["outputs"])
        self.assertEqual(nodes["select"]["status"], "waiting_upstream")

    def test_current_final_rejection_before_approval_is_not_mislabeled_stale(self):
        report = self.runs / "final-quality.json"
        report.write_text('{"outcome":"rejected","reason":"尚未审核"}', encoding="utf-8")
        evidence = {"current": True, "accepted": False, "content_version": self.batch["content_version"],
                    "run_id": "synthetic-final", "report_path": str(report),
                    "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest()}
        with patch("robodata.business.get_final_quality", return_value=evidence):
            nodes = self.nodes()
        self.assertEqual((nodes["quality"]["status"], nodes["quality"]["outcome"]), ("completed", "rejected"))
        ref = nodes["quality"]["outputs"][0]
        self.assertEqual(download_artifact(self.batch["batch_id"], ref, business_root=self.root, runs_root=self.runs), report.read_bytes())
        self.assertEqual(nodes["convert"]["status"], "waiting_upstream")

    def test_changed_registered_basic_report_invalidates_downstream(self):
        self.prepare()
        directory = self.runs / "synthetic-quality"
        (directory / "reports").mkdir(parents=True)
        report = directory / "reports/quality_report.json"
        report.write_text('{"synthetic":true}', encoding="utf-8")
        digest = hashlib.sha256(report.read_bytes()).hexdigest()
        (directory / "registration.json").write_text(json.dumps({"evidence_sha256": {"reports/quality_report.json": digest}}))
        (directory / "state.json").write_text(json.dumps({"run_id": "synthetic-quality", "status": "completed",
            "request": {"operation": "quality", "batch_id": self.batch["batch_id"], "content_version": 1},
            "nodes": [{"node_id": "quality", "status": "completed"}], "report_path": str(report)}))
        self.assertEqual(self.nodes()["clean"]["status"], "completed")
        report.write_text('{"synthetic":"tampered"}', encoding="utf-8")
        nodes = self.nodes()
        self.assertEqual(nodes["clean"]["status"], "failed")
        self.assertEqual(nodes["review"]["status"], "superseded")
        self.assertFalse(next(a for a in nodes["clean"]["outputs"] if a["role"] == "基础核验 JSON")["valid"])

    def test_real_runtime_timeout_diagnostic_and_checkpoint_are_projected(self):
        run_id = "20260911T000002_123456abcdef"
        directory = self.runs / run_id
        (directory / "steps").mkdir(parents=True)
        checkpoint = directory / "steps/quality.json"
        checkpoint.write_text(json.dumps({"stage": "quality", "status": "failed"}))
        state = {"run_id": run_id, "status": "failed", "request": {"batch_id": self.batch["batch_id"], "operation": "quality", "content_version": 1},
                 "nodes": [{"node_id": "quality", "status": "timed_out", "message": "人工故障等待已被监督器终止", "diagnostics": {"test_fixture": True}}]}
        (directory / "state.json").write_text(json.dumps(state), encoding="utf-8")
        # A failed pre-registration run still belongs to the current source version.
        node = self.nodes()["clean"]
        self.assertEqual(node["status"], "timed_out")
        self.assertEqual(node["intermediates"][-1]["sha256"], hashlib.sha256(checkpoint.read_bytes()).hexdigest())
        self.assertTrue(node["diagnostics"]["test_fixture"])

    def test_logs_filter_actual_audit_operation_and_runtime(self):
        self.quality()
        self.call("confirm_cleaning")
        self.call("save_annotation", 2, task="测试", outcome="uncertain")
        events = read_flow_events(self.batch, self.runs, node_id="annotate", episode_index=2)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"], "business_audit")
        self.assertEqual(len(events_jsonl(self.batch, self.runs, node_id="annotate").splitlines()), 1)
        self.assertFalse(read_flow_events(self.batch, self.runs, level="ERROR"))

    def test_one_run_can_register_conversion_and_delivery_idempotently(self):
        self.prepare()
        spec = self.store.conversion_spec(self.batch["batch_id"])
        proof = {"official_loader_passed": True, "numeric_consistency_passed": True,
                 "episode_count": 2, "row_count": 7, "snapshot_id": spec["snapshot_id"]}
        self.call("register_verified_output", spec, run_id="shared-real-run", artifact_path="explicit synthetic output stub", validation=proof)
        output = self.batch["outputs"][-1]
        self.call("register_delivery", output["output_id"], snapshot_id=spec["snapshot_id"], zip_path="explicit synthetic archive stub",
                  zip_sha256="a" * 64, packaging_run_id="shared-real-run", archive_verified=True)
        revision = self.batch["revision"]
        # Trusted-adapter contract stubs only; this test claims no real compatibility.
        self.call("register_verified_output", spec, run_id="shared-real-run", artifact_path="explicit synthetic output stub", validation=proof)
        self.assertEqual(self.batch["revision"], revision)
        self.assertEqual(len(self.batch["outputs"]), 1)
        self.assertEqual(len(self.batch["deliveries"]), 1)

    def test_old_run_id_keys_are_read_without_overwriting_prior_history(self):
        self.quality()
        path = self.root / f"{self.batch['batch_id']}.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        prior = saved["applied_runs"].pop("apply_quality_report:synthetic-quality")
        saved["applied_runs"]["synthetic-quality"] = prior
        path.write_text(json.dumps(saved), encoding="utf-8")
        revision = self.batch["revision"]
        self.quality()
        self.assertEqual(self.batch["revision"], revision)


if __name__ == "__main__":
    unittest.main()
