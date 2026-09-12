"""Fixed HDF5 conversion orchestration. Heavy dependencies stay in another venv.

Unverified output remains isolated in <output_root>/<run_id>/staging. An available
marker outside that dataset is written only after official and workbench checks.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import threading

from .config import PROJECT_ROOT

DEFAULT_EXPORT_ROOT = PROJECT_ROOT / "work" / "exports"
CONVERSION_TOOL_ROOT = PROJECT_ROOT / "tools" / "conversion"
CONVERSION_PYTHON = CONVERSION_TOOL_ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
CONVERSION_WORKER = CONVERSION_TOOL_ROOT / "worker.py"
CONVERSION_STAGES = {
    "file_validation": "文件校验", "hdf5_validation": "HDF5 结构与数值检查",
    "field_mapping": "字段映射", "format_write": "官方格式写入",
    "official_verify": "官方加载验证", "output_quality": "输出质检",
}
STAGE_TIMEOUTS = {"file_validation": 30, "hdf5_validation": 60, "field_mapping": 60,
                  "format_write": 180, "official_verify": 120, "output_quality": 60}


def list_exports(output_root: Path | None = None) -> list[dict]:
    """Only offer registered outputs whose current bytes still match verification."""
    from .dataset import dataset_fingerprint
    root = Path(output_root or DEFAULT_EXPORT_ROOT).resolve()
    exports = []
    for marker in root.glob("*/available.json"):
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
            data_path = Path(value["output_path"]).resolve()
            # A copied/untrusted registry cannot silently point outside its run.
            if data_path != marker.parent / "staging" or value.get("status") != "available":
                continue
            if not data_path.is_dir() or dataset_fingerprint(data_path) != value["fingerprint"]:
                continue
            exports.append({**value, "path": str(data_path), "available_path": str(marker.resolve())})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(exports, key=lambda item: item["run_id"], reverse=True)


def latest_export(output_root: Path | None = None) -> dict | None:
    exports = list_exports(output_root)
    return exports[0] if exports else None


def _step_failed(directory: Path, state: dict, stage: str, message: str, diagnostics=None) -> None:
    from .runtime import _event, _node_for, _save_state, _set_node
    node = _node_for(state, stage)
    evidence = {}
    if diagnostics:
        evidence = {key: diagnostics[key] for key in ("file", "demo", "field", "row") if diagnostics.get(key) is not None}
        evidence.update(diagnostics.get("evidence", {}))
    _set_node(node, "failed", message, evidence=evidence, **({"diagnostics": diagnostics} if diagnostics else {}))
    if diagnostics and diagnostics.get("file"):
        node["input_path"] = diagnostics["file"]
    _event(directory, stage, "failed", message, level="ERROR", evidence=evidence,
           **({"diagnostics": diagnostics} if diagnostics else {}))
    state.update(status="failed", message=f"{CONVERSION_STAGES[stage]}失败，输出未登记为可用；请查看该节点日志")
    _save_state(directory, state)


def _read_step(directory: Path, state: dict, stage: str, success: bool) -> dict | None:
    from .runtime import _event, _node_for, _read_json, _save_state
    result_path = directory / "steps" / f"{stage}.json"
    result = _read_json(result_path, {})
    if not success:
        node = _node_for(state, stage)
        if node["status"] == "timed_out":
            state.update(status="failed", message=f"{CONVERSION_STAGES[stage]}超时，工作进程已终止；输出未登记为可用")
            _save_state(directory, state)
            return None
        diagnostic = _read_json(directory / "diagnostic.json", {})
        if diagnostic and diagnostic.get("stage") == stage:
            message = diagnostic.get("message_zh") or diagnostic.get("message") or "转换节点执行失败"
            _step_failed(directory, state, stage, message, diagnostic)
        else:
            node = _node_for(state, stage)
            state.update(status="failed", message=f"{CONVERSION_STAGES[stage]}未完成，输出未登记为可用；{node['message']}")
            _save_state(directory, state)
        return None
    if not isinstance(result, dict) or result.get("stage") != stage or result.get("status") != "passed":
        _step_failed(directory, state, stage, "工作进程没有生成有效的成功记录；不能把退出码为零等同于验证通过")
        return None
    node = _node_for(state, stage)
    node["evidence"] = result.get("details", {})
    node["artifact_path"] = str(result_path)
    _event(directory, stage, "evidence", "节点结果已保存，可查看字段、样本位置与验证证据", evidence=result.get("details", {}), artifact_path=str(result_path))
    state["artifacts"][stage] = str(result_path)
    _save_state(directory, state)
    return result


def coordinate_conversion(directory: Path, state: dict, supervise=None) -> None:
    """Called inside the persistent runtime coordinator, under its single-run lock."""
    from .dataset import dataset_fingerprint
    from .errors import error_details, explain_error
    from .runtime import (_atomic_json, _event, _identity, _node, _read_json,
                          _save_state, run_command_step, utc_now)
    request = state["request"]
    verifying = bool(request.get("verify_only"))
    stages = ("file_validation", "official_verify", "output_quality") if verifying else tuple(CONVERSION_STAGES)
    nodes = []
    for stage in stages:
        node = _node(stage)
        node["label"] = CONVERSION_STAGES[stage]
        nodes.append(node)
    state["nodes"] = nodes
    state.update(episode_total=3, episode_completed=0,
                 message="正在重新验证已有 LeRobot v3.0 产物" if verifying else "正在转换固定的 Panda／Lift 仿真测试数据")
    stage = "file_validation"
    try:
        if not request.get("input_path"):
            raise ValueError("请指定固定版本 HDF5 样本的 input_path")
        source_path = Path(request["input_path"]).resolve()
        output_root = Path(request.get("output_root") or DEFAULT_EXPORT_ROOT).resolve()
        if verifying and not request.get("export_path"):
            raise ValueError("重新验证需要指定 export_path")
        staging = Path(request["export_path"]).resolve() if verifying else output_root / directory.name / "staging"
        marker = staging.parent / "available.json"
        if verifying and not staging.is_dir():
            raise FileNotFoundError("需要重新验证的输出数据目录不存在")
        if not verifying and (staging.exists() or marker.exists()):
            raise FileExistsError("本次运行的目标目录已存在；不会覆盖旧产物")
        request.update(input_path=str(source_path), output_root=str(output_root), output_path=str(staging))
        state.update(output_path=str(staging), available_path=None,
                     verification_path=str(directory / "steps" / "official_verify.json"))
        _atomic_json(directory / "request.json", request)
        _save_state(directory, state)
        environment = {"HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
                       "ROBODATA_PARENT_PID": str(os.getpid()), "ROBODATA_PARENT_IDENTITY": _identity(os.getpid())}
        records = {}
        for stage in stages:
            (directory / "diagnostic.json").unlink(missing_ok=True)
            if stage == "output_quality":
                command = [sys.executable, "-m", "robodata.conversion", "--quality-output", str(staging), "--artifacts", str(directory)]
            else:
                if not CONVERSION_PYTHON.is_file():
                    raise FileNotFoundError("独立转换环境尚未安装；请执行转换环境的固定依赖安装步骤")
                command = [str(CONVERSION_PYTHON), str(CONVERSION_WORKER), "--stage", stage,
                           "--input", str(source_path), "--output", str(staging), "--artifacts", str(directory)]
            timeout = request["timeouts"].get(stage, STAGE_TIMEOUTS[stage])
            success = run_command_step(directory, state, stage, command, timeout=timeout, env_overrides=environment)
            record = _read_step(directory, state, stage, success)
            quality_json = directory / "reports" / "quality_report.json"
            if stage == "output_quality" and quality_json.is_file():
                state["report_path"] = str(quality_json)
                state["artifacts"].update(json=str(quality_json), html=str(quality_json.with_suffix(".html")))
                _save_state(directory, state)
            if record is None:
                return
            if stage == "official_verify":
                details = record.get("details", {})
                if (details.get("format_version") != "v3.0" or details.get("checked_numeric_frames") != 174 or
                        details.get("sample_count") != 9 or details.get("batch_size") != 2 or details.get("offline") is not True):
                    _step_failed(directory, state, stage, "官方验证记录不完整：需要全量数值、9 个视频位置、离线加载及 batch_size=2 验证证据")
                    return
            records[stage] = record
        # Both official decoding and workbench checks succeeded; recheck identity
        # before making this output selectable by the browser or CLI.
        report = _read_json(Path(state["report_path"]))
        current_fingerprint = dataset_fingerprint(staging)
        if current_fingerprint != report["input_fingerprint"]:
            raise ValueError("输出文件在质检后发生变化，不能登记为验证通过")
        from .source import file_sha256
        if file_sha256(source_path) != records["file_validation"]["input_sha256"]:
            raise ValueError("源 HDF5 在运行过程中发生变化，不能登记为验证通过")
        if verifying:
            state.update(status="completed", message="已有产物的官方加载验证和输出质检全部通过",
                         episode_completed=3, summary=report["summary"], result_digest=report["result_digest"],
                         available_path=str(marker) if marker.is_file() else None)
            _event(directory, "output_quality", "verified", "已有产物完成重新验证，数据文件未被重写", output_path=str(staging), fingerprint=current_fingerprint)
            _save_state(directory, state)
            return
        available = {"schema_version": 1, "run_id": directory.name, "status": "available",
                     "output_path": str(staging), "path": str(staging), "fingerprint": current_fingerprint,
                     "input_path": str(source_path), "input_sha256": records["file_validation"]["input_sha256"],
                     "format_version": "v3.0", "episode_count": report["summary"]["episode_count"],
                     "frame_count": report["summary"]["row_count"], "created_at": utc_now(),
                     "report_path": state["report_path"], "source": report["source"],
                     "official_verification": str(directory / "steps" / "official_verify.json")}
        _atomic_json(marker, available)
        state.update(status="completed", message="转换、官方加载验证和输出质检全部完成；产物已登记为可用",
                     available_path=str(marker), episode_completed=3, summary=report["summary"], result_digest=report["result_digest"])
        state["artifacts"]["available"] = str(marker)
        _event(directory, "output_quality", "registered", "已登记通过官方加载验证和输出质检的 LeRobot v3.0 产物", output_path=str(staging), fingerprint=current_fingerprint)
        _save_state(directory, state)
    except Exception as exc:
        _step_failed(directory, state, stage, explain_error(exc, CONVERSION_STAGES[stage]), error_details(exc, include_traceback=True))


def _quality_output(output: Path, directory: Path) -> int:
    from .errors import error_details, explain_error
    from .quality import run_batch
    from .reports import write_report
    from .runtime import _atomic_json, _parent_watchdog, _read_json, _set_node, utc_now
    stage = "output_quality"
    state = _read_json(directory / "state.json")
    threading.Thread(target=_parent_watchdog, args=(state["coordinator_pid"], state["coordinator_identity"]), daemon=True).start()
    try:
        report = run_batch(output, params=state["request"].get("params"), source_kind="converted_public")
        report["run_id"] = directory.name
        report["execution"] = {"nodes": state["nodes"], "events_path": "events.jsonl", "kind": "conversion"}
        summary = report["summary"]
        passed = (summary["episode_count"] == 3 and summary["row_count"] == 174 and
                  summary["coverage_performed"] == summary["coverage_total"] and
                  summary["issue_count"] == 0)
        node = next(n for n in report["execution"]["nodes"] if n["node_id"] == stage)
        _set_node(node, "completed" if passed else "failed", "输出质检通过" if passed else "输出质检未通过")
        paths = write_report(report, directory / "reports")
        if not passed:
            raise ValueError("转换输出仍有异常、未检查项或任务帧数不一致，不能登记为可用")
        record = {"schema_version": 1, "stage": stage, "status": "passed", "details": {
                  "summary": summary, "fingerprint": report["input_fingerprint"], "report_path": str(paths["json"]),
                  "format_version": "v3.0", "checked_numeric_frames": 174}}
        _atomic_json(directory / "steps" / f"{stage}.json", record)
        return 0
    except Exception as exc:
        details = {"stage": stage, "status": "failed", "message_zh": explain_error(exc, "输出质检"),
                   "timestamp": utc_now(), **error_details(exc, include_traceback=True)}
        _atomic_json(directory / "diagnostic.json", details)
        _atomic_json(directory / "steps" / f"{stage}.json", {"stage": stage, "status": "failed", "details": details})
        return 1


def main():
    parser = argparse.ArgumentParser(description="独立运行转换产物的工作台质检")
    parser.add_argument("--quality-output", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(_quality_output(args.quality_output, args.artifacts))


if __name__ == "__main__":
    main()
