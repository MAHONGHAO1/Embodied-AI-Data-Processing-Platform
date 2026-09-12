"""Synthetic business-state tests; no robot data or live engine is touched."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from robodata_business import BatchStore, ConflictError, WorkflowError


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "store"
        self.store = BatchStore(self.root)
        self.state = self.store.create_batch("明确标注的合成业务测试", {
            "kind": "synthetic_test", "input_fingerprint": "fixture-a", "license": "not_applicable",
        }, [{"episode_index": 0, "row_count": 3}, {"episode_index": 2, "row_count": 4}])
        self.batch_id = self.state["batch_id"]

    def call(self, method, *args, **kwargs):
        self.state = getattr(self.store, method)(self.batch_id, *args,
                                              expected_revision=self.state["revision"], **kwargs)
        return self.state

    def report(self, bad=False):
        return {"input_fingerprint": "fixture-a", "episodes": [
            {"episode_index": index, "row_count": count,
             "status": "issues" if bad and index == 2 else "passed",
             "coverage": [{"rule": "test_rule", "status": "performed"}],
             "issues": [{"message": "合成异常", "row_index": 1}] if bad and index == 2 else []}
            for index, count in [(0, 3), (2, 4)]]}

    def prepare(self, bad=False):
        self.call("apply_quality_report", self.report(bad), run_id="test-quality-run")
        self.call("confirm_cleaning")

    def approve(self, index, outcome="success"):
        self.call("save_annotation", index, task="测试抬升任务", outcome=outcome, tags=["测试"])
        self.call("submit_annotation", index)
        self.call("review_annotation", index, approve=True)

    def verified_output(self):
        spec = self.store.conversion_spec(self.batch_id)
        # Trusted converter stub for contract tests only: no real conversion claimed.
        proof = {"official_loader_passed": True, "numeric_consistency_passed": True,
                 "episode_count": len(spec["episodes"]),
                 "row_count": sum(e["row_count"] for e in spec["episodes"]),
                 "snapshot_id": spec["snapshot_id"], "test_fixture": True}
        self.call("register_verified_output", spec, run_id="synthetic-converter-stub",
                  artifact_path="synthetic-output-not-created", validation=proof)
        return self.state["outputs"][-1]

    def test_unchecked_starts_quarantined_and_balanced(self):
        self.assertEqual(self.state["counts"]["quarantine"], {"episodes": 2, "rows": 7})
        self.assertEqual(self.state["total_rows"], 7)
        with self.assertRaisesRegex(WorkflowError, "质检"):
            self.call("set_disposition", 0, "keep")
        with self.assertRaisesRegex(WorkflowError, "没有可保留"):
            self.call("confirm_cleaning")

    def test_duplicate_input_points_to_original_batch(self):
        with self.assertRaisesRegex(WorkflowError, self.batch_id):
            self.store.create_batch("重复", {"kind": "test", "input_fingerprint": "fixture-a"},
                                    [{"episode_index": 8, "row_count": 1}])
        self.assertEqual(len(self.store.list_batches()), 1)

    def test_invalid_source_and_episode_inventory_rejected(self):
        for episodes in [[], [{"episode_index": -1, "row_count": 1}],
                         [{"episode_index": True, "row_count": 1}],
                         [{"episode_index": 0, "row_count": 0}],
                         [{"episode_index": 0, "row_count": 1}] * 2]:
            with self.subTest(episodes=episodes), self.assertRaises(WorkflowError):
                self.store.create_batch("bad", {"kind": "test", "input_fingerprint": "other"}, episodes)

    def test_quality_report_wrong_input_incomplete_set_and_false_pass_rejected(self):
        wrong_input = self.report()
        wrong_input["input_fingerprint"] = "other"
        missing_episode = self.report()
        missing_episode["episodes"].pop()
        false_pass = self.report()
        false_pass["episodes"][0]["coverage"][0]["status"] = "unavailable"
        wrong_rows = self.report()
        wrong_rows["episodes"][0]["row_count"] = 99
        duplicate = self.report()
        duplicate["episodes"][1] = duplicate["episodes"][0]
        for report in [wrong_input, missing_episode, false_pass, wrong_rows, duplicate]:
            with self.subTest(report=report), self.assertRaises(WorkflowError):
                self.call("apply_quality_report", report, run_id="test")
        self.assertEqual(self.store.get_batch(self.batch_id)["revision"], 1)

    def test_anomaly_isolation_and_clean_selection(self):
        self.prepare(bad=True)
        self.assertEqual(self.state["counts"]["keep"]["rows"], 3)
        self.assertEqual(self.state["counts"]["quarantine"]["rows"], 4)
        with self.assertRaises(WorkflowError):
            self.call("set_disposition", 2, "keep")
        self.call("set_disposition", 2, "exclude", reason="合成数据异常，不进入本次交付")
        self.assertFalse(self.state["cleaning_confirmed"])
        self.assertEqual(sum(c["rows"] for c in self.state["counts"].values()), 7)
        self.assertEqual(sum(c["episodes"] for c in self.state["counts"].values()), 2)

    def test_incomplete_and_load_failed_stay_isolated(self):
        report = self.report()
        report["episodes"][0]["status"] = "incomplete"
        report["episodes"][0]["coverage"][0]["status"] = "unavailable"
        report["episodes"][1]["status"] = "load_failed"
        report["episodes"][1]["row_count"] = 0
        self.call("apply_quality_report", report, run_id="read-failure-test")
        self.assertEqual(self.state["counts"]["quarantine"]["episodes"], 2)

    def test_exclude_or_return_requires_reason(self):
        for disposition in ("exclude", "quarantine"):
            with self.assertRaisesRegex(WorkflowError, "原因"):
                self.call("set_disposition", 0, disposition)
        with self.assertRaisesRegex(WorkflowError, "退回原因"):
            self.call("review_annotation", 0, approve=False)

    def test_annotation_requires_description_and_outcome_before_submit(self):
        self.prepare()
        with self.assertRaisesRegex(WorkflowError, "任务描述"):
            self.call("submit_annotation", 0)
        self.call("save_annotation", 0, task="Lift", outcome="unlabeled")
        with self.assertRaisesRegex(WorkflowError, "执行结果"):
            self.call("submit_annotation", 0)
        self.call("save_annotation", 0, task="Lift", outcome="uncertain")
        self.call("submit_annotation", 0)
        self.assertEqual(self.state["episodes"][0]["review"]["status"], "submitted")

    def test_only_submitted_can_be_reviewed(self):
        self.prepare()
        with self.assertRaisesRegex(WorkflowError, "仅已提交"):
            self.call("review_annotation", 0, approve=True)
        with self.assertRaisesRegex(WorkflowError, "审核决定"):
            self.call("review_annotation", 0, approve="yes")

    def test_return_modify_resubmit_and_audit_preserves_reason(self):
        self.prepare(bad=True)
        self.call("save_annotation", 0, task="待完善", outcome="uncertain")
        self.call("submit_annotation", 0)
        self.call("review_annotation", 0, approve=False, reason="请补充具体任务描述")
        self.approve(0, "failure")
        spec = self.store.conversion_spec(self.batch_id)
        self.assertEqual(len(spec["episodes"]), 1)
        self.assertEqual(spec["episodes"][0]["annotation"]["outcome"], "failure")
        history = [json.loads(line) for line in self.store.audit_jsonl(self.batch_id).splitlines()]
        self.assertTrue(any(event["detail"].get("reason") == "请补充具体任务描述" for event in history))

    def test_unreviewed_kept_task_blocks_conversion_not_silently_dropped(self):
        self.prepare()
        self.approve(0)
        with self.assertRaisesRegex(WorkflowError, "所有保留任务"):
            self.store.conversion_spec(self.batch_id)
        self.approve(2)
        spec = self.store.conversion_spec(self.batch_id)
        self.assertEqual([(e["source_episode_index"], e["output_episode_index"]) for e in spec["episodes"]],
                         [(0, 0), (2, 1)])

    def test_selection_change_invalidates_approval_and_clean_confirmation(self):
        self.prepare()
        self.approve(0)
        self.approve(2)
        self.call("set_disposition", 2, "exclude", reason="本次只交付第一条")
        self.assertFalse(self.state["cleaning_confirmed"])
        self.assertEqual(self.state["episodes"][1]["review"]["status"], "draft")
        with self.assertRaises(WorkflowError):
            self.store.conversion_spec(self.batch_id)
        self.call("confirm_cleaning")
        self.assertEqual(self.state["episodes"][0]["review"]["status"], "draft")
        with self.assertRaises(WorkflowError):
            self.store.conversion_spec(self.batch_id)
        self.call("submit_annotation", 0)
        self.call("review_annotation", 0, approve=True)
        self.assertEqual(len(self.store.conversion_spec(self.batch_id)["episodes"]), 1)

    def test_new_quality_evidence_requires_new_review(self):
        self.prepare(bad=True)
        self.approve(0)
        self.call("apply_quality_report", self.report(bad=True), run_id="new-run")
        self.assertFalse(self.state["cleaning_confirmed"])
        self.assertEqual(self.state["episodes"][0]["review"]["status"], "draft")

    def test_old_browser_revision_cannot_overwrite(self):
        stale = self.state["revision"]
        self.call("save_annotation", 0, task="较新的描述", outcome="success")
        with self.assertRaises(ConflictError):
            self.store.save_annotation(self.batch_id, 0, task="旧页面", outcome="failure", expected_revision=stale)
        self.assertEqual(self.store.get_batch(self.batch_id)["episodes"][0]["annotation"]["task"], "较新的描述")

    def test_concurrent_writers_only_one_revision_wins(self):
        revision = self.state["revision"]
        def save(task):
            try:
                self.store.save_annotation(self.batch_id, 0, task=task, outcome="uncertain", expected_revision=revision)
                return "saved"
            except ConflictError:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(save, ["窗口一", "窗口二"])), ["saved", "conflict"])

    def test_restart_preserves_state_and_read_only_queries_do_not_write(self):
        self.prepare(bad=True)
        self.approve(0)
        before = (self.root / f"{self.batch_id}.json").read_bytes()
        reopened = BatchStore(self.root)
        self.assertEqual(reopened.get_batch(self.batch_id), self.state)
        reopened.list_batches()
        reopened.conversion_spec(self.batch_id)
        reopened.audit_jsonl(self.batch_id)
        self.assertEqual((self.root / f"{self.batch_id}.json").read_bytes(), before)

    def test_atomic_save_failure_keeps_previous_record_and_no_partial_audit(self):
        before = (self.root / f"{self.batch_id}.json").read_bytes()
        with patch("robodata_business.store.os.replace", side_effect=OSError("explicit synthetic disk failure")):
            with self.assertRaises(OSError):
                self.call("save_annotation", 0, task="不会保存", outcome="uncertain")
        self.assertEqual((self.root / f"{self.batch_id}.json").read_bytes(), before)
        self.assertFalse(list(self.root.glob(".batch-*.tmp")))

    def test_path_traversal_rejected(self):
        with self.assertRaises(WorkflowError):
            self.store.get_batch("../outside")

    def test_invalid_verification_not_registered(self):
        self.prepare(bad=True)
        self.approve(0)
        spec = self.store.conversion_spec(self.batch_id)
        for proof in [{}, {"official_loader_passed": True, "numeric_consistency_passed": False},
                      {"official_loader_passed": True, "numeric_consistency_passed": True,
                       "episode_count": 1, "row_count": 999, "snapshot_id": spec["snapshot_id"]}]:
            with self.subTest(proof=proof), self.assertRaises(WorkflowError):
                self.call("register_verified_output", spec, run_id="test", artifact_path="test", validation=proof)
        self.assertFalse(self.store.get_batch(self.batch_id)["outputs"])

    def test_old_converter_snapshot_cannot_be_registered_after_edit(self):
        self.prepare(bad=True)
        self.approve(0)
        old = self.store.conversion_spec(self.batch_id)
        self.approve(0, "uncertain")
        with self.assertRaises(ConflictError):
            self.call("register_verified_output", old, run_id="test", artifact_path="test",
                      validation={"official_loader_passed": True, "numeric_consistency_passed": True,
                                  "snapshot_id": old["snapshot_id"], "episode_count": 1, "row_count": 3})

    def test_registered_delivery_retained_but_stale_after_annotation_change(self):
        self.prepare(bad=True)
        self.approve(0)
        output = self.verified_output()
        spec = self.store.delivery_spec(self.batch_id, output["output_id"])
        self.call("register_delivery", output["output_id"], snapshot_id=spec["snapshot_id"],
                  zip_path="synthetic-archive-not-created.zip", zip_sha256="0" * 64,
                  packaging_run_id="synthetic-packager-stub", archive_verified=True)
        self.assertTrue(self.state["deliveries"][0]["current"])
        self.call("save_annotation", 0, task="修改任务说明", outcome="success")
        self.assertFalse(self.state["outputs"][0]["current"])
        self.assertFalse(self.state["deliveries"][0]["current"])
        self.approve(0)
        with self.assertRaises(ConflictError):
            self.store.delivery_spec(self.batch_id, output["output_id"])

    def test_failed_packaging_cannot_be_registered(self):
        self.prepare(bad=True)
        self.approve(0)
        output = self.verified_output()
        with self.assertRaises(WorkflowError):
            self.call("register_delivery", output["output_id"], snapshot_id=output["snapshot"]["snapshot_id"],
                      zip_path="test.zip", zip_sha256="0" * 64, packaging_run_id="test", archive_verified=False)
        self.assertFalse(self.store.get_batch(self.batch_id)["deliveries"])


if __name__ == "__main__":
    unittest.main()
