"""Deterministic, explainable checks on raw demonstration records.

Video timestamps establish file-level sample correspondence only. They do not
establish hardware synchronization, action causality, or motion correctness.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from .config import APP_VERSION, CAMERA, DEFAULT_RULE_PARAMS, EPISODES, REPO_ID, REVISION, RULE_VERSION
from .errors import error_details, explain_error
from .models import Episode
from .video import inspect_video, episode_video_segment

REQUIRED_FIELDS = ("episode_index", "frame_index", "timestamp", "observation.state", "action")
RULES = (
    "required_fields", "row_count", "episode_identity", "vector_shape", "numeric_finite",
    "frame_index", "timestamp_monotonic", "timestamp_interval", "video_decode",
    "video_frame_count", "video_timestamp_mapping",
)
RULE_LABELS = {
    "required_fields": "必需字段", "row_count": "任务长度", "episode_identity": "任务编号",
    "vector_shape": "状态与动作维度", "numeric_finite": "数值有效性", "frame_index": "帧索引连续性",
    "timestamp_monotonic": "时间单调性", "timestamp_interval": "采样时间间隔",
    "video_decode": "视频完整解码", "video_frame_count": "视频帧数",
    "video_timestamp_mapping": "视频与样本时间对应", "episode_load": "任务读取",
    "table_checks": "表格质检执行", "video_checks": "视频关联检查执行",
    "image_shape": "原始图像形状", "image_frame_count": "图像与记录数量", "image_read": "原始图像读取",
    "candidate_nonempty": "候选集非空", "selection_confirmed": "筛选已确认",
    "basic_coverage": "基础检查完整性", "basic_result": "基础数据检查结论",
    "task_text": "任务描述完整性", "outcome": "执行结果标注", "tags": "标签格式",
    "review_approved": "人工审核状态", "mapping_count": "候选映射与数量",
}


def json_safe(value: Any) -> Any:
    """Keep strict JSON, including evidence about NaN and infinities."""
    if isinstance(value, np.ndarray):
        return json_safe(value.tolist())
    if isinstance(value, np.generic):
        return json_safe(value.item())
    if isinstance(value, Path):
        return value.name
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def normalize_params(params: dict | None = None) -> dict[str, float]:
    defaults = {**DEFAULT_RULE_PARAMS, "video_timestamp_tolerance_frames": 0.5}
    supplied = params or {}
    unknown = set(supplied) - set(defaults)
    if unknown:
        raise ValueError(f"未知质检参数：{', '.join(sorted(unknown))}")
    try:
        normalized = {key: float(supplied.get(key, value)) for key, value in defaults.items()}
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("质检容差必须是有限且不小于零的数值") from exc
    if any(not math.isfinite(value) or value < 0 for value in normalized.values()):
        raise ValueError("质检容差必须是有限且不小于零的数值")
    return normalized


def _frame_at(episode: Episode, row_index: int | None) -> int | None:
    if row_index is None or "frame_index" not in episode.table:
        return None
    try:
        value = float(episode.table.iloc[row_index]["frame_index"])
        return int(value) if math.isfinite(value) and value.is_integer() else None
    except (TypeError, ValueError, OverflowError):
        return None


def _finite_vector(value: Any) -> np.ndarray | None:
    try:
        array = np.asarray(value, dtype=float)
        return array if np.isfinite(array).all() else None
    except (TypeError, ValueError, OverflowError):
        return None


def run_checks(episode: Episode, params: dict | None = None, check_video: bool = True,
               stage_callback=None) -> dict:
    """Check one episode without changing its data. Positions are zero-based."""
    options = normalize_params(params)
    table = episode.table
    issues: list[dict] = []
    coverage: list[dict] = []
    video: dict = {"status": "not_checked"}

    def issue(rule: str, message: str, evidence: Any = None, row: int | None = None,
              category: str = "data_issue", severity: str = "error") -> None:
        issues.append({"episode_index": episode.episode_index, "row_index": row,
                       "frame_index": _frame_at(episode, row), "rule": rule, "severity": severity,
                       "category": category, "message": message, "evidence": json_safe(evidence or {}),
                       "node_id": "video_decode" if rule == "video_decode" else
                                  "video_checks" if rule.startswith("video_") else "table_checks"})

    def covered(rule: str, detail: str = "") -> None:
        coverage.append({"rule": rule, "status": "performed", "detail": detail})

    def skipped(rule: str, reason: str, report_issue: bool = True) -> None:
        coverage.append({"rule": rule, "status": "unavailable", "detail": reason})
        if report_issue:
            issue(rule, reason, category="not_checked", severity="info")

    def snapshot() -> dict:
        performed = {item["rule"] for item in coverage if item["status"] == "performed"}
        status = "incomplete" if len(performed) != len(RULES) else "issues" if issues else "passed"
        return json_safe({"episode_index": episode.episode_index, "row_count": len(table),
                          "expected_length": episode.expected_length, "status": status,
                          "issues": issues, "coverage": coverage, "video": video})

    def stage(name: str, event: str, **details) -> None:
        if stage_callback:
            stage_callback(name, event, {"result": snapshot(), **details})

    stage("table_checks", "started", input_path=str(episode.data_path))
    missing = [field for field in REQUIRED_FIELDS if field not in table]
    covered("required_fields")
    if missing:
        issue("required_fields", "缺少必需字段", {"missing_fields": missing})
    covered("row_count")
    if len(table) != episode.expected_length:
        issue("row_count", "任务实际长度与源元数据不一致",
              {"expected": episode.expected_length, "actual": len(table)})
    if len(table) == 0:
        issue("row_count", "任务没有可检查的样本", {"actual": 0})

    # Validate scalars independently from time/frame continuity checks.
    numeric_columns = [field for field in REQUIRED_FIELDS if field in table]
    if numeric_columns and len(table):
        covered("numeric_finite")
        for field in numeric_columns:
            for row, value in enumerate(table[field]):
                array = _finite_vector(value)
                scalar = field in ("episode_index", "frame_index", "timestamp")
                if array is None or (scalar and array.ndim != 0):
                    issue("numeric_finite", "字段含非数值、非有限值或无效标量",
                          {"field": field, "value": value}, row)
    else:
        skipped("numeric_finite", "没有可读取的数值字段或样本")

    if "episode_index" in table and len(table):
        covered("episode_identity")
        for row, value in enumerate(table["episode_index"]):
            array = _finite_vector(value)
            if array is not None and array.ndim == 0 and float(array) != episode.episode_index:
                issue("episode_identity", "样本任务编号与当前任务不一致",
                      {"expected": episode.episode_index, "actual": value}, row)
    else:
        skipped("episode_identity", "缺少任务编号字段或样本")

    vector_fields = [field for field in ("observation.state", "action") if field in table]
    if vector_fields and len(table):
        covered("vector_shape", "按来源 features.shape 检查；缺失时使用约定的 6 维")
        features = episode.metadata.get("info", {}).get("features", {})
        for field in vector_fields:
            expected_shape = tuple(features.get(field, {}).get("shape", [6]))
            for row, value in enumerate(table[field]):
                try:
                    shape = np.asarray(value).shape
                except (ValueError, TypeError):
                    shape = None
                if shape != expected_shape:
                    issue("vector_shape", "字段维度与声明不一致",
                          {"field": field, "expected_shape": expected_shape, "actual_shape": shape}, row)
    else:
        skipped("vector_shape", "缺少状态和动作字段或样本")

    def scalar_column(name: str) -> np.ndarray | None:
        if name not in table or not len(table):
            return None
        values = [_finite_vector(value) for value in table[name]]
        if any(value is None or value.ndim != 0 for value in values):
            return None
        return np.array([float(value) for value in values], dtype=float)

    frames = scalar_column("frame_index")
    if frames is None:
        skipped("frame_index", "缺少有限的标量帧索引；参见字段和数值检查")
    else:
        covered("frame_index", "每条任务从 0 开始、以 1 连续递增")
        for row, value in enumerate(frames):
            if value != row:
                issue("frame_index", "帧索引不符合任务内连续序列",
                      {"expected": row, "actual": value}, row)

    timestamps = scalar_column("timestamp")
    if timestamps is None:
        skipped("timestamp_monotonic", "缺少有限的标量时间值；参见字段和数值检查")
        skipped("timestamp_interval", "缺少有限的标量时间值；参见字段和数值检查")
    else:
        covered("timestamp_monotonic", "时间必须非负，仅比较同一任务内相邻样本")
        for row in np.flatnonzero(timestamps < 0):
            issue("timestamp_monotonic", "任务时间值为负，无法作为浏览时间",
                  {"current": timestamps[row], "minimum_seconds": 0}, int(row))
        deltas = np.diff(timestamps)
        for row in np.flatnonzero(deltas <= 0) + 1:
            issue("timestamp_monotonic", "时间值重复或倒退",
                  {"previous": timestamps[row - 1], "current": timestamps[row]}, int(row))
        if not math.isfinite(episode.fps) or episode.fps <= 0:
            skipped("timestamp_interval", "来源未提供有效采样频率")
        else:
            expected = 1.0 / episode.fps
            tolerance = expected * options["interval_relative_tolerance"] + options["timestamp_absolute_tolerance"]
            covered("timestamp_interval", f"期望间隔 {expected:.8g} 秒，容差 {tolerance:.8g} 秒")
            for row in np.flatnonzero((deltas > 0) & (np.abs(deltas - expected) > tolerance)) + 1:
                issue("timestamp_interval", "相邻样本时间间隔超出容差",
                      {"expected_seconds": expected, "actual_seconds": deltas[row - 1],
                       "tolerance_seconds": tolerance}, int(row))

    stage("table_checks", "completed")
    if not check_video:
        for rule in ("video_decode", "video_frame_count", "video_timestamp_mapping"):
            skipped(rule, "此次运行未启用视频检查")
        stage("video_decode", "skipped", message="此次运行未启用视频检查")
        stage("video_checks", "skipped", message="此次运行未启用视频检查")
    else:
        stage("video_decode", "started", input_path=str(episode.video_path))
        try:
            if not episode.video_path.is_file():
                raise FileNotFoundError("主相机视频文件不存在")
            decoded = episode_video_segment(inspect_video(episode.video_path), episode)
            video = {key: json_safe(value) for key, value in decoded.items() if key != "pts_times"}
            video["status"] = "passed"
            covered("video_decode", "已解码主相机视频并检查本任务对应片段")
        except Exception as exc:
            video = {"status": "failed", "error": explain_error(exc, "读取主相机视频"),
                     "diagnostics": error_details(exc, include_traceback=True)}
            issue("video_decode", video["error"],
                  {"error": video["error"], "diagnostics": video["diagnostics"]},
                  category="load_failure")
            skipped("video_decode", "视频缺失或完整解码失败", report_issue=False)
            skipped("video_frame_count", "视频读取失败，无法检查帧数")
            skipped("video_timestamp_mapping", "视频读取失败，无法检查文件内时间对应")
            stage("video_decode", "failed", message=video["error"], diagnostics=video["diagnostics"])
            stage("video_checks", "skipped", message="视频读取失败，无法执行关联检查")
        else:
            stage("video_decode", "completed")
            stage("video_checks", "started", input_path=str(episode.video_path))
            covered("video_frame_count")
            if decoded["frame_count"] != len(table):
                issue("video_frame_count", "视频帧数与样本行数不一致",
                      {"video_frames": decoded["frame_count"], "table_rows": len(table)})
                video["status"] = "failed"
            pts = _finite_vector(decoded.get("pts_times", []))
            if timestamps is None or pts is None or pts.ndim != 1 or len(pts) != len(table) or not len(table):
                skipped("video_timestamp_mapping", "视频时间或样本时间不可用，或两者数量不一致")
            elif not math.isfinite(episode.fps) or episode.fps <= 0:
                skipped("video_timestamp_mapping", "来源未提供有效采样频率")
            else:
                tolerance = max(options["timestamp_absolute_tolerance"],
                                options["video_timestamp_tolerance_frames"] / episode.fps)
                errors = np.abs(pts - timestamps)
                # Match read_video_frame's range guard, not the much larger
                # half-frame correspondence tolerance. A close timestamp may
                # still lie outside the range the browser can actually read.
                range_epsilon = max(1e-5, 0.001 / (decoded.get("fps") or 30))
                outside_range = (timestamps < pts[0] - range_epsilon) | (timestamps > pts[-1] + range_epsilon)
                covered("video_timestamp_mapping", f"文件内对应容差 {tolerance:.8g} 秒；读取边界容差 {range_epsilon:.8g} 秒；不代表硬件同步")
                video["mapping_max_error_seconds"] = float(errors.max())
                video["mapping_tolerance_seconds"] = tolerance
                video["readable_range_seconds"] = [float(pts[0]), float(pts[-1])]
                video["readable_range_epsilon_seconds"] = range_epsilon
                video["sample_references"] = [
                    {"row_index": row, "frame_index": _frame_at(episode, row),
                     "timestamp": float(timestamps[row]), "pts_time": float(pts[row]),
                     "error_seconds": float(errors[row])}
                    for row in sorted({0, len(table) // 2, len(table) - 1})
                ]
                for row in np.flatnonzero((errors > tolerance) | outside_range):
                    reasons = []
                    if errors[row] > tolerance:
                        reasons.append("same_index_time_error")
                    if outside_range[row]:
                        reasons.append("outside_readable_video_range")
                    issue("video_timestamp_mapping", "视频帧与样本的时间差超出容差，或样本时间超出视频可读取范围",
                          {"timestamp": timestamps[row], "pts_time": pts[row],
                           "error_seconds": errors[row], "tolerance_seconds": tolerance,
                           "reasons": reasons, "video_first_pts": pts[0], "video_last_pts": pts[-1],
                           "readable_range_epsilon_seconds": range_epsilon}, int(row))
                    video["status"] = "failed"
            stage("video_checks", "completed")

    return snapshot()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_batch(root: Path, episode_indices=None, params: dict | None = None,
              check_video: bool = True, source_kind: str = "original_public") -> dict:
    """Inspect episodes independently and keep provenance and failures visible."""
    from .dataset import dataset_fingerprint, load_metadata
    from . import dataset
    from .source import DEMO_MANIFEST_NAME, MANIFEST_NAME, SAMPLE_FILES

    data_root = Path(root)
    options = normalize_params(params)
    if source_kind not in {"original_public", "injected_test", "converted_public"}:
        raise ValueError("来源类型无效：仅支持公开原始样本、故障注入测试副本或已转换公开样本")
    demo_manifest = None
    if (data_root / DEMO_MANIFEST_NAME).is_file():
        from .demo import read_demo_manifest
        demo_manifest = read_demo_manifest(data_root)
        source_kind = "injected_test"
    started = datetime.now(timezone.utc).isoformat()
    try:
        metadata = load_metadata(data_root)
    except Exception:
        if episode_indices is None:
            raise
        metadata = {}
    chosen = episode_indices if episode_indices is not None else metadata.get("episode_indices", EPISODES)
    indices = tuple(int(index) for index in chosen)
    if not indices or len(indices) != len(set(indices)):
        raise ValueError("任务编号必须非空且不能重复")
    fingerprint = dataset_fingerprint(data_root)
    manifest = metadata.get("manifest", {})
    source = metadata.get("source") or {"repo_id": REPO_ID, "revision": REVISION,
              "url": f"https://huggingface.co/datasets/{REPO_ID}/tree/{REVISION}",
              "license": "Apache-2.0（来源 README 声明）", "camera": CAMERA, "format_version": "v2.0"}
    source_kind = "converted_public" if source.get("format_version") == "v3.0" and source_kind == "original_public" else source_kind
    manifest_files = manifest.get("files", [])
    expected_hashes = {item.get("path"): item.get("sha256") for item in manifest_files if isinstance(item, dict)} if isinstance(manifest_files, list) else {}
    input_files = []
    names = dataset.input_relative_paths(data_root, metadata) if hasattr(dataset, "input_relative_paths") else (*SAMPLE_FILES, MANIFEST_NAME, DEMO_MANIFEST_NAME)
    for name in names:
        file = data_root / name
        entry = {"path": str(name).replace("\\", "/"), "status": "present" if file.is_file() else "missing"}
        if file.is_file():
            entry.update(size_bytes=file.stat().st_size, sha256=_sha256(file))
        if name not in (MANIFEST_NAME, DEMO_MANIFEST_NAME):
            expected_hash = expected_hashes.get(str(name))
            entry["manifest_sha256"] = expected_hash
            entry["hash_matches_manifest"] = entry.get("sha256") == expected_hash if expected_hash else None
        input_files.append(entry)
    source_inputs = [item for item in input_files if item["path"] not in (MANIFEST_NAME, DEMO_MANIFEST_NAME)]
    manifest_source = manifest.get("source", manifest)
    manifest_version_matches = manifest_source.get("repo_id") == source.get("repo_id") and manifest_source.get("revision") == source.get("revision")
    if any(item["status"] == "missing" or item.get("hash_matches_manifest") is False for item in source_inputs):
        integrity_status = "differs_from_manifest"
    elif manifest_version_matches and all(item.get("hash_matches_manifest") is True for item in source_inputs):
        integrity_status = "matches_manifest"
    else:
        integrity_status = "unverified"

    return {"data_root": str(data_root), "options": options, "indices": indices,
            "check_video": check_video, "source_kind": source_kind,
            "manifest": manifest, "source": source, "input_files": input_files, "demo_manifest": demo_manifest,
            "integrity_status": integrity_status, "fingerprint": fingerprint, "started": started}


def failed_episode(index: int, exc: BaseException, stage: str = "table_read",
                   checkpoint: dict | None = None, message: str | None = None) -> dict:
    """Preserve completed rules after a process/node fails; never mark missing checks passed."""
    from copy import deepcopy
    result = deepcopy(checkpoint) if checkpoint else {
        "episode_index": index, "row_count": 0, "expected_length": None,
        "issues": [], "coverage": [], "video": {"status": "not_checked"}}
    context = {"table_read": "读取任务数据", "table_checks": "执行表格质检",
               "video_decode": "读取主相机视频", "video_checks": "执行视频关联检查"}.get(stage, "执行任务节点")
    text = message or explain_error(exc, context)
    rule = {"table_read": "episode_load", "table_checks": "table_checks",
            "video_decode": "video_decode", "video_checks": "video_checks"}.get(stage, stage)
    result["issues"].append({"episode_index": index, "row_index": None, "frame_index": None,
        "rule": rule, "node_id": stage, "severity": "error", "category": "load_failure",
        "message": text + " 其余任务继续检查。",
        "evidence": {"error": text, "diagnostics": error_details(exc, include_traceback=True)}})
    present = {item["rule"] for item in result["coverage"]}
    for item in RULES:
        if item not in present:
            result["coverage"].append({"rule": item, "status": "unavailable", "detail": text})
            if item != rule:
                result["issues"].append({"episode_index": index, "row_index": None, "frame_index": None,
                    "rule": item, "severity": "info", "category": "not_checked",
                    "message": "前置节点未完成，未执行此项检查", "evidence": {"node_id": stage}})
    result["status"] = "incomplete" if checkpoint else "load_failed"
    if stage in {"video_decode", "video_checks"}:
        result["video"]["status"] = "failed"
    return json_safe(result)


def run_batch(root: Path, episode_indices=None, params: dict | None = None,
              check_video: bool = True, source_kind: str = "original_public") -> dict:
    """Synchronous compatibility entry point, using the same checks and report assembly."""
    from .dataset import load_episode
    context = prepare_batch(root, episode_indices, params, check_video, source_kind)
    started = time.perf_counter()
    episodes = []
    for index in context["indices"]:
        current = "table_read"
        checkpoint = None
        def callback(stage, event, details):
            nonlocal current, checkpoint
            current = stage
            if event in {"completed", "failed", "skipped"}:
                checkpoint = details["result"]
        try:
            episode = load_episode(Path(root), index)
            episodes.append(run_checks(episode, context["options"], check_video, callback))
        except Exception as exc:
            episodes.append(failed_episode(index, exc, current, checkpoint))
    return assemble_report(context, episodes, runtime_seconds=time.perf_counter() - started)


def _stable_result(value):
    if isinstance(value, dict):
        return {key: _stable_result(item) for key, item in value.items()
                if key not in {"diagnostics", "traceback", "run_id", "execution", "started_at_utc", "runtime_seconds"}}
    if isinstance(value, list):
        return [_stable_result(item) for item in value]
    return value


def assemble_report(context: dict, episodes: list[dict], runtime_seconds: float = 0) -> dict:
    """Assemble only saved results; never reload or recheck any episode."""
    options, indices = context["options"], context["indices"]
    check_video, source_kind = context["check_video"], context["source_kind"]
    manifest, input_files = context["manifest"], context["input_files"]
    demo_manifest, integrity_status = context["demo_manifest"], context["integrity_status"]
    fingerprint = context["fingerprint"]
    issues = [item for episode in episodes for item in episode["issues"]]
    coverage = [item for episode in episodes for item in episode["coverage"]]
    summary = {"episode_count": len(episodes), "row_count": sum(item["row_count"] for item in episodes),
               "issue_count": len(issues),
               "data_issue_count": sum(item["category"] == "data_issue" for item in issues),
               "load_failure_count": sum(item["category"] == "load_failure" for item in issues),
               "not_checked_count": sum(item["category"] == "not_checked" for item in issues),
               "passed_episode_count": sum(item["status"] == "passed" for item in episodes),
               "coverage_performed": sum(item["status"] == "performed" for item in coverage),
               "coverage_total": len(coverage)}
    report = json_safe({"schema_version": "1.0", "app_version": APP_VERSION, "source": {**context.get("source", {}),
                        "source_kind": source_kind, "manifest": manifest, "input_files": input_files,
                        "demo_manifest": demo_manifest,
                        "integrity_status": integrity_status,
                        "integrity_note": "比较当前文件与本地下载清单；本次质检没有重新进行在线来源校验"},
                       "input_fingerprint": fingerprint, "rule_version": RULE_VERSION, "params": options,
                       "check_video": check_video, "episode_indices": indices,
                       "summary": summary, "episodes": episodes, "issues": issues,
                       "limitations": ["仅检查记录及文件内视频时间对应，不代表硬件同步或双相机同步",
                                       "未判断动作控制含义、物理安全性、任务成功率或训练适用性"]})
    digest_data = json.dumps(_stable_result(report), ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
    report["result_digest"] = hashlib.sha256(digest_data.encode("utf-8")).hexdigest()
    report["started_at_utc"] = context["started"]
    report["runtime_seconds"] = round(runtime_seconds, 6)
    return report
