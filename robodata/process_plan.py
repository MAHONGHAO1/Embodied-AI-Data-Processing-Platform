"""Run a frozen processing plan without the web UI.

The plan is a reviewable JSON/TOML document: input, annotations, review policy
and whether to package. The runner only calls the same business functions the
UI calls; it does not bypass any gate.

Idempotency is phase-level, not node-level: a rerun reuses the imported batch
and any current output/delivery, but a failed conversion phase restarts that
phase from its beginning. This is stated in docs/PROCESS_PLAN.md.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tomllib

PLAN_SCHEMA_VERSION = 1
ALLOWED_OUTCOMES = {"success", "failure", "uncertain"}
DEFAULT_TIMEOUT_SECONDS = 6 * 60 * 60

EXIT_OK = 0
EXIT_REJECTED = 2
EXIT_UNSUPPORTED = 3
EXIT_INVALID = 4
EXIT_FAILED = 5


class PlanError(ValueError):
    """The plan itself is malformed; nothing was started."""


def _text(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanError(f"{label} 必须是非空字符串")
    return value.strip()


def _string_list(value, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise PlanError(f"{label} 必须是非空字符串列表")
    return sorted({item.strip() for item in value})


def _outcome(value, label: str) -> str:
    if value not in ALLOWED_OUTCOMES:
        raise PlanError(f"{label} 必须是 {sorted(ALLOWED_OUTCOMES)} 之一")
    return value


def _annotation(item: dict, *, label: str, require_task: bool = True) -> dict:
    if not isinstance(item, dict):
        raise PlanError(f"{label} 必须是对象")
    annotation = {"outcome": _outcome(item.get("outcome"), f"{label}.outcome"),
                  "tags": _string_list(item.get("tags"), f"{label}.tags"),
                  "notes": str(item.get("notes") or "").strip()}
    if item.get("task") is not None:
        annotation["task"] = _text(item["task"], f"{label}.task")
    elif require_task:
        raise PlanError(f"{label}.task 必须是非空字符串")
    if "episode_index" in item:
        index = item["episode_index"]
        if type(index) is not int or index < 0:
            raise PlanError(f"{label}.episode_index 必须是非负整数")
        annotation["episode_index"] = index
    return annotation


def _normalize_annotations(value) -> dict:
    if isinstance(value, list):
        if not value:
            raise PlanError("annotations 不能为空列表")
        return {"mode": "list", "items": [_annotation(item, label=f"annotations[{index}]")
                                          for index, item in enumerate(value)]}
    if not isinstance(value, dict):
        raise PlanError("annotations 必须是列表或对象")
    template = _text(value.get("task_template"), "annotations.task_template")
    outcome = _outcome(value.get("default_outcome"), "annotations.default_outcome")
    overrides = value.get("overrides") or []
    if not isinstance(overrides, list):
        raise PlanError("annotations.overrides 必须是列表")
    normalized = []
    seen = set()
    for index, item in enumerate(overrides):
        annotation = _annotation({**item, "outcome": item.get("outcome", outcome),
                                  "tags": item.get("tags", value.get("tags"))},
                                 label=f"annotations.overrides[{index}]", require_task=False)
        if "episode_index" not in annotation:
            raise PlanError("annotations.overrides 的每一项都必须有 episode_index")
        if annotation["episode_index"] in seen:
            raise PlanError(f"annotations.overrides 重复 episode_index={annotation['episode_index']}")
        seen.add(annotation["episode_index"])
        normalized.append(annotation)
    return {"mode": "template", "task_template": template, "default_outcome": outcome,
            "tags": _string_list(value.get("tags"), "annotations.tags"), "overrides": normalized}


def normalize_plan(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise PlanError("计划根节点必须是对象")
    if raw.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise PlanError(f"schema_version 必须是 {PLAN_SCHEMA_VERSION}")
    source = raw.get("input") or {}
    if not isinstance(source, dict):
        raise PlanError("input 必须是对象")
    review = raw.get("review") or {}
    delivery = raw.get("delivery") or {}
    resume = raw.get("resume") or {}
    for label, value in (("review", review), ("delivery", delivery), ("resume", resume)):
        if not isinstance(value, dict):
            raise PlanError(f"{label} 必须是对象")
    timeout = raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise PlanError("timeout_seconds 必须是正数")
    batch_id = resume.get("batch_id")
    if batch_id is not None:
        batch_id = _text(batch_id, "resume.batch_id")
    source_kind = str(source.get("source") or "auto").strip()
    if not source_kind:
        raise PlanError("input.source 不能为空")
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "input": {"path": _text(source.get("path"), "input.path"),
                  "label": _text(source.get("label"), "input.label"),
                  "source": source_kind,
                  "test_label": (str(source["test_label"]).strip()
                                 if source.get("test_label") is not None else None)},
        "annotations": _normalize_annotations(raw.get("annotations")),
        "review": {"approve": bool(review.get("approve", True))},
        "delivery": {"package": bool(delivery.get("package", True)),
                     "dataset_id": (str(delivery.get("dataset_id")).strip()
                                    if delivery.get("dataset_id") else None),
                     "version": (str(delivery.get("version")).strip()
                                 if delivery.get("version") else None)},
        "resume": {"batch_id": batch_id},
        "timeout_seconds": float(timeout),
    }


def load_plan(path: Path) -> dict:
    path = Path(path)
    if not path.is_file():
        raise PlanError(f"找不到计划文件：{path}")
    try:
        if path.suffix.lower() == ".toml":
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        else:
            raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PlanError(f"计划文件无法解析：{exc}") from exc
    return normalize_plan(raw)


def _format_task(template: str, episode: dict, batch: dict) -> str:
    source_id = (batch.get("source", {}).get("episode_mapping", {}) or {}).get(str(episode["episode_index"]),
                                                                               episode["episode_index"])
    values = {"episode_index": episode["episode_index"], "source_episode_id": source_id,
              "row_count": episode["row_count"]}
    try:
        task = template.format_map(values)
    except KeyError as exc:
        raise PlanError(f"task_template 占位符未知：{exc.args[0]}；可用 "
                        "{episode_index}、{source_episode_id}、{row_count}") from exc
    return _text(task, "task_template 渲染结果")


def build_annotations(plan: dict, batch: dict, kept: list[dict]) -> dict[int, dict]:
    """Resolve the plan into exactly one annotation per kept episode."""
    if not kept:
        raise PlanError("没有可标注的 keep Episode")
    spec = plan["annotations"]
    result: dict[int, dict] = {}
    if spec["mode"] == "list":
        items = spec["items"]
        if any("episode_index" in item for item in items):
            if not all("episode_index" in item for item in items):
                raise PlanError("annotations 列表不能混用有/无 episode_index 的条目")
            result = {item["episode_index"]: item for item in items}
        else:
            if len(items) != len(kept):
                raise PlanError(f"annotations 有 {len(items)} 条，但 keep Episode 有 {len(kept)} 条")
            result = {episode["episode_index"]: item for episode, item in zip(kept, items)}
    else:
        for episode in kept:
            result[episode["episode_index"]] = {
                "task": _format_task(spec["task_template"], episode, batch),
                "outcome": spec["default_outcome"], "tags": spec["tags"], "notes": "",
                "episode_index": episode["episode_index"]}
        for override in spec["overrides"]:
            index = override["episode_index"]
            if index not in result:
                raise PlanError(f"annotations.overrides 指向不存在的 keep Episode：{index}")
            current = result[index]
            result[index] = {**current, **{key: value for key, value in override.items() if key != "episode_index"}}
    expected = {episode["episode_index"] for episode in kept}
    missing = sorted(expected - set(result))
    extra = sorted(set(result) - expected)
    if missing or extra:
        raise PlanError(f"annotations 覆盖不完整：缺少 {missing}，多余 {extra}")
    for index, annotation in result.items():
        if not annotation["task"].strip():
            raise PlanError(f"Episode {index} 的任务描述为空")
    return result


def plan_digest(plan: dict) -> str:
    payload = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _state_path(plan: dict, business_root) -> Path:
    from . import business
    root = business.get_store(business_root).root / "process"
    return root / f"{plan_digest(plan)[:16]}.json"


def _read_state(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(path: Path, value: dict) -> None:
    from .runtime import _atomic_json
    _atomic_json(path, value)


def _current_output(batch: dict) -> dict | None:
    outputs = [item for item in batch.get("outputs", []) if item.get("current")]
    return outputs[-1] if outputs else None


def _current_delivery(batch: dict, output_id: str) -> dict | None:
    deliveries = [item for item in batch.get("deliveries", [])
                  if item.get("current") and item.get("output_id") == output_id]
    return deliveries[-1] if deliveries else None


def _run_state(run_id: str, runs_root, timeout: float) -> dict:
    from .runtime import wait_run
    return wait_run(run_id, runs_root=runs_root, timeout=timeout)


def _resolve_source(plan: dict) -> str:
    source = plan["input"]["source"]
    if source != "auto":
        from .adapters import get_adapter
        return get_adapter(source).descriptor.adapter_id
    from .adapters import best
    return best(Path(plan["input"]["path"]), test_label=plan["input"]["test_label"])["adapter_id"]


def _input_bytes(path: str) -> int:
    from pathlib import Path
    import re
    if not path or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", path):
        return 0
    candidate = Path(path)
    if candidate.is_file():
        return candidate.stat().st_size
    if candidate.is_dir():
        total = 0
        for item in candidate.rglob("*"):
            try:
                if item.is_file() and not item.is_symlink():
                    total += item.stat().st_size
            except OSError:
                continue
        return total
    return 0


def _disk_preflight(plan: dict, business_root=None, runs_root=None) -> str | None:
    import os
    import shutil
    from pathlib import Path
    if os.environ.get("ROBODATA_SKIP_DISK_CHECK") == "1":
        return None
    source = _input_bytes(plan["input"]["path"])
    required = max(512 * 1024 ** 2, int(source * 3) + 256 * 1024 ** 2)
    probe = Path(runs_root) if runs_root else (Path(business_root) if business_root else Path.cwd())
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return None
    if free < required:
        return (f"磁盘空间不足：预计至少需要 {required / 1024 ** 3:.2f} GiB，"
                f"当前可用 {free / 1024 ** 3:.2f} GiB；清理或更换 --runs-root 后重试")
    return None


def _result(*, status: str, exit_code: int, batch=None, runs=None, output=None, delivery=None,
            message: str = "", state_path: Path | None = None) -> dict:
    return {"schema_version": 1, "status": status, "exit_code": exit_code,
            "message": message,
            "batch_id": batch.get("batch_id") if batch else None,
            "revision": batch.get("revision") if batch else None,
            "source_kind": batch.get("source", {}).get("kind") if batch else None,
            "episodes": len(batch.get("episodes", [])) if batch else 0,
            "frames": sum(int(item.get("row_count", 0)) for item in batch.get("episodes", [])) if batch else 0,
            "runs": runs or {}, "output": output, "delivery": delivery,
            "state_path": str(state_path) if state_path else None}


def run_process(plan: dict, *, runs_root=None, business_root=None, timeout: float | None = None,
                dry_run: bool = False) -> dict:
    from . import business
    from robodata_business import ConflictError, WorkflowError

    timeout = float(timeout or plan["timeout_seconds"])
    state_path = _state_path(plan, business_root)
    store = business.get_store(business_root)
    persisted = _read_state(state_path)
    batch = None
    if plan["resume"]["batch_id"]:
        try:
            batch = store.get_batch(plan["resume"]["batch_id"])
        except Exception as exc:
            return _result(status="rejected", exit_code=EXIT_INVALID,
                           message=f"resume.batch_id 不存在：{exc}", state_path=state_path)
    elif persisted.get("batch_id"):
        try:
            candidate = store.get_batch(persisted["batch_id"])
            if candidate.get("label") == plan["input"]["label"]:
                batch = candidate
        except Exception:
            batch = None
    source_kind = batch["source"]["kind"] if batch is not None else _resolve_source(plan)
    requested = plan["input"]["source"]
    if requested != "auto" and source_kind != requested:
        return _result(status="rejected", exit_code=EXIT_INVALID, batch=batch,
                       message=f"批次来源是 {source_kind}，计划要求 {requested}", state_path=state_path)
    if dry_run:
        return _result(status="validated", exit_code=EXIT_OK, message=f"计划有效；输入适配器：{source_kind}",
                       state_path=state_path)
    disk_error = _disk_preflight(plan, business_root, runs_root)
    if disk_error:
        return _result(status="failed", exit_code=EXIT_FAILED, message=disk_error,
                       state_path=state_path)

    runs: dict[str, str] = dict(persisted.get("runs") or {})
    if batch is None:
        try:
            run_id = business.start_import(Path(plan["input"]["path"]), source_kind, plan["input"]["label"],
                                           plan["input"]["test_label"], runs_root=runs_root,
                                           business_root=business_root)
        except WorkflowError as exc:
            return _result(status="rejected", exit_code=EXIT_INVALID, message=str(exc), state_path=state_path)
        runs["import"] = run_id
        state = _run_state(run_id, runs_root, timeout)
        if state.get("status") != "completed":
            return _result(status="failed", exit_code=EXIT_FAILED, runs=runs,
                           message=state.get("message") or "导入运行未完成", state_path=state_path)
        if state.get("outcome") == "rejected":
            batch = store.get_batch(state["batch_id"]) if state.get("batch_id") else None
            return _result(status="rejected", exit_code=EXIT_REJECTED, batch=batch, runs=runs,
                           message=state.get("message") or "导入质检未通过", state_path=state_path)
        batch = store.get_batch(state["batch_id"])
        _write_state(state_path, {"schema_version": 1, "plan_digest": plan_digest(plan),
                                  "batch_id": batch["batch_id"], "runs": runs})
    if batch.get("source", {}).get("kind") != source_kind:
        return _result(status="rejected", exit_code=EXIT_INVALID, batch=batch, runs=runs,
                       message=f"批次来源是 {batch.get('source', {}).get('kind')}，计划要求 {source_kind}",
                       state_path=state_path)

    try:
        if not batch.get("cleaning_confirmed"):
            batch = store.confirm_cleaning(batch["batch_id"], expected_revision=batch["revision"])
        kept = [item for item in batch["episodes"] if item.get("disposition") == "keep"]
        if not kept:
            return _result(status="rejected", exit_code=EXIT_REJECTED, batch=batch, runs=runs,
                           message="没有 keep Episode，无法进入标注", state_path=state_path)
        if not all(item.get("review", {}).get("status") == "approved" for item in kept):
            annotations = build_annotations(plan, batch, kept)
            for episode in kept:
                index = episode["episode_index"]
                if episode.get("review", {}).get("status") == "approved":
                    continue
                if episode.get("review", {}).get("status") in {"draft", "returned"}:
                    annotation = annotations[index]
                    batch = store.save_annotation(batch["batch_id"], index, task=annotation["task"],
                                                  outcome=annotation["outcome"], tags=annotation["tags"],
                                                  notes=annotation["notes"], expected_revision=batch["revision"])
                    batch = store.submit_annotation(batch["batch_id"], index, expected_revision=batch["revision"])
                if not plan["review"]["approve"]:
                    continue
                batch = store.review_annotation(batch["batch_id"], index, approve=True,
                                                expected_revision=batch["revision"])
        _write_state(state_path, {"schema_version": 1, "plan_digest": plan_digest(plan),
                                  "batch_id": batch["batch_id"], "runs": runs,
                                  "review": "approved" if plan["review"]["approve"] else "submitted"})
        if not plan["review"]["approve"]:
            return _result(status="annotated", exit_code=EXIT_OK, batch=batch, runs=runs,
                           message="标注已提交，等待人工审核", state_path=state_path)

        output = _current_output(batch)
        delivery = _current_delivery(batch, output["output_id"]) if output else None
        if output and (not plan["delivery"]["package"] or delivery):
            result = _result(status="passed", exit_code=EXIT_OK, batch=batch, runs=runs,
                             output=output, delivery=delivery,
                             message="复用已完成的转换/交付产物", state_path=state_path)
            return result

        if plan["delivery"]["package"]:
            options = {}
            if plan["delivery"]["dataset_id"]:
                options["dataset_id"] = plan["delivery"]["dataset_id"]
            if plan["delivery"]["version"]:
                options["dataset_version"] = plan["delivery"]["version"]
            run_id = business.start_final_quality(batch["batch_id"], batch["revision"],
                                                 auto_continue=True, runs_root=runs_root,
                                                 business_root=business_root, **options)
            runs["final_quality"] = run_id
        else:
            run_id = business.start_batch_conversion(batch["batch_id"], batch["revision"],
                                                    runs_root=runs_root, business_root=business_root)
            runs["conversion"] = run_id
        _write_state(state_path, {"schema_version": 1, "plan_digest": plan_digest(plan),
                                  "batch_id": batch["batch_id"], "runs": runs})
        state = _run_state(run_id, runs_root, timeout)
        batch = store.get_batch(batch["batch_id"])
        if state.get("status") != "completed":
            return _result(status="failed", exit_code=EXIT_FAILED, batch=batch, runs=runs,
                           message=state.get("message") or "最终处理未完成", state_path=state_path)
        if state.get("outcome") == "rejected":
            return _result(status="rejected", exit_code=EXIT_REJECTED, batch=batch, runs=runs,
                           message=state.get("message") or "最终质检未通过", state_path=state_path)
        output = _current_output(batch)
        if output is None:
            return _result(status="failed", exit_code=EXIT_FAILED, batch=batch, runs=runs,
                           message="运行完成但没有当前转换产物", state_path=state_path)
        delivery = _current_delivery(batch, output["output_id"]) if plan["delivery"]["package"] else None
        if plan["delivery"]["package"] and delivery is None:
            return _result(status="failed", exit_code=EXIT_FAILED, batch=batch, runs=runs,
                           output=output, message="运行完成但没有当前交付 ZIP", state_path=state_path)
        _write_state(state_path, {"schema_version": 1, "plan_digest": plan_digest(plan),
                                  "batch_id": batch["batch_id"], "runs": runs,
                                  "output_id": output["output_id"],
                                  "delivery_id": delivery.get("delivery_id") if delivery else None})
        return _result(status="passed", exit_code=EXIT_OK, batch=batch, runs=runs,
                       output=output, delivery=delivery,
                       message="导入、标注、转换、验证与交付全部通过", state_path=state_path)
    except (WorkflowError, ConflictError) as exc:
        return _result(status="rejected", exit_code=EXIT_REJECTED, batch=batch, runs=runs,
                       message=str(exc), state_path=state_path)
