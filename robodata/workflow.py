"""Eight-node, read-only projection of authoritative batch and runtime records.

The files below ``flows`` are immutable exports of real business decisions, not
a second workflow state machine. Refreshing a page never invents an audit event.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from robodata_business import WorkflowError, ConflictError
from .config import PROJECT_ROOT

STAGES = (("import", "上传／导入"), ("clean", "数据清洗"), ("select", "数据筛选"),
          ("annotate", "数据标注"), ("review", "标注审核"), ("quality", "数据质检"),
          ("convert", "格式转换"), ("deliver", "打包交付"))
STATUS_LABELS = {"ready": "已就绪", "waiting_upstream": "等待上游", "waiting_human": "等待人工",
                 "running": "运行中", "completed": "已完成", "failed": "执行失败",
                 "timed_out": "超时", "interrupted": "中断", "superseded": "已过期"}
_AUDIT_NODE = {"create_batch": "import", "apply_quality_report": "clean", "set_disposition": "select",
               "confirm_cleaning": "select", "save_annotation": "annotate", "submit_annotation": "annotate",
               "review_annotation": "review", "register_verified_output": "convert", "register_delivery": "deliver"}
_CONTENT_FREE = {"register_verified_output", "register_delivery"}


def _root(root=None):
    return Path(root or os.environ.get("ROBODATA_BUSINESS_ROOT") or PROJECT_ROOT / "work/business").resolve()


def _runs_root(root=None):
    from .runtime import _root as runtime_root
    return runtime_root(root)


def _json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return default


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact(path, role, *, content_version, current=True, expected_sha256=None, expected_size=None):
    """Read an actual file identity; missing and changed evidence stays visible."""
    path = Path(path).resolve()
    ref = {"path": str(path), "role": role, "size": None, "sha256": None,
           "content_version": content_version, "current": bool(current), "valid": False,
           "validity": "尚未取得文件", "expected_sha256": expected_sha256}
    try:
        if path.is_symlink() or not path.is_file():
            raise WorkflowError("文件不存在或不是普通文件")
        ref.update(size=path.stat().st_size, sha256=_sha(path))
        unchanged = ((expected_sha256 is None or ref["sha256"] == expected_sha256)
                     and (expected_size is None or ref["size"] == expected_size))
        ref.update(valid=unchanged and bool(current),
                   validity="当前有效" if unchanged and current else "历史版本，已过期" if unchanged else "文件已变化，旧证据失效")
    except (OSError, WorkflowError) as exc:
        ref["validity"] = str(exc)
    return ref


def _immutable_json(path, value):
    """Install a fully flushed file without replacing an earlier snapshot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    expected = hashlib.sha256(encoded).hexdigest()
    if not path.exists():
        fd, temporary = tempfile.mkstemp(prefix=".snapshot-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass  # Another view materialized the same authoritative value.
        finally:
            Path(temporary).unlink(missing_ok=True)
    return expected


def _audits(batch):
    version = 0
    result = []
    for item in batch.get("history", []):
        if item.get("operation") not in _CONTENT_FREE:
            version += 1
        result.append({**item, "content_version": version,
                       "business_node_id": _AUDIT_NODE.get(item.get("operation")),
                       "attempt_id": f"{batch['batch_id']}:r{item['revision']}",
                       "source": "business_audit"})
    return result


def _mapping(batch):
    output_index = 0
    result = []
    for episode in batch["episodes"]:
        keep = episode["disposition"] == "keep"
        result.append({"source_episode_index": episode["episode_index"],
                       "source_demo": f"demo_{episode['episode_index']}" if batch["source"]["kind"] == "hdf5" else None,
                       "selected_episode_index": output_index if keep else None,
                       "row_count": episode["row_count"], "disposition": episode["disposition"]})
        output_index += int(keep)
    return result


def _snapshots(batch, root):
    version = batch["content_version"]
    shared = {"schema_version": 1, "batch_id": batch["batch_id"], "content_version": version,
              "input_fingerprint": batch["source"]["input_fingerprint"], "source_kind": batch["source"]["kind"],
              "source_mapping": _mapping(batch), "artifact_kind": "业务记录与数据引用，非完整数据集"}
    audits = _audits(batch)
    values = {
        "clean": {"policy": "只读核验与任务级隔离；不删帧、不填值、不修复轨迹", "source_bytes_changed": False,
                  "numeric_or_image_transforms": [], "before_rows": batch["total_rows"], "after_rows": batch["total_rows"],
                  "episodes": [{"episode_index": e["episode_index"], "quality": e["quality"]} for e in batch["episodes"]]},
        "select": {"confirmed": batch["cleaning_confirmed"], "counts": batch["counts"],
                   "episodes": [{k: e[k] for k in ("episode_index", "row_count", "disposition", "selection_reason")} for e in batch["episodes"]]},
        "annotate": {"episodes": [{"source_episode_index": e["episode_index"], "disposition": e["disposition"],
                                     "annotation": e["annotation"], "submission_status": e["review"]["status"]} for e in batch["episodes"]]},
        "review": {"episodes": [{"source_episode_index": e["episode_index"], "disposition": e["disposition"],
                                   "review": e["review"]} for e in batch["episodes"]]},
    }
    names = {"clean": "清洗无变更与核验记录", "select": "筛选清单", "annotate": "标注文件", "review": "审核记录"}
    refs = {}
    for node, value in values.items():
        value = {**shared, **value, "node_id": node,
                 "business_audit": [a for a in audits if a["business_node_id"] == node]}
        path = root / "flows" / batch["batch_id"] / str(version) / f"{node}.json"
        expected = _immutable_json(path, value)
        refs[node] = artifact(path, names[node], content_version=version, expected_sha256=expected)
    return refs


def _stage_node(operation, stage):
    if operation == "import":
        return "clean" if stage in {"quality", "registration"} else "import"
    if operation == "quality":
        return "clean"
    if stage in {"final_quality", "final_registration"}:
        return "quality"
    if stage in {"package", "verify", "archive_verify"}:
        return "deliver"
    if stage == "registration":
        return "deliver" if operation in {"delivery", "pipeline", "final_quality", "finalize", "auto_flow"} else "convert"
    if stage in {"file_validation", "hdf5_validation", "field_mapping", "format_write", "official_verify", "output_quality", "conversion_registration"}:
        return "convert"
    if stage == "source_verify":
        return "quality" if operation in {"final_quality", "conversion", "pipeline", "finalize", "auto_flow"} else "deliver" if operation == "delivery" else "convert"
    return {"conversion": "convert", "delivery": "deliver", "final_quality": "quality"}.get(operation)


def _related_runs(batch, runs_root):
    from .runtime import get_run
    result = []
    for path in sorted(runs_root.glob("*/state.json")):
        state = _json(path, {})
        request = state.get("request", {})
        if (request.get("batch_id") == batch["batch_id"] or state.get("batch_id") == batch["batch_id"]
                or path.parent.name == batch["source"].get("import_run_id")):
            try:
                result.append(get_run(path.parent.name, runs_root))
            except (OSError, ValueError):
                result.append(state)
    return result


def _recorded_hash(directory, path):
    """Use the digest frozen by a trusted registration, never re-bless edits."""
    path, directory = Path(path).resolve(), Path(directory).resolve()
    if not path.is_relative_to(directory):
        return None
    relative = path.relative_to(directory).as_posix()
    records = [directory / "registration.json", *sorted((directory / "registrations").glob("*.json"))]
    for record in records:
        registration = _json(record, {})
        digest = registration.get("evidence_sha256", {}).get(relative)
        if digest:
            return digest
    return None


def _run_attempts(batch, runs, node_id, root):
    attempts = []
    for run in runs:
        request = run.get("request", {})
        members = [n for n in run.get("nodes", []) if _stage_node(request.get("operation"), n.get("node_id")) == node_id]
        if not members:
            continue
        started = [n for n in members if n.get("status") not in {"pending", "skipped"}]
        if not started:
            continue
        current = request.get("content_version", batch["content_version"]) == batch["content_version"]
        if node_id == "import":
            current = run["run_id"] == batch["source"].get("import_run_id")
        if node_id == "clean":
            current = current or run["run_id"] in {e["quality"].get("run_id") for e in batch["episodes"]}
        status = "completed"
        for candidate in ("running", "timed_out", "interrupted", "failed"):
            if any(n.get("status") == candidate for n in members):
                status = candidate
                break
        if status == "completed" and any(n.get("status") == "pending" for n in members):
            status = "running" if run.get("status") in {"running", "queued"} else "interrupted"
        artifacts = []
        for member in members:
            for path in [member.get("artifact_path"), root / run["run_id"] / "steps" / f"{member['node_id']}.json"]:
                if path and Path(path).is_file() and str(Path(path).resolve()) not in {a["path"] for a in artifacts}:
                    ref = artifact(path, "节点检查点", content_version=batch["content_version"] if current else request.get("content_version", 1),
                                   current=current, expected_sha256=_recorded_hash(root / run["run_id"], path))
                    ref["source_content_version"] = request.get("content_version", 1)
                    artifacts.append(ref)
        attention = next((n for n in members if n.get("status") == status), started[-1])
        attempts.append({"attempt_id": run["run_id"], "execution_ref": {"run_id": run["run_id"], "nodes": [n["node_id"] for n in members]},
                         "content_version": request.get("content_version", 1), "current": current,
                         "status": status if current else "superseded", "original_status": status,
                         "reason": attention.get("message", run.get("message", "")), "artifacts": artifacts,
                         "diagnostics": attention.get("diagnostics", {}), "progress": {"completed": sum(n.get("status") == "completed" for n in members), "total": len(members)}})
    return attempts


def get_flow(batch, runs_root=None, business_root=None):
    """Derive eight visible nodes and export current manual decision snapshots."""
    from .business import get_store
    root, run_root = _root(business_root), _runs_root(runs_root)
    if isinstance(batch, str):
        batch = get_store(root).get_batch(batch)
    if not re.fullmatch(r"batch_[0-9a-f]{32}", batch.get("batch_id", "")):
        raise WorkflowError("批次编号无效")
    persisted = root / f"{batch['batch_id']}.json"
    if persisted.is_file():
        batch = get_store(root).get_batch(batch["batch_id"])
    version = batch["content_version"]
    refs = _snapshots(batch, root)
    runs = _related_runs(batch, run_root)
    errors = []
    source = batch["source"]
    source_refs = []
    if source.get("manifest_path"):
        manifest = _json(source["manifest_path"], {})
        source_refs.append(artifact(source["manifest_path"], "导入输入清单", content_version=version))
        for item in manifest.get("files", []):
            if item.get("exists") is False:
                continue  # Recorded absence is evidence, never a fictitious file artifact.
            path = Path(source["managed_path"]) / "source" / item["path"]
            source_refs.append(artifact(path, "管理副本原始数据", content_version=version,
                                        expected_sha256=item.get("sha256"), expected_size=item.get("size")))
    source_ok = bool(source_refs) and all(r["valid"] for r in source_refs)
    if source_ok:
        from .importing import verify_managed_source
        try:
            verify_managed_source(source)
        except (WorkflowError, OSError, ValueError) as exc:
            source_ok = False
            errors.append(str(exc))
            for ref in source_refs:
                ref.update(valid=False, validity="输入指纹与已登记版本不一致")
    episodes = batch["episodes"]
    kept = [e for e in episodes if e["disposition"] == "keep"]
    checked = all(e["quality"]["status"] != "not_checked" for e in episodes)
    passed = [e for e in episodes if e["quality"]["status"] == "passed"]
    submitted = bool(kept) and all(e["review"]["status"] in {"submitted", "approved"} for e in kept)
    approved = bool(kept) and all(e["review"]["status"] == "approved" for e in kept)
    nodes = []
    for node_id, label in STAGES:
        history = _run_attempts(batch, runs, node_id, run_root)
        history.extend({**a, "current": a["content_version"] == version,
                        "status": "completed" if a["content_version"] == version else "superseded"}
                       for a in _audits(batch) if a["business_node_id"] == node_id)
        for path in sorted((root / "flows" / batch["batch_id"]).glob(f"*/{node_id}.json")):
            if path.parent.name.isdigit() and int(path.parent.name) != version:
                old_version = int(path.parent.name)
                history.append({"attempt_id": f"{batch['batch_id']}:v{old_version}:{node_id}",
                                "content_version": old_version, "current": False, "status": "superseded",
                                "source": "business_snapshot_export", "reason": "旧版本业务记录，已保留供追溯",
                                "artifacts": [artifact(path, "历史业务记录", content_version=old_version, current=False)]})
        nodes.append({"node_id": node_id, "label": label, "status": "waiting_upstream", "outcome": None,
                      "reason": "等待上游有效输出", "attempt_id": None, "upstream_attempt": None,
                      "content_version": version, "inputs": [], "intermediates": [], "outputs": [],
                      "history": history, "diagnostics": {}, "progress": {"completed": 0, "total": None}})
    by = {n["node_id"]: n for n in nodes}
    def finish(key, outcome, reason):
        by[key].update(status="completed", outcome=outcome, reason=reason)
    by["import"]["inputs"] = source_refs
    if source_ok:
        finish("import", "passed", "输入已复制、校验并登记；原始文件保持只读")
        by["import"]["outputs"] = source_refs
    else:
        by["import"].update(status="failed", outcome="execution_error", reason="导入文件缺失或字节已变化，请查看输入清单")
    if checked and source_ok:
        finish("clean", "passed" if passed else "rejected", f"基础核验完成：{len(passed)} 条可保留，{len(episodes)-len(passed)} 条隔离；未修改原始记录")
    elif source_ok:
        by["clean"].update(status="ready", reason="等待执行结构、数值与可读取性核验；清洗策略为只读核验与任务级隔离")
    if checked and passed and source_ok:
        if batch["cleaning_confirmed"]:
            finish("select", "passed", f"已确认 {len(kept)} 条保留任务，其余任务按清单隔离或排除")
        else:
            by["select"].update(status="waiting_human", reason="请查看保留／隔离／排除原因并确认候选集")
    if batch["cleaning_confirmed"] and source_ok:
        if submitted:
            finish("annotate", "passed", "所有保留任务已保存有效标注并提交")
        else:
            by["annotate"].update(status="waiting_human", reason="等待保留任务的标注确认、保存与提交；退回意见保留在审核记录")
        if approved:
            finish("review", "passed", "所有保留任务已审核通过；成功／失败／不确定描述机器人任务结果")
        elif any(e["review"]["status"] == "returned" for e in kept):
            finish("review", "rejected", "存在退回任务，请按审核意见修改后重新提交")
        elif any(e["review"]["status"] == "submitted" for e in kept):
            by["review"].update(status="waiting_human", reason="等待人工审核已提交的标注")
    for key, ref in refs.items():
        by[key]["intermediates"] = [ref]
        if by[key]["status"] == "completed":
            by[key]["outputs"] = [ref]
        if not ref["valid"]:
            by[key].update(status="failed", outcome="execution_error", reason="已保存业务快照与当前记录不一致，保留文件排查")
            errors.append(ref["validity"])
    for run in runs:
        if run["run_id"] in {e["quality"].get("run_id") for e in episodes}:
            report_path = run.get("artifacts", {}).get("json") or run.get("report_path")
            if report_path:
                expected = _recorded_hash(run_root / run["run_id"], report_path)
                by["clean"]["outputs"].append(artifact(report_path, "基础核验 JSON", content_version=version, current=source_ok,
                                                       expected_sha256=expected))
                if expected is None:
                    by["clean"]["outputs"][-1].update(valid=False, validity="缺少基础核验报告的登记校验值")
                if Path(report_path).with_suffix(".html").is_file() or _recorded_hash(run_root / run["run_id"], Path(report_path).with_suffix(".html")):
                    html = Path(report_path).with_suffix(".html")
                    expected_html = _recorded_hash(run_root / run["run_id"], html)
                    by["clean"]["outputs"].append(artifact(html, "基础核验 HTML", content_version=version, current=source_ok, expected_sha256=expected_html))
                    if expected_html is None:
                        by["clean"]["outputs"][-1].update(valid=False, validity="缺少基础核验 HTML 的登记校验值")
                if not all(ref["valid"] for ref in by["clean"]["outputs"]):
                    by["clean"].update(status="failed", outcome="execution_error", reason="基础核验报告缺失、已变化或缺少登记校验值，请重新核验")
            else:
                by["clean"].update(status="failed", outcome="execution_error", reason="已登记基础核验缺少报告路径，请重新核验")
    try:
        from .business import get_final_quality
        quality = get_final_quality(batch["batch_id"], business_root=root)
    except ImportError:
        quality = None
    except (WorkflowError, OSError, ValueError) as exc:
        quality = None
        errors.append(str(exc))
    if quality:
        if quality.get("current") and source_ok:
            finish("quality", "passed" if quality.get("accepted") else "rejected",
                   "最终数据与标注质检通过" if quality.get("accepted") else "最终质检完成但不通过，请查看问题报告")
        else:
            by["quality"].update(status="superseded", reason="最终质检属于旧数据或标注版本，需要重新检查")
        by["quality"]["attempt_id"] = quality.get("run_id")
        if quality.get("report_path"):
            current = by["quality"]["status"] == "completed"
            ref = artifact(quality["report_path"], "最终质检 JSON", content_version=quality.get("content_version", version), current=current,
                           expected_sha256=quality.get("report_sha256"))
            by["quality"]["outputs"].append(ref)
            html = Path(quality["report_path"]).with_suffix(".html")
            if html.is_file():
                html_hash = quality.get("report_html_sha256") or _recorded_hash(run_root / quality["run_id"], html)
                by["quality"]["outputs"].append(artifact(html, "最终质检 HTML", content_version=quality.get("content_version", version), current=current,
                                                         expected_sha256=html_hash))
                if html_hash is None:
                    by["quality"]["outputs"][-1].update(valid=False, validity="此历史 HTML 缺少保存时的校验值；请使用已校验 JSON 或重新质检")
            if current and not all(item["valid"] for item in by["quality"]["outputs"]):
                by["quality"].update(status="failed", outcome="execution_error", reason="最终质检报告已变化，不能沿用旧结论")
    elif approved and source_ok:
        by["quality"].update(status="ready", reason="人工审核已完成，可以执行最终数据与标注质检")
    for key, items, path_key, run_key in [("convert", batch.get("outputs", []), "artifact_path", "run_id"),
                                          ("deliver", batch.get("deliveries", []), "zip_path", "packaging_run_id")]:
        if items:
            item = items[-1]
            current = item["content_version"] == version and source_ok
            integrity = True
            if key == "convert" and current:
                from .dataset import dataset_fingerprint
                expected_fingerprint = item.get("validation", {}).get("output_fingerprint")
                try:
                    integrity = bool(expected_fingerprint) and dataset_fingerprint(Path(item[path_key])) == expected_fingerprint
                except (OSError, ValueError, KeyError):
                    integrity = False
            by[key].update(status="completed" if current else "superseded", outcome="passed" if current else None,
                           reason="真实产物已验证并登记" if current else "产物属于旧业务版本，请重新处理", attempt_id=item[run_key])
            path = Path(item[path_key])
            files = [path] if key == "deliver" else sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
            by[key]["outputs"] = [artifact(p, "已验证 ZIP" if key == "deliver" else "训练数据文件", content_version=item["content_version"],
                                                  current=current, expected_sha256=item.get("zip_sha256") if key == "deliver" else None) for p in files]
            if not integrity:
                for ref in by[key]["outputs"]:
                    ref.update(valid=False, validity="训练数据指纹与官方验证记录不一致")
            if current and (not files or not all(r["valid"] for r in by[key]["outputs"])):
                by[key].update(status="failed", outcome="execution_error", reason="已登记产物缺失或字节变化，禁止作为当前交付")
        elif by["quality" if key == "convert" else "convert"]["outcome"] == "passed":
            by[key].update(status="ready", reason="上游已通过，等待后台自动执行")
    # Runtime status only overrides the business node it actually reached.
    for node in nodes:
        attempts = [a for a in node["history"] if a.get("execution_ref") and a.get("current")]
        if attempts:
            attempt = attempts[-1]
            node["intermediates"].extend(attempt["artifacts"])
            node.update(progress=attempt["progress"])
            if node["attempt_id"] is None:
                node["attempt_id"] = attempt["attempt_id"]
            if attempt["status"] in {"running", "failed", "timed_out", "interrupted"}:
                node.update(status=attempt["status"], outcome="execution_error" if attempt["status"] != "running" else None,
                            reason=attempt["reason"], attempt_id=attempt["attempt_id"], diagnostics=attempt["diagnostics"])
        if node["attempt_id"] is None and node["intermediates"]:
            node["attempt_id"] = f"{batch['batch_id']}:v{version}:{node['node_id']}"
        position = nodes.index(node)
        if position:
            upstream = nodes[position-1]
            node["inputs"] = list(upstream["outputs"])
            node["upstream_attempt"] = upstream["attempt_id"]
            if (upstream["status"] != "completed" or upstream["outcome"] != "passed") and node["outcome"] == "passed":
                node.update(status="superseded", outcome=None, reason=f"上游{upstream['label']}尚无当前有效输出；原产物仅供历史追溯")
                for ref in node["outputs"]:
                    ref.update(current=False, valid=False, validity="上游证据失效，不能作为当前输出下载")
            if node["status"] == "waiting_upstream":
                node["reason"] = f"等待{upstream['label']}的当前有效输出：{upstream['reason']}"
        node["status_label"] = STATUS_LABELS[node["status"]]
    return {"schema_version": 1, "batch_id": batch["batch_id"], "label": batch["label"], "content_version": version,
            "revision": batch["revision"], "nodes": nodes, "errors": errors,
            "authority": "批次版本与人工审计由业务记录负责；进程与执行结果由运行记录负责；此视图为派生结果"}


def download_artifact(batch_id, ref, *, business_root=None, runs_root=None):
    """Recheck a displayed reference at click time; never download stale data."""
    from .business import get_store
    batch = get_store(business_root).get_batch(batch_id)
    if not ref.get("current") or ref.get("content_version") != batch["content_version"]:
        raise ConflictError("此文件属于历史业务版本，请刷新后下载当前产物")
    if not ref.get("valid"):
        raise WorkflowError("文件尚未验证、已变化或不存在，无法下载")
    path = Path(ref["path"]).resolve()
    roots = [_root(business_root), _runs_root(runs_root)]
    # Explicit run-root references are bound by the actual digest and returned
    # from get_flow; reject unrelated file paths passed to this public adapter.
    if not any(path.is_relative_to(root) for root in roots):
        raise WorkflowError("文件不在本项目管理的产物目录中")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise WorkflowError(f"产物文件无法读取，请刷新并查看节点日志：{path.name}") from exc
    if len(data) != ref.get("size") or hashlib.sha256(data).hexdigest() != ref.get("sha256"):
        raise WorkflowError("文件在页面加载后发生变化，请刷新并重新验证")
    return data


def read_flow_events(batch, runs_root=None, *, node_id=None, episode_index=None, level=None, attempt_id=None):
    from .runtime import read_events
    events = []
    for event in _audits(batch):
        detail = event.get("detail", {})
        events.append({**event, "node_id": event["business_node_id"], "episode_index": detail.get("episode_index"),
                       "level": "WARNING" if detail.get("decision") == "returned" else "INFO",
                       "message": {"create_batch": "导入已登记", "apply_quality_report": "基础核验结果已回写", "set_disposition": "任务筛选已更新",
                                   "confirm_cleaning": "候选集已确认", "save_annotation": "标注已保存", "submit_annotation": "标注已提交",
                                   "review_annotation": "审核决定已保存", "register_verified_output": "转换产物已登记", "register_delivery": "交付包已登记"}.get(event["operation"], event["operation"])})
    root = _runs_root(runs_root)
    for run in _related_runs(batch, root):
        for event in read_events(run["run_id"], root):
            events.append({**event, "source": "runtime", "attempt_id": run["run_id"],
                           "business_node_id": _stage_node(run.get("request", {}).get("operation"), event.get("node_id"))})
    return [e for e in sorted(events, key=lambda e: e.get("at_utc", e.get("timestamp", "")))
            if (node_id is None or e.get("business_node_id") == node_id)
            and (episode_index is None or e.get("episode_index") == episode_index)
            and (level is None or e.get("level") == level)
            and (attempt_id is None or e.get("attempt_id") == attempt_id)]


def events_jsonl(batch, runs_root=None, **filters):
    return "".join(json.dumps(e, ensure_ascii=False, allow_nan=False) + "\n" for e in read_flow_events(batch, runs_root, **filters))
