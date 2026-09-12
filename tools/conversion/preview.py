"""Read-only preview of the fixed robomimic HDF5 profile, before conversion."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time
import traceback

from worker import (DEMOS, FPS, STATE_FIELDS, STATE_NAMES, ACTION_NAMES, ConversionError,
                    validate_file, fail, utc_now, write_json)

RULES = ("required_fields", "row_count", "vector_shape", "numeric_finite", "frame_index",
         "timestamp_monotonic", "timestamp_interval", "image_shape", "image_frame_count", "image_read")
RULE_VERSION = "hdf5-preview-0.4.0"
IMAGE_FIELD = "obs/agentview_image"


def safe(value):
    import numpy as np
    if isinstance(value, dict):
        return {str(k): safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe(v) for v in value]
    if isinstance(value, np.ndarray):
        return safe(value.tolist())
    if isinstance(value, np.generic):
        return safe(value.item())
    if isinstance(value, float) and not np.isfinite(value):
        return str(value)
    return value


def preview_episode(source, data, demo, expected_length, output):
    import h5py
    import numpy as np
    import pandas as pd

    episode_index = int(demo.split("_")[1])
    issues, coverage, values = [], {}, {}
    result = {"episode_index": episode_index, "source_demo": demo, "row_count": expected_length,
              "expected_length": expected_length, "actual_row_count": 0, "table_path": None,
              "images_path": None, "issues": issues, "coverage": [], "status": "incomplete"}

    def issue(rule, message, field=None, row=None, evidence=None, category="data_issue"):
        issues.append({"episode_index": episode_index, "row_index": row, "frame_index": row,
                       "rule": rule, "category": category, "severity": "info" if category == "not_checked" else "error",
                       "message": message, "node_id": "preview", "file": str(source), "field": field,
                       "evidence": {"file": str(source), "field": f"/data/{demo}/{field}" if field else f"/data/{demo}", **safe(evidence or {})}})

    def covered(rule, detail=""):
        coverage[rule] = {"rule": rule, "status": "performed", "detail": detail}

    def skipped(rule, detail):
        coverage[rule] = {"rule": rule, "status": "unavailable", "detail": detail}
        issue(rule, detail, category="not_checked")

    def finish():
        for rule in RULES:
            if rule not in coverage:
                skipped(rule, "任务结构无法读取，此项未检查。")
        result["coverage"] = [coverage[rule] for rule in RULES]
        result["status"] = ("load_failed" if any(i["category"] == "load_failure" for i in issues)
                            else "incomplete" if any(c["status"] != "performed" for c in coverage.values())
                            else "issues" if issues else "passed")
        return result

    if demo not in data or not isinstance(data[demo], h5py.Group):
        covered("required_fields")
        issue("required_fields", "任务分组缺失或不是 HDF5 数据分组。", category="load_failure")
        return finish()
    group = data[demo]
    required = [*STATE_FIELDS, "actions", IMAGE_FIELD]
    missing = [field for field in required if field not in group or not isinstance(group[field], h5py.Dataset)]
    covered("required_fields")
    if missing:
        issue("required_fields", "原始任务缺少必需数组；预览保留其他可读字段，不自动补值。", evidence={"missing_fields": missing})
    for field in required:
        if field in missing:
            continue
        try:
            values[field] = group[field][:]
        except Exception as exc:
            issue("image_read" if field == IMAGE_FIELD else "required_fields", "原始数组读取失败，其他任务继续预览。", field,
                  category="load_failure", evidence={"exception_type": type(exc).__name__, "exception_message": str(exc), "traceback": traceback.format_exc()})
    actions = values.get("actions")
    available_lengths = [len(a) for a in values.values() if a.ndim > 0]
    length = len(actions) if actions is not None and actions.ndim > 0 else (available_lengths[0] if available_lengths else 0)
    result["actual_row_count"] = length
    if actions is not None and actions.ndim > 0:
        covered("row_count")
        declared = int(group.attrs.get("num_samples", -1))
        if length != expected_length or declared != length:
            issue("row_count", "实际动作记录数与固定配置或任务元数据不一致。", "actions",
                  evidence={"expected": expected_length, "actual": length, "declared_num_samples": declared})
    else:
        skipped("row_count", "动作字段不可读，无法确认原始任务记录数。")

    numeric_fields = {**STATE_FIELDS, "actions": 7}
    if all(field in values for field in numeric_fields):
        covered("vector_shape")
        covered("numeric_finite")
    else:
        skipped("vector_shape", "状态或动作字段缺失，维度检查覆盖不完整。")
        skipped("numeric_finite", "状态或动作字段缺失，数值检查覆盖不完整；已检查可读字段。")
    for field, dim in numeric_fields.items():
        if field not in values:
            continue
        array = values[field]
        if array.shape != (length, dim):
            issue("vector_shape", "原始状态／动作数组的维度或记录长度不符合映射。", field,
                  evidence={"expected_shape": [length, dim], "actual_shape": list(array.shape)})
        if array.dtype.kind not in "fiu":
            issue("numeric_finite", "原始状态／动作字段不是数值数组。", field, evidence={"dtype": str(array.dtype)})
            continue
        bad = np.argwhere(~np.isfinite(array))
        for location in bad[:100]:
            location = tuple(int(v) for v in location)
            issue("numeric_finite", "原始状态／动作包含 NaN 或无穷值。", field, location[0] if location else None,
                  evidence={"column": location[1] if len(location) > 1 else None, "value": str(array[location])})
        if len(bad) > 100:
            issue("numeric_finite", "该字段异常数量较多，逐行证据显示前 100 个。", field, evidence={"total_invalid_values": len(bad)})

    # These indices and times are clearly derived preview coordinates, not source clocks.
    frame_index = np.arange(length, dtype=np.int64)
    timestamps = frame_index.astype(np.float64) / FPS
    table = {"episode_index": np.full(length, episode_index), "frame_index": frame_index, "timestamp": timestamps}
    for rule in ("frame_index", "timestamp_monotonic", "timestamp_interval"):
        if length:
            covered(rule, "检查派生预览坐标：frame_index 从 0 开始，timestamp = frame_index / 20；不验证源硬件时间。")
        else:
            skipped(rule, "没有可读样本，无法生成和检查预览索引／时间。")
    if all(field in values and values[field].shape == (length, dim) and values[field].dtype.kind in "fiu" for field, dim in STATE_FIELDS.items()):
        table["observation.state"] = list(np.concatenate([values[field] for field in STATE_FIELDS], axis=1))
    if actions is not None and actions.shape == (length, 7) and actions.dtype.kind in "fiu":
        table["action"] = list(actions)
    table_path = Path("episodes") / f"episode_{episode_index:06d}.parquet"
    (output / table_path).parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(table).to_parquet(output / table_path, index=False)
    result["table_path"] = table_path.as_posix()

    images = values.get(IMAGE_FIELD)
    if images is None:
        for rule in ("image_read", "image_shape", "image_frame_count"):
            skipped(rule, "相机数组缺失或无法读取，此项未检查。")
    else:
        covered("image_read", "已读取全部所选任务原始图像数组。")
        covered("image_shape")
        covered("image_frame_count")
        valid_shape = images.ndim == 4 and images.shape[1:] == (84, 84, 3) and images.dtype == np.dtype("uint8")
        if not valid_shape:
            issue("image_shape", "主相机必须为 N × 84 × 84 × 3 的 uint8 RGB 数组。", IMAGE_FIELD,
                  evidence={"actual_shape": list(images.shape), "dtype": str(images.dtype)})
        if images.ndim == 0 or len(images) != length:
            issue("image_frame_count", "相机图像数与动作记录数不一致。", IMAGE_FIELD,
                  evidence={"expected": length, "actual": len(images) if images.ndim else 0})
        if valid_shape:
            images_path = Path("images") / f"episode_{episode_index:06d}.npz"
            (output / images_path).parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(output / images_path, images=images)
            result["images_path"] = images_path.as_posix()
    return finish()


def build_preview(source: Path, output: Path, test_mode: str | None = None):
    import h5py
    started = time.monotonic()
    if test_mode is not None and not test_mode.strip():
        fail("test_label_missing", "故障副本必须填写明确的测试标记。", file=str(source))
    provenance = validate_file(source, test_mode)
    try:
        handle = h5py.File(source, "r")
    except Exception as exc:
        fail("hdf5_unreadable", "HDF5 文件无法打开，请查看文件来源和原始错误。", file=str(source), evidence={"exception_type": type(exc).__name__, "exception_message": str(exc)})
    with handle:
        if "data" not in handle or not isinstance(handle["data"], h5py.Group):
            fail("hdf5_missing_data", "HDF5 缺少 data 分组，无法识别导入配置。", file=str(source), field="/data")
        data = handle["data"]
        try:
            env_raw = data.attrs["env_args"]
            env = json.loads(env_raw.decode("utf-8") if isinstance(env_raw, bytes) else env_raw)
        except Exception as exc:
            fail("hdf5_env_invalid", "HDF5 缺少有效环境说明，无法核实预览配置。", file=str(source), field="/data@env_args", evidence={"error": str(exc)})
        if env.get("env_name") != "Lift" or env.get("env_kwargs", {}).get("control_freq") != FPS:
            fail("hdf5_profile_mismatch", "预览只支持已验证的 Panda / Lift / 20 Hz 仿真测试配置。", file=str(source))
        demos = [k for k in data if k.startswith("demo_") and isinstance(data[k], h5py.Group)]
        actual_total = sum(data[k]["actions"].shape[0] for k in demos if "actions" in data[k] and isinstance(data[k]["actions"], h5py.Dataset) and data[k]["actions"].ndim > 0)
        declared_total = int(data.attrs.get("total", -1))
        warnings = []
        if declared_total != actual_total:
            warnings.append({"code": "source_total_mismatch", "message": f"源全局统计与实际动作记录数不一致；保留来源警告，批次按 {'、'.join(DEMOS)} 清单重新统计。", "declared_total": declared_total, "actual_total": actual_total})
        output.mkdir(parents=True, exist_ok=True)
        episodes = []
        for demo, length in DEMOS.items():
            try:
                episodes.append(preview_episode(source, data, demo, length, output))
            except Exception as exc:
                # A corrupt individual task must remain visible while other tasks
                # are extracted. Top-level format/environment failures still fail.
                episode_index = int(demo.split("_")[1])
                evidence = {"file": str(source), "field": f"/data/{demo}", "exception_type": type(exc).__name__,
                            "exception_message": str(exc), "traceback": traceback.format_exc()}
                base = {"episode_index": episode_index, "row_index": None, "frame_index": None,
                        "node_id": "preview", "severity": "error", "evidence": evidence}
                issues = [{**base, "rule": "required_fields", "category": "load_failure",
                           "message": "该任务预览执行失败，已保存原始诊断；其余任务继续。"}]
                issues.extend({**base, "rule": rule, "category": "not_checked", "severity": "info",
                               "message": "该任务预览执行失败，此项未检查。"} for rule in RULES)
                episodes.append({"episode_index": episode_index, "source_demo": demo, "row_count": length,
                                 "expected_length": length, "actual_row_count": 0, "table_path": None, "images_path": None,
                                 "status": "load_failed", "issues": issues,
                                 "coverage": [{"rule": rule, "status": "unavailable", "detail": "任务预览执行失败。"} for rule in RULES]})
    metadata = {"schema_version": 1, "kind": "hdf5_preview", "source": provenance, "fps": FPS,
                "state_names": STATE_NAMES, "action_names": ACTION_NAMES,
                "source_statistics": {"declared_total": declared_total, "actual_total": actual_total, "demo_count": len(demos)},
                "warnings": warnings, "required_rules": list(RULES), "episodes": episodes,
                "timestamp_semantics": "预览坐标按 frame_index / 20 派生；不是原始采集时间，未验证硬件同步。"}
    write_json(output / "metadata.json", safe(metadata))
    issues = [issue for ep in episodes for issue in ep["issues"]]
    coverage = [c for ep in episodes for c in ep["coverage"]]
    fingerprint = hashlib.sha256(json.dumps({"sha256": provenance["sha256"], "rules": RULE_VERSION, "test_mode": test_mode, "demos": DEMOS}, sort_keys=True).encode()).hexdigest()
    report = {"schema_version": 1, "rule_version": RULE_VERSION, "input_fingerprint": fingerprint,
              "source": {**provenance, "source_kind": "injected_test" if test_mode else "original_public"},
              "params": {"fps": FPS, "timestamp_kind": "derived", "image_shape": [84, 84, 3]},
              "episodes": episodes, "issues": issues,
              "limitations": ["预览未写入 LeRobot，检查与图像缓存不代表已完成最终转换。", metadata["timestamp_semantics"]],
              "summary": {"episode_count": len(episodes), "row_count": sum(e["row_count"] for e in episodes),
                          "actual_row_count": sum(e["actual_row_count"] for e in episodes), "issue_count": len(issues),
                          "data_issue_count": sum(i["category"] == "data_issue" for i in issues),
                          "load_failure_count": sum(i["category"] == "load_failure" for i in issues),
                          "not_checked_count": sum(i["category"] == "not_checked" for i in issues),
                          "passed_episode_count": sum(e["status"] == "passed" for e in episodes),
                          "coverage_total": len(coverage), "coverage_performed": sum(c["status"] == "performed" for c in coverage)}}
    report = safe(report)
    report["result_digest"] = hashlib.sha256(json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    report.update(started_at_utc=utc_now(), runtime_seconds=round(time.monotonic() - started, 3))
    write_json(output / "quality_report.json", report)
    write_json(output / "report.json", report)
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--test-mode")
    args = parser.parse_args()
    try:
        metadata = build_preview(args.input.resolve(), args.output.resolve(), args.test_mode)
        print(json.dumps({"status": "completed", "episode_count": len(metadata["episodes"]), "metadata": "metadata.json"}))
        return 0
    except Exception as exc:
        diagnostic = {"status": "failed", "stage": "preview", "message_zh": str(exc) if isinstance(exc, ConversionError) else "HDF5 原始预览解析失败，请查看原始错误详情。",
                      "code": exc.code if isinstance(exc, ConversionError) else "preview_failed", "file": str(args.input),
                      "exception_type": type(exc).__name__, "exception_message": str(exc), "traceback": traceback.format_exc(),
                      **(exc.context if isinstance(exc, ConversionError) else {})}
        write_json(args.output / "diagnostic.json", diagnostic)
        print(json.dumps(diagnostic, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
