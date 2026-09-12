"""Fixed public robomimic HDF5 -> LeRobot v3.0 stages; no training or upload."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback


SOURCE_REPO = "robomimic/robomimic_datasets"
SOURCE_REVISION = "74fa018461f479cd9fd15b924a16103012096203"
SOURCE_PATH = "test/test.hdf5"
SOURCE_SIZE = 45939724
SOURCE_SHA256 = "80dea9ca9bb99dd7b96109712fc6b46bb6fa6b87e9622b8aebbe1e4ee5fadbab"
# 固定 test.hdf5（revision 74fa0184）的全部 10 条 demo 帧数。
# 校验始终使用这份冻结清单，不从被检文件自身推导，避免自证。
_ALL_DEMO_LENGTHS = {"demo_0": 59, "demo_1": 58, "demo_2": 57, "demo_3": 55, "demo_4": 51,
                     "demo_5": 58, "demo_6": 49, "demo_7": 49, "demo_8": 44, "demo_9": 51}
_DEFAULT_DEMO_INDICES = (0, 1, 2)
FPS = 20
CAMERA = "observation.images.agentview"
REPO_ID = "local/robomimic-lift-test-subset"
TASK = "Lift"
STATE_FIELDS = {"obs/robot0_eef_pos": 3, "obs/robot0_eef_quat": 4, "obs/robot0_gripper_qpos": 2}
STATE_NAMES = [f"{field.split('/')[-1]}[{i}]" for field, dim in STATE_FIELDS.items() for i in range(dim)]
ACTION_NAMES = [f"actions[{i}]" for i in range(7)]
STAGES = ("file_validation", "hdf5_validation", "field_mapping", "format_write", "official_verify", "archive_verify")


class ConversionError(Exception):
    def __init__(self, code: str, message: str, **context):
        super().__init__(message)
        self.code, self.context = code, context


def fail(code: str, message: str, **context):
    raise ConversionError(code, message, **context)


def _select_demos(raw=None):
    """按 ROBODATA_HDF5_EPISODES 选择 demo 子集，默认保持 V0.4 验收所用的 3 条。

    支持 "0-9" / "0,1,2" / "0-2,5" 形式；越界或非法输入直接失败，
    不静默降级为部分范围。返回 {demo_name: 预期帧数}。
    """
    text = os.environ.get("ROBODATA_HDF5_EPISODES") if raw is None else raw
    text = (text or "").strip()
    if text:
        indices = []
        for part in text.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                lo_text, _, hi_text = part.partition("-")
                try:
                    lo, hi = int(lo_text), int(hi_text)
                except ValueError:
                    fail("invalid_episode_range", f"数据范围片段无法解析：{part}")
                if hi < lo:
                    fail("invalid_episode_range", f"数据范围区间上界小于下界：{part}")
                indices.extend(range(lo, hi + 1))
            else:
                try:
                    indices.append(int(part))
                except ValueError:
                    fail("invalid_episode_range", f"数据范围片段无法解析：{part}")
    else:
        indices = list(_DEFAULT_DEMO_INDICES)
    selected = {}
    for index in sorted(set(indices)):
        name = f"demo_{index}"
        if index < 0 or name not in _ALL_DEMO_LENGTHS:
            fail("episode_out_of_range", f"数据范围包含固定样本之外的 demo：{name}",
                 available=sorted(_ALL_DEMO_LENGTHS))
        selected[name] = _ALL_DEMO_LENGTHS[name]
    if not selected:
        fail("invalid_episode_range", "数据范围为空")
    return selected


DEMOS = _select_demos()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def validate_file(source: Path, test_mode: str | None) -> dict:
    if not source.is_file():
        fail("source_missing", "找不到待转换的 HDF5 文件。", file=str(source))
    actual_size, digest = source.stat().st_size, sha256(source)
    matches = actual_size == SOURCE_SIZE and digest == SOURCE_SHA256
    if not matches and not test_mode:
        fail("source_hash_mismatch", "源文件与固定公开版本不一致，已停止转换；不能把改动副本标为原始数据。", file=str(source), evidence={"expected_size": SOURCE_SIZE, "actual_size": actual_size, "expected_sha256": SOURCE_SHA256, "actual_sha256": digest})
    return {
        "repo_id": SOURCE_REPO, "revision": SOURCE_REVISION, "path": SOURCE_PATH,
        "license": "MIT (upstream declaration)", "size": actual_size, "sha256": digest,
        "expected_sha256": SOURCE_SHA256, "matches_fixed_source": matches,
        "test_mode": test_mode, "kind": "public_simulation_test_subset", "camera": CAMERA,
    }


def load_conversion_spec(path: Path | None) -> dict | None:
    """Validate the frozen business selection; this is never an approval UI."""
    if path is None:
        return None
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
        episodes = spec["episodes"]
        if not isinstance(episodes, list) or not episodes:
            raise ValueError("候选任务为空")
        for key in ("batch_id", "snapshot_id", "input_fingerprint"):
            if not isinstance(spec.get(key), str) or not spec[key].strip():
                raise ValueError(f"缺少 {key}")
        if type(spec.get("content_version")) is not int or spec["content_version"] < 1:
            raise ValueError("业务版本必须为正整数")
        seen = set()
        for output_index, episode in enumerate(episodes):
            source_index = episode["source_episode_index"]
            if type(source_index) is not int or f"demo_{source_index}" not in DEMOS or source_index in seen:
                raise ValueError("源任务必须是固定配置中的不重复任务 0、1、2")
            seen.add(source_index)
            if type(episode["output_episode_index"]) is not int or episode["output_episode_index"] != output_index:
                raise ValueError("输出任务编号必须按候选顺序从 0 连续编号")
            if type(episode["row_count"]) is not int or episode["row_count"] != DEMOS[f"demo_{source_index}"]:
                raise ValueError("候选任务长度与固定配置不一致")
            annotation = episode["annotation"]
            if not isinstance(annotation, dict) or not isinstance(annotation.get("task"), str) or not annotation["task"].strip():
                raise ValueError("已审核任务描述不能为空")
            if not isinstance(episode.get("quality_run_id"), str) or not episode["quality_run_id"].strip():
                raise ValueError("候选任务缺少对应的质检运行编号")
        # BatchStore signs the complete snapshot using canonical strict JSON.
        unsigned = {k: v for k, v in spec.items() if k != "snapshot_id"}
        expected = hashlib.sha256(json.dumps(unsigned, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        if spec["snapshot_id"] != expected:
            raise ValueError("候选快照摘要不一致，内容可能已修改")
    except Exception as exc:
        fail("conversion_spec_invalid", f"转换候选清单无效：{exc}", file=str(path))
    return spec


def inspect_hdf5(source: Path, spec: dict | None = None) -> dict:
    import h5py
    import numpy as np

    try:
        handle = h5py.File(source, "r")
    except Exception as exc:
        fail("hdf5_unreadable", "HDF5 无法打开，文件可能损坏或被截断。", file=str(source), evidence={"original_type": type(exc).__name__, "original_message": str(exc)})
    with handle:
        if "data" not in handle:
            fail("hdf5_missing_data", "HDF5 缺少 data 数据分组。", file=str(source), field="/data")
        data = handle["data"]
        env_raw = data.attrs.get("env_args")
        if env_raw is None:
            fail("hdf5_missing_env", "缺少环境配置，无法核实任务与派生时间频率。", file=str(source), field="/data@env_args")
        if isinstance(env_raw, bytes):
            env_raw = env_raw.decode("utf-8")
        try:
            env = json.loads(env_raw)
        except Exception as exc:
            fail("hdf5_env_invalid", "环境配置不是可读取的 JSON。", file=str(source), field="/data@env_args", evidence={"error": str(exc)})
        control_freq = env.get("env_kwargs", {}).get("control_freq")
        if control_freq != FPS or env.get("env_name") != "Lift":
            fail("hdf5_profile_mismatch", "当前适配器只接受已验证的 Lift / 20 Hz 配置。", file=str(source), field="/data@env_args", evidence={"env_name": env.get("env_name"), "control_freq": control_freq})
        episodes = []
        selection = spec["episodes"] if spec else [
            {"source_episode_index": index, "output_episode_index": index, "row_count": length,
             "annotation": {"task": TASK}, "quality_run_id": None}
            for index, length in enumerate(DEMOS.values())]
        for selected in selection:
            episode, source_index = selected["output_episode_index"], selected["source_episode_index"]
            demo, expected_length = f"demo_{source_index}", selected["row_count"]
            if demo not in data:
                fail("hdf5_missing_demo", "缺少需要转换的任务分组。", file=str(source), demo=demo, field=f"/data/{demo}")
            group = data[demo]
            required = [*STATE_FIELDS, "actions", "obs/agentview_image"]
            for field in required:
                if field not in group:
                    fail("hdf5_missing_field", "任务缺少必需字段。", file=str(source), demo=demo, field=f"/data/{demo}/{field}")
                if not isinstance(group[field], h5py.Dataset):
                    fail("hdf5_field_not_dataset", "预期为数组的字段实际不是 HDF5 数据集。", file=str(source), demo=demo, field=f"/data/{demo}/{field}")
            actions = group["actions"]
            if actions.ndim != 2:
                fail("hdf5_actions_rank", "动作数组必须为帧数 × 动作维数的二维数组。", file=str(source), demo=demo, field=f"/data/{demo}/actions", evidence={"shape": list(actions.shape)})
            length = actions.shape[0]
            if length != expected_length or int(group.attrs.get("num_samples", -1)) != length:
                fail("hdf5_length_mismatch", "任务长度与固定适配配置或任务元数据不一致。", file=str(source), demo=demo, field=f"/data/{demo}@num_samples", evidence={"expected": expected_length, "actions_length": length, "num_samples": int(group.attrs.get("num_samples", -1))})
            for field in required:
                array = group[field]
                if array.ndim == 0 or array.shape[0] != length:
                    fail("hdf5_frame_count_mismatch", "图像、状态或动作的帧数不一致。", file=str(source), demo=demo, field=f"/data/{demo}/{field}", evidence={"expected": length, "shape": list(array.shape)})
                if field != "obs/agentview_image":
                    if array.dtype.kind not in "fiu":
                        fail("hdf5_numeric_type", "状态或动作字段不是数值数组。", file=str(source), demo=demo, field=f"/data/{demo}/{field}", evidence={"dtype": str(array.dtype)})
                    values = array[:]
                    bad = np.argwhere(~np.isfinite(values))
                    if len(bad):
                        location = tuple(int(i) for i in bad[0])
                        fail("hdf5_nonfinite", "状态或动作包含 NaN 或无穷值，已定位到首个问题。", file=str(source), demo=demo, field=f"/data/{demo}/{field}", row=location[0], evidence={"column": location[1] if len(location) > 1 else None, "value": str(values[location])})
            episodes.append({"episode_index": episode, "source_episode_index": source_index,
                             "output_episode_index": episode, "source_demo": demo, "length": length,
                             "task": selected["annotation"]["task"], "source_task": TASK,
                             "annotation": selected["annotation"], "quality_run_id": selected.get("quality_run_id"),
                             "fields": {field: {"shape": list(group[field].shape), "dtype": str(group[field].dtype)} for field in required}})
        source_demos = [key for key in data if key.startswith("demo_") and isinstance(data[key], h5py.Group)]
        actual_total = sum(data[key]["actions"].shape[0] for key in source_demos if "actions" in data[key])
        declared_total = int(data.attrs.get("total", -1))
        warnings = []
        if declared_total != actual_total:
            warnings.append({"code": "source_total_mismatch", "message": "源文件全局 total 与实际任务动作帧数之和不同；保留源文件，输出只按所选任务重新统计。", "declared_total": declared_total, "actual_total": actual_total, "source_demo_count": len(source_demos)})
        return {"episodes": episodes, "selected_frames": sum(ep["length"] for ep in episodes), "selected_episodes": len(episodes), "source_demo_count": len(source_demos), "source_declared_total": declared_total, "source_actual_total": actual_total, "env_name": env["env_name"], "control_freq": control_freq, "warnings": warnings, "conversion_spec": spec}


def mapping_spec() -> dict:
    return {
        "observation.state": {"source_fields": [{"field": field, "width": dim} for field, dim in STATE_FIELDS.items()], "names": STATE_NAMES, "dtype": "float32", "width": 9, "meaning": "保留来源末端位置、四元数与夹爪值的原始顺序，不补充未核实单位。"},
        "action": {"source_field": "actions", "names": ACTION_NAMES, "dtype": "float32", "width": 7, "meaning": "保留原始控制表示与顺序，不改写为关节目标或补充控制单位。"},
        CAMERA: {"source_field": "obs/agentview_image", "shape": [84, 84, 3], "dtype": "uint8", "color_order": "RGB", "storage": "H.264 MP4 (lossy)"},
        "timestamp": {"derivation": "frame_index / 20", "fps": FPS, "kind": "derived_from_control_freq", "hardware_synchronization_verified": False},
    }


def map_arrays(source: Path, metadata: dict) -> list[dict]:
    import h5py
    import numpy as np

    mapped = []
    with h5py.File(source, "r") as handle:
        for episode in metadata["episodes"]:
            demo, length = episode["source_demo"], episode["length"]
            group = handle[f"data/{demo}"]
            values = {}
            for field, dim in {**STATE_FIELDS, "actions": 7}.items():
                array = group[field]
                if array.shape != (length, dim) or array.dtype.kind not in "fiu":
                    fail("mapping_dimension_mismatch", "字段维度或数值类型与固定映射不一致，不能继续拼接。", file=str(source), demo=demo, field=f"/data/{demo}/{field}", evidence={"expected_shape": [length, dim], "actual_shape": list(array.shape), "dtype": str(array.dtype)})
                original = array[:]
                bad = np.argwhere(~np.isfinite(original))
                if len(bad):
                    row, column = (int(i) for i in bad[0])
                    fail("mapping_nonfinite", "源数值包含 NaN 或无穷值，已定位到首个问题。", file=str(source), demo=demo, field=f"/data/{demo}/{field}", row=row, evidence={"column": column, "value": str(original[row, column])})
                with np.errstate(over="ignore", invalid="ignore"):
                    converted = original.astype(np.float32)
                if not np.isfinite(converted).all():
                    row, column = (int(i) for i in np.argwhere(~np.isfinite(converted))[0])
                    fail("mapping_float32_overflow", "源数值转为 float32 后溢出，已停止转换。", file=str(source), demo=demo, field=f"/data/{demo}/{field}", row=row, evidence={"column": column})
                values[field] = converted
            images = group["obs/agentview_image"]
            if images.shape != (length, 84, 84, 3) or images.dtype != np.dtype("uint8"):
                fail("mapping_image_shape", "相机必须为帧数 × 84 × 84 × 3 的 uint8 RGB 数组。", file=str(source), demo=demo, field=f"/data/{demo}/obs/agentview_image", evidence={"shape": list(images.shape), "dtype": str(images.dtype)})
            mapped.append({**episode, "state": np.concatenate([values[field] for field in STATE_FIELDS], axis=1), "action": values["actions"], "images": images[:], "timestamp": (np.arange(length, dtype=np.float64) / FPS).astype(np.float32)})
    return mapped


def output_files(output: Path) -> list[dict]:
    result = []
    for folder in ("meta", "data", "videos"):
        for path in sorted((output / folder).rglob("*")):
            if path.is_file() and path.suffix in (".json", ".parquet", ".mp4"):
                result.append({"path": path.relative_to(output).as_posix(), "size": path.stat().st_size, "sha256": sha256(path)})
    if (output / "annotations.json").is_file():
        path = output / "annotations.json"
        result.append({"path": "annotations.json", "size": path.stat().st_size, "sha256": sha256(path)})
    return result


def streaming_encoding() -> bool:
    """流式编码直接向编码器喂帧，不落临时图片目录。

    默认关闭，保持 V0.4 已验证的编码路径。在临时文件批量清理被策略拦截的
    受限环境下必须启用（ROBODATA_STREAMING_ENCODING=1），否则 save_episode
    会在清理临时图片目录阶段失败，表现为工作进程退出码 1。
    """
    value = (os.environ.get("ROBODATA_STREAMING_ENCODING") or "").strip().lower()
    return value not in ("", "0", "false", "no")


def write_dataset(source: Path, output: Path, provenance: dict, metadata: dict, mapped: list[dict]) -> dict:
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    if not sys.flags.utf8_mode:
        fail("utf8_mode_required", "Windows 中文路径转换需要在启动此进程前设置 PYTHONUTF8=1。", file=str(output))
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            fail("output_not_empty", "输出目录已有内容，已停止以避免覆盖；请使用新的运行目录。", file=str(output))
        output.rmdir()  # Only an already checked, empty staging directory.
    features = {
        "observation.state": {"dtype": "float32", "shape": (9,), "names": STATE_NAMES},
        "action": {"dtype": "float32", "shape": (7,), "names": ACTION_NAMES},
        CAMERA: {"dtype": "video", "shape": (84, 84, 3), "names": ["height", "width", "channels"]},
    }
    dataset = LeRobotDataset.create(repo_id=REPO_ID, root=output, fps=FPS, features=features, robot_type="panda_simulation", video_backend="pyav", vcodec="h264", image_writer_processes=0, image_writer_threads=0, batch_encoding_size=1, streaming_encoding=streaming_encoding())
    try:
        for episode in mapped:
            for row in range(episode["length"]):
                dataset.add_frame({"task": episode["task"], "observation.state": episode["state"][row], "action": episode["action"][row], CAMERA: episode["images"][row]})
            dataset.save_episode(parallel_encoding=False)
    finally:
        dataset.finalize()
    write_json(output / "annotations.json", {
        "schema_version": 1, "source_task": TASK,
        "mapping_policy": "人工审核的 annotation.task 写入训练 task；原始 Lift 和完整标注随附保留。",
        "conversion_spec": metadata.get("conversion_spec"),
        "episodes": [{key: ep[key] for key in ("source_episode_index", "output_episode_index", "source_demo", "source_task", "task", "annotation", "quality_run_id")} for ep in metadata["episodes"]],
    })
    manifest = {
        "schema_version": 1, "created_at": utc_now(), "format_version": "v3.0", "profile": "robomimic_lift_test_v1",
        "source": provenance, "mapping": mapping_spec(), "episodes": metadata["episodes"],
        "total_episodes": metadata["selected_episodes"], "total_frames": metadata["selected_frames"],
        "time_policy": "Derived timestamp = frame_index / 20 from source control_freq; no hardware synchronization claim.",
        "warnings": metadata["warnings"], "source_statistics": {"declared_total": metadata["source_declared_total"], "actual_total": metadata["source_actual_total"], "demo_count": metadata["source_demo_count"]},
        "writer": {name: importlib.metadata.version(name) for name in ["lerobot", "torch", "torchvision", "av", "h5py", "numpy", "datasets"]},
        "official_validation_required": True,
        "conversion_spec": metadata.get("conversion_spec"),
        "files": output_files(output),
    }
    write_json(output / "source_manifest.json", manifest)
    (output / "CONVERSION.md").write_text(
        f"# 转换说明\n\n本数据来自固定版本 robomimic 的 Panda / Lift 仿真测试文件，选择 {', '.join(ep['source_demo'] for ep in metadata['episodes'])}，"
        "不把测试片段声明为完整真实机器人轨迹。状态按末端位置、四元数、夹爪值顺序拼接为9维；"
        "动作保留7维原始表示，均转换为float32。相机为84×84 RGB，H.264有损编码。\n\n"
        f"时间来自帧序号除以20 Hz，不代表硬件同步。源全局统计异常保留在source_manifest.json，输出按{metadata['selected_episodes']}条{metadata['selected_frames']}帧重算。"
        "人工标注中的任务描述写入训练 task；原始 Lift 与完整标注保存在 annotations.json，源／输出任务编号映射见清单。"
        "source_manifest.json记录输入来源、映射和实际产物哈希。必须通过官方离线加载及主工作台质检，才能注册为可用产物。\n"
        + (f"\n**这是明确标注的注入测试副本：{provenance['test_mode']}，不能冒充固定原始样本。**\n" if provenance["test_mode"] else ""), encoding="utf-8")
    return {"format_version": "v3.0", "output": str(output), "episodes": metadata["selected_episodes"], "frames": metadata["selected_frames"], "source_manifest": "source_manifest.json", "file_count": len(manifest["files"]), "source_sha256": provenance["sha256"], "warnings": metadata["warnings"]}


def verify_dataset(source: Path | None, output: Path, artifacts: Path, provenance: dict, metadata: dict,
                   mapped: list[dict], *, archive_only: bool = False) -> dict:
    import numpy as np
    import pandas as pd
    import torch
    from torch.utils.data import DataLoader
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    if os.environ.get("HF_HUB_OFFLINE") != "1" or os.environ.get("HF_DATASETS_OFFLINE") != "1":
        fail("offline_mode_required", "官方加载验收必须使用离线进程，不能自动补下载缺失文件。", file=str(output))
    manifest_path = output / "source_manifest.json"
    if not manifest_path.is_file():
        fail("manifest_missing", "输出缺少来源清单，无法追溯与验证。", file=str(manifest_path))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("source", {}).get("sha256") != provenance["sha256"] or manifest.get("source", {}).get("test_mode") != provenance["test_mode"]:
        fail("verification_source_mismatch", "输出记录的源文件或测试标记与本次输入不一致。", file=str(manifest_path))
    if manifest.get("conversion_spec") != metadata.get("conversion_spec"):
        fail("verification_spec_mismatch", "输出记录的候选集与本次冻结清单不一致。", file=str(manifest_path))
    expected_files = manifest.get("files")
    actual_files = output_files(output)
    if not expected_files or expected_files != actual_files:
        expected_map = {entry["path"]: entry for entry in expected_files or []}
        actual_map = {entry["path"]: entry for entry in actual_files}
        changed = [key for key in sorted(set(expected_map) | set(actual_map)) if expected_map.get(key) != actual_map.get(key)]
        fail("output_hash_mismatch", "输出文件已缺失或发生变化，官方加载前的完整性检查未通过。", file=str(output / changed[0]) if changed else str(output), evidence={"changed_files": changed})
    dataset = LeRobotDataset(repo_id=REPO_ID, root=output, video_backend="pyav")
    if len(dataset) != metadata["selected_frames"] or dataset.meta.total_episodes != len(mapped) or dataset.meta.info.get("codebase_version") != "v3.0":
        fail("official_metadata_mismatch", "官方加载的任务数、帧数或格式版本与所选源数据不一致。", file=str(output / "meta/info.json"), evidence={"loaded_frames": len(dataset), "loaded_episodes": dataset.meta.total_episodes})
    if dataset.meta.info.get("splits") != {"train": f"0:{len(mapped)}"}:
        fail("official_split_mismatch", "本配置输出必须把全部所选任务明确划分为 train。", file=str(output / "meta/info.json"), field="splits", evidence={"actual": dataset.meta.info.get("splits")})
    table = pd.concat([pd.read_parquet(path) for path in sorted((output / "data").rglob("*.parquet"))], ignore_index=True).sort_values("index").reset_index(drop=True)
    if len(table) != metadata["selected_frames"] or set(table["episode_index"].tolist()) != set(range(len(mapped))):
        fail("verification_table_inventory", "实际表格总量或任务编号集合与输出清单不一致。", file=str(output / "data"))
    global_start = 0
    samples, decoded_images = [], []
    for episode in mapped:
        demo, ep_index, length = episode["source_demo"], episode["episode_index"], episode["length"]
        rows = table[table["episode_index"] == ep_index].sort_values("frame_index")
        if len(rows) != length:
            fail("verification_episode_length", "输出任务的帧数与源任务不一致。", file=str(output), demo=demo, evidence={"expected": length, "actual": len(rows)})
        for key, expected in [("observation.state", episode["state"]), ("action", episode["action"])]:
            actual = np.stack(rows[key].to_numpy()).astype(np.float32)
            if not np.isfinite(actual).all():
                bad = np.argwhere(~np.isfinite(actual))[0]
                fail("verification_nonfinite", "输出数组包含 NaN 或无穷值。", file=str(output / "data"), demo=demo, field=key, row=int(bad[0]))
            if actual.shape != expected.shape or (not archive_only and not np.array_equal(actual, expected)):
                mismatch = np.argwhere(actual != expected) if actual.shape == expected.shape else np.array([[0, 0]])
                fail("verification_numeric_mismatch", "输出数值与源字段的 float32 映射不一致。", file=str(output), demo=demo, field=key, row=int(mismatch[0, 0]), evidence={"actual_shape": list(actual.shape), "expected_shape": list(expected.shape)})
        for key, expected in [("index", np.arange(global_start, global_start + length)), ("frame_index", np.arange(length))]:
            if not np.array_equal(rows[key].to_numpy(), expected):
                fail("verification_index_mismatch", "输出样本索引不连续或与源顺序不一致。", file=str(output), demo=demo, field=key)
        if not np.allclose(rows["timestamp"].to_numpy(), episode["timestamp"], rtol=0, atol=1e-6):
            fail("verification_time_mismatch", "输出派生时间与帧序号 / 20 Hz 不一致。", file=str(output), demo=demo, field="timestamp")
        for row in [0, length // 2, length - 1]:
            index = global_start + row
            item = dataset[index]
            for key, expected in [("observation.state", episode["state"][row]), ("action", episode["action"][row])]:
                if not np.array_equal(item[key].numpy(), expected):
                    fail("official_sample_numeric", "官方读取样本数值与源字段映射不一致。", file=str(output), demo=demo, field=key, row=row)
            if int(item["episode_index"]) != ep_index or int(item["frame_index"]) != row or int(item["index"]) != index or item["task"] != episode["task"]:
                fail("official_sample_identity", "官方读取的样本身份或任务文本不正确。", file=str(output), demo=demo, row=row)
            image = item[CAMERA]
            if image.dtype != torch.float32 or tuple(image.shape) != (3, 84, 84) or not torch.isfinite(image).all() or float(image.min()) < 0 or float(image.max()) > 1:
                fail("official_sample_image", "官方读取图像的类型、尺寸或范围不正确。", file=str(output), demo=demo, field=CAMERA, row=row)
            pixels = np.rint(image.permute(1, 2, 0).numpy() * 255).astype(np.uint8)
            decoded_images.append(pixels)
            sample = {"episode": ep_index, "source_demo": demo, "row": row, "global_index": index, "timestamp": float(item["timestamp"]), "decoded_image_sha256": hashlib.sha256(pixels.tobytes()).hexdigest()}
            if not archive_only:
                source_image = episode["images"][row]
                sample.update(source_image_sha256=hashlib.sha256(source_image.tobytes()).hexdigest(), source_decoded_mean_absolute_error=float(np.abs(pixels.astype(np.float32) - source_image.astype(np.float32)).mean()))
            samples.append(sample)
        global_start += length
    batch = next(iter(DataLoader(dataset, batch_size=2, num_workers=0, shuffle=False)))
    if tuple(batch["observation.state"].shape) != (2, 9) or tuple(batch["action"].shape) != (2, 7) or tuple(batch[CAMERA].shape) != (2, 3, 84, 84):
        fail("official_batch_shape", "官方 DataLoader 的批次维度不正确。", file=str(output))
    np.savez_compressed(artifacts / "decoded_samples.npz", images=np.stack(decoded_images), episode_indices=np.array([sample["episode"] for sample in samples]), frame_indices=np.array([sample["row"] for sample in samples]), global_indices=np.array([sample["global_index"] for sample in samples]))
    return {"format_version": "v3.0", "source_sha256": provenance["sha256"], "checked_numeric_frames": 0 if archive_only else len(table),
            "output_numeric_frames_checked": len(table), "source_numeric_consistency_rechecked": not archive_only,
            "validation_scope": "解包后文件哈希、元数据、索引、有限数值与官方离线加载；源数值一致性仅引用转换时记录，未重新读取源文件" if archive_only else "输出全量数值与源 float32 映射一致，官方离线抽查和 DataLoader 加载",
            "episode_count": len(mapped), "row_count": len(table), "snapshot_id": (metadata.get("conversion_spec") or {}).get("snapshot_id"),
            "sample_count": len(samples), "samples": samples, "batch_size": 2, "batch_image_shape": list(batch[CAMERA].shape), "decoded_samples": "decoded_samples.npz", "offline": True, "video_backend": "pyav", "cuda_available": torch.cuda.is_available(), "image_comparison": "H.264 is lossy; no exact-pixel preservation claim", "file_count": len(actual_files), "warnings": metadata["warnings"]}


def verify_archive(output: Path, artifacts: Path) -> dict:
    """Validate an independently unpacked export without having its source HDF5."""
    import numpy as np
    import pandas as pd
    path = output / "source_manifest.json"
    if not path.is_file():
        fail("manifest_missing", "解包数据缺少来源清单。", file=str(path))
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("profile") != "robomimic_lift_test_v1" or manifest.get("format_version") != "v3.0":
        fail("archive_profile_unsupported", "解包验证仅支持本项目已验证的 LeRobot v3.0 输出配置。", file=str(path))
    episodes = manifest.get("episodes", [])
    if not episodes or [e.get("episode_index") for e in episodes] != list(range(len(episodes))):
        fail("archive_episode_index", "清单任务编号必须从 0 连续排列。", file=str(path))
    frames = sum(int(e["length"]) for e in episodes)
    if manifest.get("total_frames") != frames or manifest.get("total_episodes") != len(episodes):
        fail("archive_manifest_count", "清单所记任务数、帧数不一致。", file=str(path))
    paths = sorted((output / "data").rglob("*.parquet"))
    if not paths:
        fail("archive_tables_missing", "解包数据缺少实际表格。", file=str(output / "data"))
    table = pd.concat([pd.read_parquet(p) for p in paths], ignore_index=True)
    required = ["index", "episode_index", "frame_index", "timestamp", "observation.state", "action"]
    missing = [field for field in required if field not in table]
    if missing:
        fail("archive_fields_missing", "解包表格缺少必需字段。", file=str(output / "data"), evidence={"missing_fields": missing})
    mapped = []
    for episode in episodes:
        ep = int(episode["episode_index"])
        rows = table[table["episode_index"] == ep].sort_values("frame_index")
        values = {}
        for field, dim, name in [("observation.state", 9, "state"), ("action", 7, "action")]:
            array = np.stack(rows[field].to_numpy()) if len(rows) else np.empty((0, dim))
            if array.shape != (int(episode["length"]), dim) or not np.isfinite(array).all():
                fail("archive_numeric_invalid", "解包数据数值的维度、长度或有效性不符合清单。", file=str(output / "data"), field=field, demo=episode["source_demo"], evidence={"shape": list(array.shape)})
            values[name] = array.astype(np.float32)
        mapped.append({**episode, **values, "timestamp": (np.arange(episode["length"]) / FPS).astype(np.float32)})
    metadata = {"episodes": episodes, "selected_frames": frames, "selected_episodes": len(episodes),
                "conversion_spec": manifest.get("conversion_spec"), "warnings": manifest.get("warnings", [])}
    return verify_dataset(None, output, artifacts, manifest["source"], metadata, mapped, archive_only=True)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=STAGES)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--spec", type=Path, help="Frozen business conversion_spec JSON")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--test-mode", help="Explicit injection label; marks the modified input as a test copy")
    args = parser.parse_args()
    source, output, artifacts = args.input.resolve() if args.input else None, args.output.resolve(), args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    if os.environ.get("ROBODATA_PARENT_PID"):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from robodata.runtime import _parent_watchdog
        threading.Thread(target=_parent_watchdog, args=(int(os.environ["ROBODATA_PARENT_PID"]), os.environ.get("ROBODATA_PARENT_IDENTITY")), daemon=True).start()
    started = time.monotonic()
    result = {"schema_version": 1, "stage": args.stage, "status": "running", "input": str(source), "test_mode": args.test_mode, "started_at": utc_now()}
    try:
        if args.test_mode is not None and not args.test_mode.strip():
            fail("test_label_missing", "注入测试必须填写明确的测试标签。", file=str(source))
        spec = load_conversion_spec(args.spec)
        if args.stage == "archive_verify":
            details = verify_archive(output, artifacts)
        else:
            if source is None:
                fail("source_argument_missing", "转换与源数值校验必须指定 --input。")
            provenance = validate_file(source, args.test_mode)
            result["input_sha256"] = provenance["sha256"]
            if args.stage == "file_validation":
                details = provenance
            else:
                metadata = inspect_hdf5(source, spec)
                if args.stage == "hdf5_validation":
                    details = metadata
                else:
                    mapped = map_arrays(source, metadata)
                    if args.stage == "field_mapping":
                        details = {"mapping": mapping_spec(), "episodes": metadata["episodes"], "frames": metadata["selected_frames"], "warnings": metadata["warnings"]}
                    elif args.stage == "format_write":
                        details = write_dataset(source, output, provenance, metadata, mapped)
                    else:
                        details = verify_dataset(source, output, artifacts, provenance, metadata, mapped)
        result.update(status="passed", details=details, elapsed_seconds=round(time.monotonic() - started, 3), completed_at=utc_now())
        write_json(artifacts / "steps" / f"{args.stage}.json", result)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as exc:
        context = exc.context if isinstance(exc, ConversionError) else {}
        message = str(exc) if isinstance(exc, ConversionError) else f"{dict(zip(STAGES, ['文件校验', 'HDF5 结构校验', '字段映射', '格式写入', '官方加载验证', '交付包官方加载验证']))[args.stage]}执行失败；请展开原始错误详情定位。"
        if isinstance(exc, OSError) and args.stage == "format_write":
            message = "输出文件写入失败，请检查目标路径是否被文件占用、目录权限和可用空间。"
        default_file = str(output) if args.stage in ("format_write", "official_verify", "archive_verify") else str(source)
        diagnostic = {"stage": args.stage, "status": "failed", "code": exc.code if isinstance(exc, ConversionError) else "stage_execution_failed", "message": message, "message_zh": message, "file": context.get("file", default_file), "demo": context.get("demo"), "field": context.get("field"), "row": context.get("row"), "evidence": context.get("evidence", {}), "exception_type": type(exc).__name__, "exception_message": str(exc), "traceback": traceback.format_exc(), "timestamp": utc_now()}
        write_json(artifacts / "diagnostic.json", diagnostic)
        result.update(status="failed", diagnostic=diagnostic, elapsed_seconds=round(time.monotonic() - started, 3), completed_at=utc_now())
        write_json(artifacts / "steps" / f"{args.stage}.json", result)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
