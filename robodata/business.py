"""Trusted adapters connecting local business snapshots to supervised real workers.

The store only records decisions; this module obtains evidence from actual files,
official loader subprocesses and complete quality reports before registering them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

from robodata_business import BatchStore, ConflictError, WorkflowError
from .config import PROJECT_ROOT, RULE_VERSION

BUSINESS_STAGES = {
    "import": ("copy", "preview", "inventory", "import_registration", "quality", "registration"),
    "quality": ("quality", "registration"),
    "conversion": ("source_verify", "final_quality", "final_registration", "file_validation", "hdf5_validation", "field_mapping", "format_write", "official_verify", "output_quality", "registration"),
    "conversion_so100": ("source_verify", "final_quality", "final_registration", "file_validation", "metadata_upgrade", "field_mapping", "format_write", "official_verify", "output_quality", "registration"),
    "final_quality": ("source_verify", "final_quality", "final_registration", "file_validation", "hdf5_validation", "field_mapping", "format_write", "official_verify", "output_quality", "conversion_registration", "package", "verify", "archive_verify", "registration"),
    "final_quality_so100": ("source_verify", "final_quality", "final_registration", "file_validation", "metadata_upgrade", "field_mapping", "format_write", "official_verify", "output_quality", "conversion_registration", "package", "verify", "archive_verify", "registration"),
    "delivery": ("source_verify", "package", "verify", "archive_verify", "registration"),
    "verify_delivery": ("verify", "archive_verify"),
}
BUSINESS_LABELS = {"copy": "复制与校验输入", "preview": "原始数据预览解析", "inventory": "登记前核对清单",
                   "import_registration": "登记导入批次", "final_quality": "最终数据与标注质检", "final_registration": "最终质检证据登记", "conversion_registration": "转换产物登记",
                   "quality": "批次基础质检", "source_verify": "管理副本完整性复核",
                   "file_validation": "文件校验", "hdf5_validation": "HDF5 结构与数值检查",
                   "metadata_upgrade": "SO-100 元数据升级（v2.0 → v2.1）",
                   "field_mapping": "审核候选集字段映射", "format_write": "官方格式写入",
                   "official_verify": "官方加载与全量数值验证", "output_quality": "输出质检",
                   "package": "生成交付 ZIP", "verify": "独立解包与清单校验",
                   "archive_verify": "交付包官方离线加载", "registration": "结果回写与业务版本核验"}
BUSINESS_TIMEOUTS = {"preview": 120, "quality": 300, "format_write": 240,
                     "metadata_upgrade": 240, "final_quality": 300,
                     "official_verify": 120, "output_quality": 120, "archive_verify": 120,
                     "package": 120, "verify": 120}
from .conversion import CONVERSION_TOOL_ROOT  # noqa: E402  必须在 SO100_WORKER 之前
SO100_WORKER = CONVERSION_TOOL_ROOT / "so100_worker.py"


def _so100_worker_path() -> Path:
    return SO100_WORKER


def get_store(root=None) -> BatchStore:
    return BatchStore(root or os.environ.get("ROBODATA_BUSINESS_ROOT") or PROJECT_ROOT / "work" / "business")


def safe_list_batches(root=None) -> dict:
    return get_store(root).safe_list_batches()


FINAL_RULE_VERSION = "final-quality-0.4.0"
FINAL_RULES = ("candidate_nonempty", "selection_confirmed", "basic_coverage", "basic_result",
               "task_text", "outcome", "tags", "review_approved", "mapping_count")


def business_stages(operation, request=None):
    if operation == "conversion" and request and request.get("source_kind") == "so100":
        stages = BUSINESS_STAGES["conversion_so100"]
    elif operation == "final_quality" and request and request.get("source_kind") == "so100":
        stages = BUSINESS_STAGES["final_quality_so100"]
    else:
        stages = BUSINESS_STAGES[operation]
    if operation in {"conversion", "final_quality"} and request and not request.get("auto_continue", True):
        return stages[:3]
    return stages


def business_nodes(operation, request=None):
    from .runtime import _node
    return [{**_node(stage), "label": BUSINESS_LABELS[stage]} for stage in business_stages(operation, request)]


def _snapshot_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _base(operation, business_root=None, **options):
    return {"kind": "business", "operation": operation, "business_root": str(get_store(business_root).root),
            "timeouts": {**BUSINESS_TIMEOUTS, **options.pop("timeouts", {})}, **options}


def start_import(path, source_kind, label, test_label=None, *, runs_root=None, business_root=None, **options):
    from .runtime import start_run
    if source_kind not in {"hdf5", "so100"}:
        raise WorkflowError("本版本仅支持固定 HDF5 和 SO-100 配置")
    if not isinstance(label, str) or not label.strip():
        raise WorkflowError("请填写批次名称")
    if test_label is not None and (not isinstance(test_label, str) or not test_label.strip()):
        raise WorkflowError("故障副本必须填写明确的测试标签")
    return start_run(_base("import", business_root, input_path=str(Path(path).expanduser().resolve()),
                           source_kind=source_kind, label=label.strip(), test_label=test_label, **options), runs_root)


def _batch_request(operation, batch_id, expected_revision, business_root=None, **options):
    store = get_store(business_root)
    batch = store.get_batch(batch_id)
    if type(expected_revision) is not int or batch["revision"] != expected_revision:
        raise ConflictError("页面内容已过期，请刷新后再开始处理")
    return _base(operation, store.root, batch_id=batch_id, expected_revision=expected_revision,
                 content_version=batch["content_version"], source=batch["source"],
                 source_kind=batch["source"]["kind"],
                 input_fingerprint=batch["source"]["input_fingerprint"],
                 inventory=[{"episode_index": e["episode_index"], "row_count": e["row_count"]} for e in batch["episodes"]],
                 **options)


def start_batch_quality(batch_id, expected_revision, *, runs_root=None, business_root=None, **options):
    from .runtime import start_run
    request = _batch_request("quality", batch_id, expected_revision, business_root, **options)
    request["rule_version"] = "hdf5-preview-0.4.0" if request["source_kind"] == "hdf5" else RULE_VERSION
    request["snapshot_id"] = _snapshot_digest({k: request[k] for k in ("batch_id", "content_version", "input_fingerprint", "inventory", "rule_version")})
    return start_run(request, runs_root)


def start_batch_conversion(batch_id, expected_revision, *, runs_root=None, business_root=None, **options):
    from .runtime import start_run
    request = _batch_request("conversion", batch_id, expected_revision, business_root, **options)
    if request["source_kind"] not in {"hdf5", "so100"}:
        raise WorkflowError("当前审核候选集转换仅支持固定 HDF5 与 SO-100 配置")
    request["snapshot"] = get_store(business_root).conversion_spec(batch_id)
    request["batch_snapshot"] = get_store(business_root).get_batch(batch_id)
    if request["source_kind"] == "hdf5":
        request["input_path"] = request["source"]["hdf5_path"]
    else:
        request["input_path"] = request["source"]["data_root"]
    return start_run(request, runs_root)


def _candidate_snapshot(batch):
    kept = [e for e in batch["episodes"] if e["disposition"] == "keep"]
    snapshot = {"batch_id": batch["batch_id"], "content_version": batch["content_version"],
                "input_fingerprint": batch["source"]["input_fingerprint"],
                "episodes": [{"source_episode_index": e["episode_index"], "output_episode_index": i,
                              "row_count": e["row_count"], "annotation": e["annotation"],
                              "quality_run_id": e["quality"].get("run_id")} for i, e in enumerate(kept)]}
    snapshot["snapshot_id"] = _snapshot_digest(snapshot)
    return snapshot


def start_final_quality(batch_id, expected_revision, *, auto_continue=True, runs_root=None, business_root=None, **options):
    from .runtime import start_run
    request = _batch_request("final_quality", batch_id, expected_revision, business_root,
                             auto_continue=bool(auto_continue), **options)
    batch = get_store(business_root).get_batch(batch_id)
    request.update(snapshot=_candidate_snapshot(batch), batch_snapshot=batch)
    if request["source_kind"] == "hdf5":
        request["input_path"] = request["source"]["hdf5_path"]
    else:
        # SO-100 与 HDF5 共用同一条最终质检与转换链路。
        request["input_path"] = request["source"]["data_root"]
    return start_run(request, runs_root)


def start_delivery(batch_id, output_id, expected_revision, *, runs_root=None, business_root=None, **options):
    from .runtime import start_run
    request = _batch_request("delivery", batch_id, expected_revision, business_root, **options)
    request["snapshot"] = get_store(business_root).delivery_spec(batch_id, output_id)
    return start_run(request, runs_root)


def start_verify_delivery(archive_path, *, runs_root=None, business_root=None, **options):
    from .runtime import start_run
    return start_run(_base("verify_delivery", business_root,
                           archive_path=str(Path(archive_path).resolve()), **options), runs_root)


def validate_quality_report(batch, report, request, run_id):
    """Reject incomplete or misbound coverage even if a worker reports passed."""
    required = set(batch["source"].get("required_rules", []))
    if not required or report.get("input_fingerprint") != request["input_fingerprint"] or report.get("run_id") != run_id:
        raise WorkflowError("质检报告的输入指纹、运行编号或必需规则配置不一致")
    if report.get("rule_version") != request["rule_version"]:
        raise WorkflowError("质检报告的规则版本与本次请求不一致")
    expected = {e["episode_index"]: e["row_count"] for e in request["inventory"]}
    results = report.get("episodes", [])
    if len(results) != len(expected) or {e.get("episode_index") for e in results} != set(expected):
        raise WorkflowError("质检报告任务范围与导入清单不一致")
    for result in results:
        coverage = result.get("coverage", [])
        rules = [c.get("rule") for c in coverage]
        if len(rules) != len(set(rules)) or set(rules) != required:
            raise WorkflowError(f"任务 {result['episode_index']} 的必需规则覆盖缺失、重复或不一致")
        if result.get("status") != "load_failed" and result.get("row_count") != expected[result["episode_index"]]:
            raise WorkflowError("质检记录数与导入清单不一致")
        if any(c.get("status") not in {"performed", "unavailable", "not_checked"} for c in coverage):
            raise WorkflowError("质检覆盖状态无效")
        if result.get("status") == "passed" and (result.get("issues") or any(c["status"] != "performed" for c in coverage)):
            raise WorkflowError("未完整执行或仍有问题的任务不能标为通过")
    return report


def _assert_frozen_batch(request):
    batch = get_store(request["business_root"]).get_batch(request["batch_id"])
    if (batch["revision"] != request["expected_revision"] or batch["content_version"] != request["content_version"]
            or _candidate_snapshot(batch) != request["snapshot"]):
        raise ConflictError("处理期间数据选择、标注或审核已变化，本次旧快照不能继续流转")
    return batch


def _final_quality(directory, request):
    """Recheck real source bytes and annotation semantics without resetting review."""
    from copy import deepcopy
    from .importing import _quality, verify_managed_source
    from .runtime import _read_json, utc_now
    from .reports import report_json, report_html
    from .source import file_sha256
    batch = _assert_frozen_batch(request)
    verify_managed_source(request["source"])
    checks_dir = directory / "final_checks"
    checks_dir.mkdir(parents=True, exist_ok=True)
    check_request = {**request, "rule_version": "hdf5-preview-0.4.0" if request["source_kind"] == "hdf5" else RULE_VERSION}
    details = _quality(check_request, checks_dir)
    fresh = _read_json(Path(details["report_path"]))
    validate_quality_report(batch, fresh, check_request, checks_dir.name)
    required = set(request["source"]["required_rules"])
    selected = [e for e in batch["episodes"] if e["disposition"] == "keep"]
    indexed = {e["episode_index"]: e for e in fresh["episodes"]}
    report = deepcopy(fresh)
    report.update(kind="final_quality", run_id=directory.name, rule_version=FINAL_RULE_VERSION,
                  snapshot_id=request["snapshot"]["snapshot_id"], content_version=request["content_version"],
                  rules=list(FINAL_RULES), basic_rule_version=check_request["rule_version"],
                  basic_report_path=details["report_path"], started_at_utc=utc_now(),
                  basic_report_sha256=file_sha256(Path(details["report_path"])),
                  params={"required_basic_rules": sorted(required), "annotation_outcomes": ["success", "failure", "uncertain"]},
                  episodes=[], issues=[], episode_indices=[e["episode_index"] for e in selected],
                  limitations=["最终质检检查当前保留任务的数据可读取性与标注完整性，不验证机器人动作是否成功。",
                               "合法失败示范可以通过；原始数据保持不变，异常任务必须人工隔离或排除。"])

    def issue(index, rule, message, field=None, evidence=None):
        return {"episode_index": index, "row_index": None, "frame_index": None, "rule": rule,
                "node_id": "final_quality", "category": "data_issue", "message": message,
                "field": field, "file": str(get_store(request["business_root"])._path(batch["batch_id"])), "evidence": evidence or {}}

    mappings = request["snapshot"]["episodes"]
    mapping_ok = (len(mappings) == len(selected) and [m["source_episode_index"] for m in mappings] == [e["episode_index"] for e in selected]
                  and [m["output_episode_index"] for m in mappings] == list(range(len(selected)))
                  and sum(m["row_count"] for m in mappings) == sum(e["row_count"] for e in selected))
    if not selected:
        report["issues"].append(issue(None, "candidate_nonempty", "没有保留任务，最终质检不通过"))
    for episode in selected:
        result = indexed[episode["episode_index"]]
        annotation = episode.get("annotation", {})
        coverage = result.get("coverage", [])
        checks = {
            "candidate_nonempty": (True, "候选集包含此任务", None),
            "selection_confirmed": (batch["cleaning_confirmed"] is True, "人工筛选尚未确认", "cleaning_confirmed"),
            "basic_coverage": (len(coverage) == len(required) and {c["rule"] for c in coverage} == required and all(c["status"] == "performed" for c in coverage), "保留任务未执行完整基础规则", "quality.coverage"),
            "basic_result": (result["status"] == "passed" and not result["issues"] and episode["quality"]["status"] == "passed", "保留任务存在数据问题或缺少已登记的基础检查", "quality.status"),
            "task_text": (isinstance(annotation.get("task"), str) and bool(annotation["task"].strip()), "任务描述不能为空", "annotation.task"),
            "outcome": (isinstance(annotation.get("outcome"), str) and annotation["outcome"] in {"success", "failure", "uncertain"}, "任务执行结果未标注或取值无效", "annotation.outcome"),
            "tags": (isinstance(annotation.get("tags"), list) and all(isinstance(t, str) and t.strip() for t in annotation["tags"]), "问题标签必须为非空文本列表（无标签可为空列表）", "annotation.tags"),
            "review_approved": (episode.get("review", {}).get("status") == "approved", "保留任务的标注尚未通过人工审核", "review.status"),
            "mapping_count": (mapping_ok, "候选任务、导出索引或映射数量不一致", "episodes"),
        }
        own_issues = [issue(episode["episode_index"], rule, reason, field, {"actual": annotation.get(field.split(".")[-1]) if field and field.startswith("annotation.") else None})
                      for rule, (passed, reason, field) in checks.items() if not passed]
        own_issues.extend(result["issues"])
        report["episodes"].append({"episode_index": episode["episode_index"], "row_count": episode["row_count"],
            "expected_length": episode["row_count"], "status": "issues" if own_issues else "passed", "issues": own_issues,
            "coverage": [{"rule": rule, "status": "performed", "detail": "通过" if values[0] else values[1]} for rule, values in checks.items()]})
        report["issues"].extend(own_issues)
    report["summary"] = {"episode_count": len(selected), "row_count": sum(e["row_count"] for e in selected),
        "issue_count": len(report["issues"]), "data_issue_count": sum(i["category"] == "data_issue" for i in report["issues"]),
        "load_failure_count": sum(i["category"] == "load_failure" for i in report["issues"]),
        "not_checked_count": sum(i["category"] == "not_checked" for i in report["issues"]),
        "passed_episode_count": sum(e["status"] == "passed" for e in report["episodes"]),
        "coverage_total": len(selected) * len(FINAL_RULES), "coverage_performed": len(selected) * len(FINAL_RULES)}
    report["outcome"] = "passed" if not report["issues"] and selected else "rejected"
    report["result_digest"] = _snapshot_digest({"snapshot_id": report["snapshot_id"], "rule_version": FINAL_RULE_VERSION,
                                                "episodes": report["episodes"], "issues": report["issues"]})
    destination = directory / "reports"
    destination.mkdir(exist_ok=True)
    path = destination / "final_quality.json"
    path.write_text(report_json(report), encoding="utf-8")
    path.with_suffix(".html").write_text(report_html(report), encoding="utf-8")
    verify_managed_source(request["source"])
    _assert_frozen_batch(request)
    return {"report_path": str(path), "report_sha256": file_sha256(path), "outcome": report["outcome"], "snapshot_id": report["snapshot_id"],
            "rule_version": FINAL_RULE_VERSION, "summary": report["summary"]}


def _final_evidence_path(request, run_id):
    return get_store(request["business_root"]).root / "final_quality" / request["batch_id"] / f"{run_id}.json"


def _register_final_quality(directory, request):
    from .runtime import _read_json, _atomic_json, utc_now
    from .source import file_sha256
    from .importing import verify_managed_source
    batch = _assert_frozen_batch(request)
    verify_managed_source(request["source"])
    details = _read_step(directory, "final_quality")["details"]
    path = Path(details["report_path"])
    report = _read_json(path)
    expected = {e["source_episode_index"]: e["row_count"] for e in request["snapshot"]["episodes"]}
    results = report.get("episodes", [])
    if (file_sha256(path) != details.get("report_sha256")
            or report.get("run_id") != directory.name or report.get("rule_version") != FINAL_RULE_VERSION
            or report.get("snapshot_id") != request["snapshot"]["snapshot_id"]
            or report.get("input_fingerprint") != batch["source"]["input_fingerprint"]
            or report.get("content_version") != request["content_version"]
            or len(results) != len(expected) or {e["episode_index"] for e in results} != set(expected)
            or any(e["row_count"] != expected[e["episode_index"]] for e in results)
            or report["summary"]["episode_count"] != len(expected)
            or report["summary"]["row_count"] != sum(expected.values())
            or file_sha256(Path(report["basic_report_path"])) != report["basic_report_sha256"]):
        raise WorkflowError("最终质检报告未绑定本次运行、版本和实际输入")
    accepted = report.get("outcome") == "passed" and not report["issues"] and bool(report["episodes"])
    if accepted and any(e["status"] != "passed" or len(e["coverage"]) != len(FINAL_RULES) or {c["rule"] for c in e["coverage"]} != set(FINAL_RULES)
                        or any(c["status"] != "performed" for c in e["coverage"]) for e in report["episodes"]):
        raise WorkflowError("最终质检报告覆盖不完整，不能登记为通过")
    evidence = {"schema_version": 1, "batch_id": batch["batch_id"], "run_id": directory.name,
                "content_version": batch["content_version"], "snapshot_id": report["snapshot_id"],
                "report_path": str(path), "report_sha256": file_sha256(path), "rule_version": FINAL_RULE_VERSION,
                "report_html_sha256": file_sha256(path.with_suffix(".html")),
                "accepted": accepted, "outcome": "passed" if accepted else "rejected", "updated_at": utc_now()}
    evidence_path = _final_evidence_path(request, directory.name)
    prior = _read_json(evidence_path)
    if prior:
        if {k: v for k, v in prior.items() if k != "updated_at"} != {k: v for k, v in evidence.items() if k != "updated_at"}:
            raise WorkflowError("相同运行不能登记不同的最终质检证据")
        return prior
    _atomic_json(evidence_path, evidence)
    return evidence


def get_final_quality(batch_id, business_root=None):
    from .runtime import _read_json
    from .source import file_sha256
    store = get_store(business_root)
    batch = store.get_batch(batch_id)
    paths = sorted((store.root / "final_quality" / batch_id).glob("*.json"), reverse=True)
    if not paths:
        return None
    result = _read_json(paths[0])
    result["current"] = (result.get("content_version") == batch["content_version"]
                         and result.get("snapshot_id") == _candidate_snapshot(batch)["snapshot_id"])
    path = Path(result["report_path"])
    result["evidence_valid"] = path.is_file() and file_sha256(path) == result["report_sha256"]
    result["html_evidence_valid"] = bool(result.get("report_html_sha256")) and path.with_suffix(".html").is_file() and file_sha256(path.with_suffix(".html")) == result["report_html_sha256"]
    report = _read_json(path, {}) if result["evidence_valid"] else {}
    basic_path = Path(report.get("basic_report_path", ""))
    result["basic_evidence_valid"] = bool(report.get("basic_report_sha256")) and basic_path.is_file() and file_sha256(basic_path) == report["basic_report_sha256"]
    result["current"] = result["current"] and result["evidence_valid"] and result["html_evidence_valid"] and result["basic_evidence_valid"]
    return result


def _require_final_quality(directory, request):
    from .source import file_sha256
    from .runtime import _read_json
    evidence = _read_json(_final_evidence_path(request, directory.name), {})
    if (not evidence.get("accepted") or evidence.get("snapshot_id") != request["snapshot"]["snapshot_id"]
            or evidence.get("content_version") != request["content_version"]
            or file_sha256(Path(evidence["report_path"])) != evidence["report_sha256"]
            or not Path(evidence["report_path"]).with_suffix(".html").is_file()
            or file_sha256(Path(evidence["report_path"]).with_suffix(".html")) != evidence.get("report_html_sha256")):
        raise WorkflowError("当前快照尚未通过最终数据与标注质检，不能进入转换")
    report = _read_json(Path(evidence["report_path"]), {})
    basic_path = Path(report.get("basic_report_path", ""))
    if not basic_path.is_file() or file_sha256(basic_path) != report.get("basic_report_sha256"):
        raise WorkflowError("最终质检引用的基础检查报告缺失或已变化，不能继续流转")
    return evidence


def _read_step(directory, stage):
    from .runtime import _read_json
    result = _read_json(directory / "steps" / f"{stage}.json", {})
    if result.get("stage") != stage or result.get("status") != "passed" or not isinstance(result.get("details"), dict):
        raise WorkflowError(f"{BUSINESS_LABELS[stage]}缺少有效结果，不能把进程退出当作验证成功")
    return result


def _validated_output(directory, request):
    from .dataset import dataset_fingerprint
    from .runtime import _atomic_json, _read_json
    from .source import file_sha256
    snapshot = request["snapshot"]
    final_evidence = _require_final_quality(directory, request)
    expected_episodes, expected_rows = len(snapshot["episodes"]), sum(e["row_count"] for e in snapshot["episodes"])
    official = _read_step(directory, "official_verify")
    details = official["details"]
    if (details.get("format_version") != "v3.0" or details.get("checked_numeric_frames") != expected_rows
            or details.get("sample_count") != 3 * expected_episodes or details.get("batch_size") != 2
            or details.get("offline") is not True or details.get("snapshot_id") != snapshot["snapshot_id"]):
        raise WorkflowError("官方验证证据缺少全量数值、每任务首中末帧或离线批量加载")
    source_kind = request["source"]["kind"]
    if source_kind == "hdf5":
        if details.get("source_numeric_consistency_rechecked") is not True:
            raise WorkflowError("HDF5 官方验证缺少源数值一致性重核")
        source_sha = file_sha256(Path(request["source"]["hdf5_path"]))
        if official.get("input_sha256") != source_sha or details.get("source_sha256") != source_sha:
            raise WorkflowError("官方验证所对应的源文件哈希不一致")
    else:
        if details.get("output_numeric_frames_checked") != expected_rows:
            raise WorkflowError("SO-100 官方验证未核对全量数值帧数")
        # 把来源版本与 manifest 哈希补进 details，便于后续交付包沿用。
        details = {**details, "manifest_sha256": file_sha256(Path(request["output_path"]) / "source_manifest.json"),
                   "source_repo": details.get("source_repo") or request["source"].get("repo_id"),
                   "source_revision": details.get("source_revision") or request["source"].get("revision")}
        _atomic_json(directory / "steps" / "official_verify.json", {**official, "details": details})
    quality = _read_step(directory, "output_quality")["details"]
    if quality.get("workbench_official_frames_matched") != expected_episodes * 3:
        raise WorkflowError("工作台与官方加载器的任务视频偏移交叉验证不完整")
    report_path = Path(quality["report_path"])
    report = _read_json(report_path)
    summary = report["summary"]
    from .quality import RULES
    episodes = report.get("episodes", [])
    if (len(episodes) != expected_episodes or summary.get("row_count") != expected_rows
            or summary.get("issue_count") != 0 or summary.get("coverage_total") != expected_episodes * len(RULES)
            or summary.get("coverage_performed") != summary["coverage_total"]
            or any(e.get("status") != "passed" or {c["rule"] for c in e["coverage"]} != set(RULES) for e in episodes)):
        raise WorkflowError("输出质检未完整通过或数量与审核候选集不一致")
    fingerprint = dataset_fingerprint(Path(request["output_path"]))
    if report["input_fingerprint"] != fingerprint or report.get("run_id") != directory.name:
        raise WorkflowError("转换输出在质检后发生变化或报告绑定错误")
    return {"official_loader_passed": True, "numeric_consistency_passed": True,
            "source_numeric": True, "episode_count": expected_episodes, "row_count": expected_rows,
            "snapshot_id": snapshot["snapshot_id"], "format_version": "v3.0",
            "output_fingerprint": fingerprint,
            "final_quality_required": True, "final_quality_sha256": final_evidence["report_sha256"],
            "official_evidence_sha256": file_sha256(directory / "steps/official_verify.json"),
            "report_sha256": file_sha256(report_path), "quality_report_path": str(report_path),
            "source_kind": source_kind}


def _validate_archive(directory, request):
    from .source import file_sha256
    verified = _read_step(directory, "verify")["details"]
    official = _read_step(directory, "archive_verify")["details"]
    if verified.get("manifest_verified") is not True:
        raise WorkflowError("交付包清单与文件校验尚未通过")
    count, rows = verified["episode_count"], verified["row_count"]
    if (official.get("format_version") != "v3.0" or official.get("episode_count") != count
            or official.get("row_count") != rows or official.get("sample_count") != count * 3
            or official.get("offline") is not True or official.get("batch_size") != 2):
        raise WorkflowError("交付包官方加载证据不完整或数量不一致")
    if request.get("snapshot") and official.get("snapshot_id") != request["snapshot"]["snapshot_id"]:
        raise WorkflowError("交付包官方加载证据的业务快照不一致")
    actual = file_sha256(Path(verified["zip_path"]))
    if actual != verified["zip_sha256"]:
        raise WorkflowError("交付 ZIP 在验证后发生变化，不能登记")
    if request.get("snapshot"):
        episodes = request["snapshot"]["episodes"]
        if count != len(episodes) or rows != sum(e["row_count"] for e in episodes):
            raise WorkflowError("交付包数量与冻结的审核候选集不一致")
    from .delivery import _validate_payload
    _validate_payload(Path(verified["unpacked_path"]), request.get("snapshot", {}).get("snapshot_id"))
    return verified


def _evidence_hashes(directory, operation, registration_stage="registration", request=None, request_path=None):
    from .source import file_sha256
    request_path = Path(request_path) if request_path else directory / "request.json"
    records = {request_path.relative_to(directory).as_posix(): file_sha256(request_path)}
    for stage in business_stages(operation, request):
        if stage == registration_stage:
            break
        if stage in {"registration", "import_registration", "conversion_registration"}:
            continue
        _read_step(directory, stage)
        path = directory / "steps" / f"{stage}.json"
        records[path.relative_to(directory).as_posix()] = file_sha256(path)
    for path in (directory / "reports").glob("*"):
        if path.is_file():
            records[path.relative_to(directory).as_posix()] = file_sha256(path)
    return records


def _register(directory):
    from .runtime import _read_json, _atomic_json, utc_now
    from .importing import verify_managed_source
    pending = _read_json(directory / "registration.json", {})
    request = _read_json(Path(pending.get("request_path", directory / "request.json")))
    store = get_store(request["business_root"])
    operation = request["operation"]
    registration_stage = pending.get("stage", "registration")
    expected = pending.get("evidence_sha256")
    if not expected or expected != _evidence_hashes(directory, operation, registration_stage, request, pending.get("request_path")):
        raise WorkflowError("登记证据不完整或处理产物证据已变化，请保留该运行排查")
    registration_operation = pending.get("registration_operation", operation)
    if registration_operation == "import":
        details = _read_step(directory, "inventory")["details"]
        verify_managed_source(details["source"])
        state = store.create_batch(details["label"], details["source"], details["episodes"], run_id=directory.name)
    else:
        verify_managed_source(request["source"])
        state = store.get_batch(request["batch_id"])
        if registration_operation == "quality":
            details = _read_step(directory, "quality")["details"]
            report = _read_json(Path(details["report_path"]))
            validate_quality_report(state, report, request, directory.name)
            state = store.apply_quality_report(state["batch_id"], report, run_id=directory.name,
                       expected_revision=request["expected_revision"], snapshot_id=request["snapshot_id"])
        elif registration_operation == "conversion":
            validation = _validated_output(directory, request)
            state = store.register_verified_output(state["batch_id"], request["snapshot"], run_id=directory.name,
                        artifact_path=request["output_path"], validation=validation, expected_revision=request["expected_revision"])
        elif registration_operation == "delivery":
            verified = _validate_archive(directory, request)
            state = store.register_delivery(state["batch_id"], request["snapshot"]["output_id"],
                        snapshot_id=request["snapshot"]["snapshot_id"], zip_path=verified["zip_path"],
                        zip_sha256=verified["zip_sha256"], packaging_run_id=directory.name, archive_verified=True,
                        expected_revision=request["expected_revision"])
        else:
            raise WorkflowError("此操作无需业务回写")
    result = {**pending, "status": "registered", "batch_id": state["batch_id"], "run_id": directory.name,
              "revision": state["revision"], "message": "处理证据与业务版本已核对，结果已登记", "updated_at": utc_now()}
    _atomic_json(directory / "registration.json", result)
    _atomic_json(directory / "registrations" / f"{registration_stage}.json", result)
    return result


def reconcile_run(run_id, runs_root=None):
    """Retry only a pending completed artifact registration, without rerunning work."""
    from .runtime import (_run_dir, _read_json, _atomic_json, get_run, ACTIVE_STATUSES,
                          _startup_guard, _save_state, _set_node, _event, utc_now)
    directory = _run_dir(run_id, runs_root)
    with _startup_guard(directory.parent):
        state = get_run(run_id, runs_root)
        if state["status"] in ACTIVE_STATUSES:
            raise WorkflowError("后台仍在运行，请等待结果回写")
        registration = _read_json(directory / "registration.json", {})
        if registration.get("status") in {"registered", "stale"}:
            return registration
        if registration.get("status") not in {"pending", "failed"}:
            raise WorkflowError("本次运行尚未产生可恢复登记的完整处理产物")
        try:
            result = _register(directory)
            recovered_stage = registration.get("stage", "registration")
            _atomic_json(directory / "steps" / f"{recovered_stage}.json", {"stage": recovered_stage, "status": "passed", "details": result})
            for node in state["nodes"]:
                if node["node_id"] == recovered_stage:
                    _set_node(node, "completed", "已核对保留证据并恢复业务登记", evidence=result)
                elif node["status"] == "pending":
                    _set_node(node, "skipped", "协调器中断后未执行此步骤；本次仅恢复已完成产物的登记")
            state.update(batch_id=result["batch_id"], status="completed", completed_at=utc_now(),
                         message="已核对保留的处理证据并恢复业务登记；中断时尚未执行的步骤须重新运行")
        except ConflictError as exc:
            result = {**registration, "status": "stale", "message": str(exc)}
        except Exception as exc:
            result = {**registration, "status": "failed", "message": str(exc)}
        _atomic_json(directory / "registration.json", result)
        state["business_registration"] = result
        _event(directory, "registration", "reconciled", result["message"],
               level="INFO" if result["status"] == "registered" else "WARNING", registration_status=result["status"])
        _save_state(directory, state)
        return result


def coordinate_business(directory, state):
    from .runtime import (_atomic_json, _event, _identity, _node_for, _read_json, _save_state, _set_node, run_command_step)
    from .conversion import CONVERSION_PYTHON, CONVERSION_WORKER
    from .errors import error_details, explain_error
    request = state["request"]
    operation = request["operation"]
    root = Path(request["business_root"])
    request["managed_dir"] = str(root / "managed" / directory.name)
    if operation in {"conversion", "final_quality"} and request.get("auto_continue", True):
        request["output_path"] = str(root / "outputs" / directory.name / "staging")
        # SO-100 需要 v2.1 中间副本目录，与 staging 平行放置
        if request.get("source_kind") == "so100":
            request["v21_work_path"] = str(root / "outputs" / directory.name / "v21_work")
        _atomic_json(directory / "conversion_spec.json", request["snapshot"])
    _atomic_json(directory / "request.json", request)
    state["batch_id"] = request.get("batch_id")
    state["episode_total"] = len(request.get("snapshot", {}).get("episodes", request.get("inventory", [])))
    environment = {"HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
                   "ROBODATA_PARENT_PID": str(os.getpid()), "ROBODATA_PARENT_IDENTITY": _identity(os.getpid())}
    stages = business_stages(operation, request)
    registration_stages = {"registration", "import_registration", "conversion_registration"}
    stage = stages[0]
    try:
        for stage in stages:
            (directory / "diagnostic.json").unlink(missing_ok=True)
            if stage in registration_stages:
                registration_operation = ("import" if stage == "import_registration" else "conversion" if stage == "conversion_registration"
                    else "quality" if operation == "import" else "delivery" if operation == "final_quality" else operation)
                evidence_request = directory / "requests" / f"{stage}.json"
                _atomic_json(evidence_request, request)
                pending = {"status": "pending", "run_id": directory.name, "batch_id": request.get("batch_id"),
                           "stage": stage, "registration_operation": registration_operation,
                           "request_path": str(evidence_request),
                           "operation": operation, "message": "处理产物已保存，等待核对业务版本并登记",
                           "evidence_sha256": _evidence_hashes(directory, operation, stage, request, evidence_request)}
                _atomic_json(directory / "registration.json", pending)
                state["business_registration"] = pending
                state["artifacts"]["registration"] = str(directory / "registration.json")
            if stage in {"copy", "preview", "inventory", "quality"}:
                command = [sys.executable, "-m", "robodata.importing", "--stage", stage, "--run-dir", str(directory)]
            elif stage in {"source_verify", "output_quality", "final_quality", "final_registration", *registration_stages}:
                command = [sys.executable, "-m", "robodata.business", "--stage", stage, "--run-dir", str(directory)]
            elif stage in {"package", "verify"}:
                command = [sys.executable, "-m", "robodata.delivery", "--stage", stage, "--run-dir", str(directory)]
            elif stage == "archive_verify":
                unpacked = _read_step(directory, "verify")["details"]["dataset_path"]
                worker = _so100_worker_path() if request.get("source_kind") == "so100" else CONVERSION_WORKER
                command = [str(CONVERSION_PYTHON), str(worker), "--stage", stage,
                           "--output", unpacked, "--artifacts", str(directory)]
            else:
                _assert_frozen_batch(request)
                _require_final_quality(directory, request)
                worker = _so100_worker_path() if request.get("source_kind") == "so100" else CONVERSION_WORKER
                command = [str(CONVERSION_PYTHON), str(worker), "--stage", stage,
                           "--input", request["input_path"], "--output", request["output_path"],
                           "--artifacts", str(directory), "--spec", str(directory / "conversion_spec.json")]
                if request["source"].get("test_label"):
                    command.extend(["--test-mode", request["source"]["test_label"]])
                if stage in {"metadata_upgrade", "format_write"}:
                    # SO-100 worker 两个阶段都需要 v2.1 中间副本目录
                    command.extend(["--work", request["v21_work_path"]])
            success = run_command_step(directory, state, stage, command,
                         timeout=request["timeouts"].get(stage, request["timeouts"]["default"]), env_overrides=environment)
            if stage in registration_stages:
                state["business_registration"] = _read_json(directory / "registration.json", pending)
            if not success:
                node = _node_for(state, stage)
                diagnostic = _read_json(directory / "diagnostic.json", {})
                if diagnostic:
                    node.update(diagnostics=diagnostic, input_path=diagnostic.get("file") or diagnostic.get("input_path"))
                    node["message"] = diagnostic.get("message_zh", node["message"])
                    _event(directory, stage, "diagnostic", node["message"], level="ERROR", diagnostics=diagnostic)
                state.update(status="failed", message=f"{BUSINESS_LABELS[stage]}未完成；此前产物和日志已保留。{node['message']}")
                return
            step = _read_step(directory, stage)
            node = _node_for(state, stage)
            node.update(evidence=step["details"], artifact_path=str(directory / "steps" / f"{stage}.json"))
            state["artifacts"][stage] = node["artifact_path"]
            if stage in {"quality", "final_quality", "output_quality"}:
                report_path = Path(step["details"]["report_path"])
                report = _read_json(report_path)
                state.update(report_path=str(report_path), summary=report["summary"], result_digest=report["result_digest"])
                state["artifacts"].update(json=str(report_path), html=str(report_path.with_suffix(".html")))
                count = report["summary"].get("data_issue_count", 0)
                node.update(data_issue_count=count, message="检查完成，发现数据异常" if count else "检查完成")
                if stage == "final_quality":
                    node["outcome"] = report["outcome"]
                    state["final_quality_outcome"] = report["outcome"]
                    state["artifacts"]["final_quality_report"] = str(report_path)
                for issue in report.get("issues", []):
                    _event(directory, stage, "issue", issue["message"], issue.get("episode_index"),
                           level="WARNING" if issue.get("category") == "data_issue" else "ERROR" if issue.get("category") == "load_failure" else "INFO",
                           row_index=issue.get("row_index"), rule=issue.get("rule"),
                           source_node_id=issue.get("node_id"), field=issue.get("field"),
                           input_path=issue.get("file") or issue.get("evidence", {}).get("file"),
                           evidence=issue.get("evidence", {}))
            if stage == "inventory":
                state["episode_total"] = len(step["details"]["episodes"])
            if stage in registration_stages:
                state["batch_id"] = step["details"]["batch_id"]
            if stage == "import_registration":
                batch = get_store(root).get_batch(state["batch_id"])
                from .importing import verify_managed_source
                verify_managed_source(batch["source"])
                if all(e["quality"]["status"] != "not_checked" for e in batch["episodes"]):
                    for remaining in state["nodes"]:
                        if remaining["status"] == "pending":
                            _set_node(remaining, "skipped", "相同输入已有基础检查，复用原批次且保留已有审核")
                    state.update(status="completed", episode_completed=len(batch["episodes"]),
                                 message="相同输入已登记，已打开已有批次；保留既有筛选、审核和交付记录")
                    return
                request.update(batch_id=batch["batch_id"], expected_revision=batch["revision"], content_version=batch["content_version"],
                    source=batch["source"], source_kind=batch["source"]["kind"], input_fingerprint=batch["source"]["input_fingerprint"],
                    inventory=[{"episode_index": e["episode_index"], "row_count": e["row_count"]} for e in batch["episodes"]],
                    rule_version="hdf5-preview-0.4.0" if batch["source"]["kind"] == "hdf5" else RULE_VERSION)
                request["snapshot_id"] = _snapshot_digest({k: request[k] for k in ("batch_id", "content_version", "input_fingerprint", "inventory", "rule_version")})
                _atomic_json(directory / "request.json", request)
            if stage == "final_registration" and not step["details"]["accepted"]:
                for remaining in state["nodes"]:
                    if remaining["status"] == "pending":
                        _set_node(remaining, "skipped", "最终数据与标注质检不通过，等待修改并重新审核")
                state.update(status="completed", outcome="rejected", episode_completed=state["episode_total"],
                             message="最终质检执行完成但未通过，问题报告已保存；转换和交付未启动")
                return
            if stage == "conversion_registration":
                batch = get_store(root).get_batch(state["batch_id"])
                if batch["content_version"] != request["content_version"]:
                    raise ConflictError("转换登记后业务版本已变化，不能继续打包")
                output = next(o for o in batch["outputs"] if o["run_id"] == directory.name)
                request.update(expected_revision=batch["revision"], snapshot=get_store(root).delivery_spec(batch["batch_id"], output["output_id"]))
                _atomic_json(directory / "request.json", request)
            if stage == "verify":
                state["artifacts"]["zip"] = step["details"]["zip_path"]
            _event(directory, stage, "evidence", "节点证据已保存", evidence=step["details"])
            _save_state(directory, state)
        if operation == "verify_delivery":
            _validate_archive(directory, request)
        message = "导入及基础清洗检查已完成，等待人工筛选与标注审核" if operation == "import" else "业务处理已完成，真实处理证据与记录均已保存"
        state.update(status="completed", outcome="passed", episode_completed=state["episode_total"], message=message)
    except Exception as exc:
        diagnostic = error_details(exc, include_traceback=True)
        _set_node(_node_for(state, stage), "failed", explain_error(exc, BUSINESS_LABELS[stage]), diagnostics=diagnostic)
        state.update(status="failed", message=explain_error(exc, BUSINESS_LABELS[stage]))
        _event(directory, stage, "failed", state["message"], level="ERROR", diagnostics=diagnostic)
    finally:
        _save_state(directory, state)


def _output_quality(directory, request):
    from .dataset import load_episode
    from .video import read_video_frame
    from .quality import run_batch, RULES
    from .reports import write_report
    expected = request["snapshot"]["episodes"]
    report = run_batch(Path(request["output_path"]), episode_indices=[e["output_episode_index"] for e in expected], source_kind="converted_public")
    report["run_id"] = directory.name
    summary = report["summary"]
    paths = write_report(report, directory / "reports")
    if (summary["episode_count"] != len(expected) or summary["row_count"] != sum(e["row_count"] for e in expected)
            or summary["issue_count"] or summary["coverage_performed"] != len(expected) * len(RULES)):
        raise WorkflowError("输出质检未完整通过，不能登记为验证通过的产物")
    official = _read_step(directory, "official_verify")["details"]
    samples = official.get("samples", [])
    expected_positions = {(e["output_episode_index"], row) for e in expected for row in (0, e["row_count"] // 2, e["row_count"] - 1)}
    if {(s["episode"], s["row"]) for s in samples} != expected_positions:
        raise WorkflowError("官方视频抽查未覆盖候选集中每条任务的首、中、末位置")
    for sample in samples:
        episode = load_episode(Path(request["output_path"]), sample["episode"])
        row = episode.table.iloc[sample["row"]]
        frame = read_video_frame(episode.video_path, float(row["timestamp"]) + episode.video_start_time)
        if int(row["index"]) != sample["global_index"] or hashlib.sha256(frame["image"].tobytes()).hexdigest() != sample["decoded_image_sha256"]:
            raise WorkflowError(f"任务 {sample['episode']} 行 {sample['row']} 的工作台与官方图像或索引不同，请检查共享视频偏移")
    return {"report_path": str(paths["json"]), "fingerprint": report["input_fingerprint"], "summary": summary,
            "workbench_official_frames_matched": len(samples)}


def main():
    from .runtime import _atomic_json, _read_json
    from .errors import error_details, explain_error
    parser = argparse.ArgumentParser(description="受监督的业务结果处理节点")
    parser.add_argument("--stage", choices=["source_verify", "output_quality", "registration", "import_registration", "conversion_registration", "final_quality", "final_registration"], required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    request = _read_json(directory / "request.json")
    try:
        if args.stage in {"registration", "import_registration", "conversion_registration"}:
            details = _register(directory)
        elif args.stage == "final_quality":
            details = _final_quality(directory, request)
        elif args.stage == "final_registration":
            details = _register_final_quality(directory, request)
        elif args.stage == "source_verify":
            from .importing import verify_managed_source
            details = {"input_fingerprint": verify_managed_source(request["source"])}
        else:
            details = _output_quality(directory, request)
        _atomic_json(directory / "steps" / f"{args.stage}.json", {"stage": args.stage, "status": "passed", "details": details})
        return 0
    except Exception as exc:
        diagnostic = {"stage": args.stage, "message_zh": explain_error(exc, BUSINESS_LABELS[args.stage]),
                      "input_path": request.get("input_path"), **error_details(exc, include_traceback=True)}
        _atomic_json(directory / "diagnostic.json", diagnostic)
        if args.stage in {"registration", "import_registration", "conversion_registration"}:
            prior = _read_json(directory / "registration.json", {})
            _atomic_json(directory / "registration.json", {**prior, "status": "stale" if isinstance(exc, ConflictError) else "failed", "message": str(exc)})
        _atomic_json(directory / "steps" / f"{args.stage}.json", {"stage": args.stage, "status": "failed", "diagnostic": diagnostic})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
