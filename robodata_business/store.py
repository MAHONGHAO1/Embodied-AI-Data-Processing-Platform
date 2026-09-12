"""Local single-user batch, selection, annotation and review state.

This package does not import robot readers, run checks, convert, or publish data.
All writes are confined to the explicit store directory supplied by its caller.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid


class WorkflowError(ValueError):
    """A user-facing workflow constraint, expressed in Chinese."""


class ConflictError(WorkflowError):
    """An older browser view attempted to overwrite newer state."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(f"{label}不能为空")
    return value.strip()


def _positive_int(value, label):
    if type(value) is not int or value < 1:
        raise WorkflowError(f"{label}必须为正整数")
    return value


def _index(value):
    if type(value) is not int or value < 0:
        raise WorkflowError("任务编号必须为不小于零的整数")
    return value


class BatchStore:
    """Atomic batch snapshots with audit history and optimistic revision checks.

    The store directory must be dedicated to business metadata. Original source
    paths are never opened or modified by this class. No default path is supplied.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, batch_id):
        if not isinstance(batch_id, str) or not re.fullmatch(r"batch_[0-9a-f]{32}", batch_id):
            raise WorkflowError("批次编号无效")
        path = self.root / f"{batch_id}.json"
        if path.is_symlink():
            raise WorkflowError("业务记录不能使用符号链接")
        return path

    @contextmanager
    def _lock(self):
        lock_path = self.root / ".write.lock"
        if lock_path.is_symlink():
            raise WorkflowError("锁文件不能使用符号链接")
        with lock_path.open("a+b") as handle:
            if handle.seek(0, 2) == 0:
                handle.write(b"0")
                handle.flush()
            deadline = time.monotonic() + 5
            while True:
                handle.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise ConflictError("另一项操作正在保存，请稍后刷新重试") from exc
                    time.sleep(0.01)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read(self, batch_id):
        try:
            for attempt in range(20):
                try:
                    result = json.loads(self._path(batch_id).read_text(encoding="utf-8"))
                    break
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.025)
        except FileNotFoundError as exc:
            raise WorkflowError("批次不存在") from exc
        except (ValueError, UnicodeError) as exc:
            raise WorkflowError("批次记录损坏，不能继续覆盖；请保留文件排查") from exc
        if not isinstance(result, dict) or result.get("schema_version") != 1 or result.get("batch_id") != batch_id:
            raise WorkflowError("批次记录版本或编号不一致")
        return result

    def _write(self, state):
        destination = self._path(state["batch_id"])
        encoded = json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=".batch-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            for attempt in range(20):
                try:
                    os.replace(temporary, destination)
                    break
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.025)
        finally:
            Path(temporary).unlink(missing_ok=True)

    @staticmethod
    def _episode(state, episode_index):
        _index(episode_index)
        for episode in state["episodes"]:
            if episode["episode_index"] == episode_index:
                return episode
        raise WorkflowError("此批次中不存在该任务")

    @staticmethod
    def _audit(state, operation, detail):
        state["history"].append({"revision": state["revision"], "at_utc": _now(),
                                 "operation": operation, "detail": detail,
                                 "actor": "local_single_user"})

    def _mutate(self, batch_id, expected_revision, operation, change, *, content=True, idempotency=None):
        with self._lock():
            state = self._read(batch_id)
            if idempotency is not None:
                key = f"{operation}:{idempotency['run_id']}"
                prior = state.get("applied_runs", {}).get(key)
                # Old records keyed by run_id remain readable. A single
                # supervised run may now register several distinct operations.
                legacy = state.get("applied_runs", {}).get(idempotency["run_id"])
                if prior is None and legacy and legacy.get("operation") == operation:
                    prior = legacy
                if prior is not None:
                    if prior != {"operation": operation, **idempotency}:
                        raise WorkflowError("同一运行编号不能登记不同的业务快照")
                    return self.get_batch(batch_id)
            if type(expected_revision) is not int or expected_revision != state["revision"]:
                raise ConflictError("页面内容已过期，请刷新后再保存，避免覆盖新修改")
            detail = change(state)
            state["revision"] += 1
            if content:
                state["content_version"] += 1
            state["updated_at_utc"] = _now()
            self._audit(state, operation, detail)
            if idempotency is not None:
                state.setdefault("applied_runs", {})[key] = {"operation": operation, **idempotency}
            self._write(state)
        return self.get_batch(batch_id)

    def create_batch(self, label, source, episodes, *, run_id=None):
        """Register metadata only, after an importer has stored and inventoried files.

        source requires kind and input_fingerprint. Each episode requires an
        integer episode_index and positive row_count. All tasks start isolated.
        """
        label = _text(label, "批次名称")
        if not isinstance(source, dict):
            raise WorkflowError("来源说明必须是对象")
        source = json.loads(json.dumps(source, ensure_ascii=False, allow_nan=False))
        source["kind"] = _text(source.get("kind"), "来源类型")
        source["input_fingerprint"] = _text(source.get("input_fingerprint"), "输入指纹")
        if not isinstance(episodes, list) or not episodes:
            raise WorkflowError("批次必须包含任务")
        items, seen = [], set()
        for episode in episodes:
            if not isinstance(episode, dict):
                raise WorkflowError("任务说明必须是对象")
            index = _index(episode.get("episode_index"))
            if index in seen:
                raise WorkflowError("同一批次的任务编号不能重复")
            seen.add(index)
            items.append({"episode_index": index,
                          "row_count": _positive_int(episode.get("row_count"), "记录数"),
                          "quality": {"status": "not_checked", "run_id": None},
                          "disposition": "quarantine", "selection_reason": "尚未检查",
                          "annotation": {"task": "", "outcome": "unlabeled", "tags": [], "notes": ""},
                          "review": {"status": "draft", "reason": ""}})
        with self._lock():
            fingerprint = source["input_fingerprint"]
            for path in self.root.glob("batch_*.json"):
                try:
                    existing = self._read(path.stem)
                    existing_fingerprint = existing["source"]["input_fingerprint"]
                except (WorkflowError, KeyError, TypeError, OSError):
                    continue  # A damaged independent batch must not block this import.
                if existing_fingerprint == fingerprint:
                    if run_id is not None:
                        return self.get_batch(existing["batch_id"])
                    raise WorkflowError(f"相同输入已登记，请打开已有批次：{existing['batch_id']}")
            batch_id = f"batch_{uuid.uuid4().hex}"
            state = {"schema_version": 1, "batch_id": batch_id, "label": label,
                     "source": source, "episodes": sorted(items, key=lambda x: x["episode_index"]),
                     "revision": 1, "content_version": 1, "created_at_utc": _now(),
                     "updated_at_utc": _now(), "cleaning_confirmed": False,
                     "outputs": [], "deliveries": [], "history": []}
            self._audit(state, "create_batch", {"episode_count": len(items), "run_id": run_id})
            self._write(state)
        return self.get_batch(batch_id)

    def get_batch(self, batch_id):
        state = self._read(batch_id)
        counts = {name: {"episodes": 0, "rows": 0} for name in ("keep", "quarantine", "exclude")}
        for episode in state["episodes"]:
            counts[episode["disposition"]]["episodes"] += 1
            counts[episode["disposition"]]["rows"] += episode["row_count"]
        state["counts"] = counts
        state["total_episodes"] = len(state["episodes"])
        state["total_rows"] = sum(e["row_count"] for e in state["episodes"])
        for output in state["outputs"]:
            output["current"] = output["content_version"] == state["content_version"]
        for delivery in state["deliveries"]:
            delivery["current"] = delivery["content_version"] == state["content_version"]
        return state

    def list_batches(self):
        return self.safe_list_batches()["batches"]

    def safe_list_batches(self):
        batches, errors = [], []
        for path in sorted(self.root.glob("batch_*.json")):
            try:
                batches.append(self.get_batch(path.stem))
            except (WorkflowError, OSError, KeyError, TypeError, ValueError) as exc:
                errors.append({"batch_id": path.stem, "path": str(path), "message": "此批次记录损坏或无法读取，其他批次仍可使用",
                               "diagnostics": {"exception_type": type(exc).__name__, "original_message": str(exc)}})
        return {"batches": batches, "errors": errors}

    def apply_quality_report(self, batch_id, report, *, run_id, expected_revision, snapshot_id=None):
        """Apply a complete report for this batch's exact input fingerprint."""
        run_id = _text(run_id, "运行编号")
        def change(state):
            if report.get("input_fingerprint") != state["source"]["input_fingerprint"]:
                raise WorkflowError("质检报告的输入指纹与批次不一致")
            results = report.get("episodes")
            if not isinstance(results, list) or any(not isinstance(r, dict) for r in results):
                raise WorkflowError("质检报告缺少任务结果")
            indices = [r.get("episode_index") for r in results]
            if any(type(i) is not int for i in indices) or len(set(indices)) != len(indices):
                raise WorkflowError("质检报告的任务编号无效或重复")
            if set(indices) != {e["episode_index"] for e in state["episodes"]}:
                raise WorkflowError("质检报告必须完整对应当前批次的任务")
            for result in results:
                episode = self._episode(state, result["episode_index"])
                status = result.get("status")
                if status not in ("passed", "issues", "incomplete", "load_failed"):
                    raise WorkflowError("无法识别质检结论")
                coverage, issues = result.get("coverage"), result.get("issues")
                if not isinstance(coverage, list) or not isinstance(issues, list):
                    raise WorkflowError("质检报告缺少检查覆盖或问题明细")
                if status == "passed" and (not coverage or issues or any(
                        not isinstance(c, dict) or c.get("status") != "performed" for c in coverage)):
                    raise WorkflowError("检查未完整执行或仍有问题，不能标为通过")
                if status != "load_failed" and result.get("row_count") != episode["row_count"]:
                    raise WorkflowError("质检报告记录数与导入清单不一致")
                episode["quality"] = {"status": status, "run_id": run_id,
                                      "issue_count": len(issues), "coverage": coverage, "issues": issues}
                # New evidence must not silently preserve earlier approval.
                episode["disposition"] = "keep" if status == "passed" else "quarantine"
                episode["selection_reason"] = "自动检查通过，建议保留" if status == "passed" else "自动检查发现问题或未完整执行"
                episode["review"] = {"status": "draft", "reason": "质检结果更新，请重新提交审核"}
            state["cleaning_confirmed"] = False
            return {"run_id": run_id, "episode_count": len(results)}
        return self._mutate(batch_id, expected_revision, "apply_quality_report", change,
                            idempotency={"run_id": run_id, "snapshot_id": snapshot_id or _digest(report)})

    def set_disposition(self, batch_id, episode_index, disposition, *, reason="", expected_revision):
        if disposition not in ("keep", "quarantine", "exclude"):
            raise WorkflowError("请选择保留、隔离或排除")
        if disposition != "keep":
            reason = _text(reason, "隔离或排除原因")
        def change(state):
            episode = self._episode(state, episode_index)
            if disposition == "keep" and episode["quality"]["status"] != "passed":
                raise WorkflowError("任务未通过完整质检，不能进入正式候选集")
            before = {"disposition": episode["disposition"], "reason": episode["selection_reason"]}
            episode["disposition"], episode["selection_reason"] = disposition, reason
            episode["review"] = {"status": "draft", "reason": "清洗选择变更，请重新提交审核"}
            # Selection defines the downstream dataset version. Keep existing
            # annotation text as a draft, but never reuse any old approval.
            invalidated = []
            for selected in state["episodes"]:
                if selected["disposition"] == "keep":
                    selected["review"] = {"status": "draft", "reason": "候选集版本变化，请重新确认标注并提交审核"}
                    invalidated.append(selected["episode_index"])
            state["cleaning_confirmed"] = False
            return {"episode_index": episode_index, "before": before,
                    "after": {"disposition": disposition, "reason": reason},
                    "invalidated_review_episode_indices": invalidated}
        return self._mutate(batch_id, expected_revision, "set_disposition", change)

    def confirm_cleaning(self, batch_id, *, expected_revision):
        def change(state):
            kept = [e for e in state["episodes"] if e["disposition"] == "keep"]
            if not kept:
                raise WorkflowError("没有可保留的任务，不能确认可交付候选集")
            if any(e["quality"]["status"] != "passed" for e in kept):
                raise WorkflowError("候选集包含未通过质检的任务")
            state["cleaning_confirmed"] = True
            return {"kept_episode_indices": [e["episode_index"] for e in kept]}
        return self._mutate(batch_id, expected_revision, "confirm_cleaning", change)

    def save_annotation(self, batch_id, episode_index, *, task, outcome, tags=None, notes="", expected_revision):
        if outcome not in ("unlabeled", "success", "failure", "uncertain"):
            raise WorkflowError("任务结果必须为未标注、成功、失败或不确定")
        if not isinstance(task, str) or not isinstance(notes, str):
            raise WorkflowError("任务描述与备注必须是文本")
        tags = [] if tags is None else tags
        if not isinstance(tags, list) or any(not isinstance(t, str) or not t.strip() for t in tags):
            raise WorkflowError("问题标签必须是非空文本列表")
        annotation = {"task": task.strip(), "outcome": outcome,
                      "tags": sorted(set(t.strip() for t in tags)), "notes": notes.strip()}
        def change(state):
            episode = self._episode(state, episode_index)
            before = episode["annotation"]
            episode["annotation"] = annotation
            episode["review"] = {"status": "draft", "reason": "标注已更新，请重新提交审核"}
            return {"episode_index": episode_index, "before": before, "after": annotation}
        return self._mutate(batch_id, expected_revision, "save_annotation", change)

    def submit_annotation(self, batch_id, episode_index, *, expected_revision):
        def change(state):
            episode = self._episode(state, episode_index)
            if not state["cleaning_confirmed"] or episode["disposition"] != "keep":
                raise WorkflowError("请先确认清洗结果，并选择保留的任务")
            _text(episode["annotation"]["task"], "任务描述")
            if episode["annotation"]["outcome"] == "unlabeled":
                raise WorkflowError("请选择任务执行结果，无法判断时可选不确定")
            if episode["review"]["status"] not in ("draft", "returned"):
                raise WorkflowError("当前任务已提交或已审核，请修改标注后再提交")
            episode["review"] = {"status": "submitted", "reason": "", "submitted_at_utc": _now()}
            return {"episode_index": episode_index}
        return self._mutate(batch_id, expected_revision, "submit_annotation", change)

    def review_annotation(self, batch_id, episode_index, *, approve, reason="", expected_revision):
        if type(approve) is not bool:
            raise WorkflowError("审核决定必须为通过或退回")
        if not approve:
            reason = _text(reason, "退回原因")
        def change(state):
            episode = self._episode(state, episode_index)
            if not state["cleaning_confirmed"] or episode["disposition"] != "keep":
                raise WorkflowError("清洗结果发生变化，请重新确认候选集")
            if episode["review"]["status"] != "submitted":
                raise WorkflowError("仅已提交的标注可以审核")
            episode["review"] = {"status": "approved" if approve else "returned",
                                  "reason": reason, "reviewed_at_utc": _now()}
            return {"episode_index": episode_index, "decision": episode["review"]["status"], "reason": reason}
        return self._mutate(batch_id, expected_revision, "review_annotation", change)

    @staticmethod
    def _conversion_spec(state):
        kept = [e for e in state["episodes"] if e["disposition"] == "keep"]
        if not state["cleaning_confirmed"] or not kept:
            raise WorkflowError("请先确认非空的清洗候选集")
        if any(e["quality"]["status"] != "passed" or e["review"]["status"] != "approved" for e in kept):
            raise WorkflowError("所有保留任务必须通过质检和人工审核，不能静默漏掉未审核任务")
        spec = {"batch_id": state["batch_id"], "content_version": state["content_version"],
                "input_fingerprint": state["source"]["input_fingerprint"],
                "episodes": [{"source_episode_index": e["episode_index"], "output_episode_index": i,
                              "row_count": e["row_count"], "annotation": e["annotation"],
                              "quality_run_id": e["quality"]["run_id"]} for i, e in enumerate(kept)]}
        spec["snapshot_id"] = _digest(spec)
        return spec

    def conversion_spec(self, batch_id):
        """Return a frozen selection contract; performs no conversion or writes."""
        return self._conversion_spec(self._read(batch_id))

    def register_verified_output(self, batch_id, snapshot, *, run_id, artifact_path,
                                 validation, expected_revision):
        """Record a trusted adapter's verification result, not a verification itself.

        Main-thread integration must call this only after actual official loader
        and numeric checks. Never expose raw validation flags as user form inputs.
        """
        run_id = _text(run_id, "转换运行编号")
        artifact_path = _text(artifact_path, "转换产物路径")
        if not isinstance(validation, dict) or validation.get("official_loader_passed") is not True or validation.get("numeric_consistency_passed") is not True:
            raise WorkflowError("转换产物尚未通过官方加载与数值一致性验证")
        def change(state):
            expected = self._conversion_spec(state)
            if snapshot != expected:
                raise ConflictError("转换期间批次、标注或审核已变化，请使用新快照重新转换")
            count = sum(e["row_count"] for e in expected["episodes"])
            if (validation.get("episode_count") != len(expected["episodes"])
                    or validation.get("row_count") != count
                    or validation.get("snapshot_id") != expected["snapshot_id"]):
                raise WorkflowError("验证结果的快照或数据数量与候选集不一致")
            output = {"output_id": f"output_{uuid.uuid4().hex}", "content_version": state["content_version"],
                      "snapshot": expected, "run_id": run_id, "artifact_path": artifact_path,
                      "validation": validation, "registered_at_utc": _now()}
            state["outputs"].append(output)
            return {"output_id": output["output_id"], "run_id": run_id}
        return self._mutate(batch_id, expected_revision, "register_verified_output", change, content=False,
                            idempotency={"run_id": run_id, "snapshot_id": snapshot["snapshot_id"]})

    @staticmethod
    def _delivery_spec(state, output_id):
        output = next((o for o in state["outputs"] if o["output_id"] == output_id), None)
        if output is None:
            raise WorkflowError("找不到该转换产物")
        current = BatchStore._conversion_spec(state)
        if output["snapshot"] != current:
            raise ConflictError("转换产物已过期，请重新转换并验证")
        return {"batch_id": state["batch_id"], "output_id": output_id,
                "content_version": state["content_version"], "snapshot_id": current["snapshot_id"],
                "source": state["source"], "episodes": current["episodes"],
                "artifact_path": output["artifact_path"], "conversion_run_id": output["run_id"]}

    def delivery_spec(self, batch_id, output_id):
        """Return a packaging input contract. Does not generate or validate a ZIP."""
        return self._delivery_spec(self._read(batch_id), output_id)

    def register_delivery(self, batch_id, output_id, *, snapshot_id, zip_path, zip_sha256,
                          packaging_run_id, archive_verified, expected_revision):
        zip_path = _text(zip_path, "交付包路径")
        packaging_run_id = _text(packaging_run_id, "打包运行编号")
        if not isinstance(zip_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", zip_sha256):
            raise WorkflowError("交付包必须记录有效的 SHA-256")
        if archive_verified is not True:
            raise WorkflowError("交付包尚未通过文件清单、校验值及可读取性验证")
        def change(state):
            spec = self._delivery_spec(state, output_id)
            if snapshot_id != spec["snapshot_id"]:
                raise ConflictError("打包快照已过期，不能登记为当前交付")
            delivery = {"delivery_id": f"delivery_{uuid.uuid4().hex}", **spec,
                        "zip_path": zip_path, "zip_sha256": zip_sha256,
                        "packaging_run_id": packaging_run_id, "registered_at_utc": _now()}
            state["deliveries"].append(delivery)
            return {"delivery_id": delivery["delivery_id"], "output_id": output_id}
        return self._mutate(batch_id, expected_revision, "register_delivery", change, content=False,
                            idempotency={"run_id": packaging_run_id, "snapshot_id": snapshot_id})

    def audit_jsonl(self, batch_id):
        """Derive JSONL from the atomic history; no dual-write transaction risk."""
        return "".join(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n"
                       for event in self._read(batch_id)["history"])
