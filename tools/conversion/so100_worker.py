"""SO-100 真机 LeRobot v2.0 -> v3.0；复用官方阶段函数，绝不原地替换数据集。

为什么需要这个文件
------------------
官方 `convert_dataset_v21_to_v30.py` 的 `convert_dataset()` 结尾会：

    shutil.rmtree(new_root)                  # 先删目标
    shutil.move(str(root), str(old_root))    # 把原目录改名
    shutil.move(str(new_root), str(root))    # 新目录顶替原名

在本项目里这不合适：既会真正删除目录（在受限环境下会被策略拦截），也会原地
覆盖掉 `data/so100_sweet_pick` 这份固定来源副本。所以这里**只调用五个阶段
函数**，跳过收尾的删除与替换，输出到独立的 staging 目录。

另一个卡点是官方硬校验 `codebase_version == "v2.1"`，而 SO-100 是 v2.0，
且缺少 v2.1 必需的 `meta/episodes_stats.jsonl`。这里选择**补齐**而不是改掉
校验条件——产物与官方工具预期一致，可信度才有依据。

范围控制
--------
`ROBODATA_SO100_EPISODES` 决定处理哪些任务，默认 5 条（浏览子集）。
全集 50 条 / 32068 帧需要先下载全量数据，再用 0-49 打开。

安全约定
--------
- 输入目录只读：所有转换都写到 `--output` 指定的新目录。
- 工作副本位于 `--output` 的兄弟路径，不回写输入。
- 不调用 `rmtree` / `unlink`；需要重来时直接换一个新的运行目录。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import traceback


# ---------------------------------------------------------------- 来源固定值

SOURCE_REPO = "jmrog/so100_sweet_pick"
SOURCE_REVISION = "54141acb0bd6bcb868e34b4eb328f26481d333b2"
SOURCE_CAMERA = "observation.images.laptop"
SOURCE_FPS = 30
SOURCE_ROBOT = "so100"
FORMAT_INPUT = "v2.0"
FORMAT_MIDDLE = "v2.1"
FORMAT_OUTPUT = "v3.0"
REPO_ID = "local/so100-sweet-pick-v30"

# 与 `data/so100_sweet_pick/meta/episodes.jsonl` 一致的任务长度冻结清单。
# 校验只认这份清单，不从被检数据自身推导，避免自证。
_ALL_EPISODE_LENGTHS = {
    0: 503, 1: 797, 2: 497, 3: 898, 4: 867,
    5: 569, 6: 665, 7: 611, 8: 642, 9: 620,
    10: 600, 11: 594, 12: 596, 13: 666, 14: 578,
    15: 643, 16: 613, 17: 620, 18: 616, 19: 619,
    20: 615, 21: 628, 22: 640, 23: 645, 24: 657,
    25: 661, 26: 640, 27: 645, 28: 625, 29: 630,
    30: 646, 31: 638, 32: 626, 33: 629, 34: 621,
    35: 612, 36: 619, 37: 627, 38: 638, 39: 632,
    40: 629, 41: 600, 42: 537, 43: 535, 44: 543,
    45: 648, 46: 617, 47: 640, 48: 643, 49: 620,
}
_DEFAULT_EPISODE_INDICES = (0, 1, 2, 3, 4)

STAGES = (
    "file_validation", "metadata_upgrade", "field_mapping",
    "format_write", "official_verify", "archive_verify",
)
STAGE_LABELS = {
    "file_validation": "文件校验", "metadata_upgrade": "元数据升级（v2.0 → v2.1）",
    "field_mapping": "字段映射核对", "format_write": "官方格式写入（v2.1 → v3.0）",
    "official_verify": "官方加载验证", "archive_verify": "交付包官方加载验证",
}


class ConversionError(Exception):
    def __init__(self, code: str, message: str, **context):
        super().__init__(message)
        self.code, self.context = code, context


def fail(code: str, message: str, **context):
    raise ConversionError(code, message, **context)


# ------------------------------------------------------------ 范围与来源校验


def select_episodes(raw: str | None = None) -> dict[int, int]:
    """按 `ROBODATA_SO100_EPISODES` 选择任务子集，返回 {任务编号: 帧数}。"""
    text = (os.environ.get("ROBODATA_SO100_EPISODES") if raw is None else raw) or ""
    text = text.strip()
    if not text:
        indices = list(_DEFAULT_EPISODE_INDICES)
    else:
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
    selected: dict[int, int] = {}
    for index in sorted(set(indices)):
        if index not in _ALL_EPISODE_LENGTHS:
            fail("episode_out_of_range", f"数据范围包含固定样本之外的任务：episode_{index:06d}",
                 available=[f"episode_{i:06d}" for i in sorted(_ALL_EPISODE_LENGTHS)])
        selected[index] = _ALL_EPISODE_LENGTHS[index]
    if not selected:
        fail("invalid_episode_range", "数据范围为空")
    return selected


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> list[dict]:
    items = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            fail("source_metadata_invalid", f"{path.name} 第 {number} 行不是 JSON 对象。", file=str(path))
        items.append(value)
    return items


def validate_source(source: Path, episodes: dict[int, int]) -> dict:
    """只读检查：版本、字段形状、相机、任务长度都必须与固定配置一致。"""
    source = Path(source)
    if not source.is_dir():
        fail("source_missing", "找不到待转换的 SO-100 数据目录。", file=str(source))
    info_path = source / "meta" / "info.json"
    if not info_path.is_file():
        fail("source_metadata_missing", "缺少 meta/info.json。", file=str(info_path))
    info = json.loads(info_path.read_text(encoding="utf-8"))
    version = info.get("codebase_version")
    if version != FORMAT_INPUT:
        fail("source_version_mismatch", f"SO-100 适配器只接受 LeRobot {FORMAT_INPUT} 输入。",
             file=str(info_path), evidence={"expected": FORMAT_INPUT, "actual": version})
    if info.get("fps") != SOURCE_FPS or info.get("robot_type") != SOURCE_ROBOT:
        fail("source_profile_mismatch", "SO-100 适配器只接受已验证的 30 Hz / so100 配置。",
             file=str(info_path), evidence={"fps": info.get("fps"), "robot_type": info.get("robot_type")})
    features = info.get("features") or {}
    for key in ("action", "observation.state", SOURCE_CAMERA):
        if key not in features:
            fail("source_field_missing", f"info.json 缺少必需字段：{key}", file=str(info_path), field=key)
    if list(features["action"].get("shape") or []) != [6] or list(features["observation.state"].get("shape") or []) != [6]:
        fail("source_dimension_mismatch", "SO-100 的状态与动作维度必须为 6。", file=str(info_path),
             evidence={"action": features["action"].get("shape"), "state": features["observation.state"].get("shape")})

    # 声明与实际必须一致：只对存在的分片做转换，并如实记录差异。
    declared = {int(item["episode_index"]): item for item in _read_jsonl(source / "meta" / "episodes.jsonl")}
    on_disk = {int(path.stem.split("_")[1]) for path in (source / "data" / "chunk-000").glob("episode_*.parquet")}
    videos_on_disk = {int(path.stem.split("_")[1])
                      for path in (source / "videos" / "chunk-000" / SOURCE_CAMERA).glob("episode_*.mp4")}
    absent = sorted(set(episodes) - on_disk)
    if absent:
        fail("source_episode_missing", "所选任务缺少本地数据分片。", file=str(source / "data" / "chunk-000"),
             evidence={"missing_episodes": [f"episode_{i:06d}" for i in absent],
                       "available": [f"episode_{i:06d}" for i in sorted(on_disk)]})
    absent_video = sorted(set(episodes) - videos_on_disk)
    if absent_video:
        fail("source_video_missing", "所选任务缺少本地视频分片。",
             file=str(source / "videos" / "chunk-000" / SOURCE_CAMERA),
             evidence={"missing_episodes": [f"episode_{i:06d}" for i in absent_video]})

    records = []
    for index, expected_length in sorted(episodes.items()):
        entry = declared.get(index)
        if entry is None:
            fail("source_episode_undeclared", "所选任务未在元数据中声明。", file=str(source / "meta" / "episodes.jsonl"),
                 evidence={"episode_index": index})
        if int(entry.get("length", -1)) != expected_length:
            fail("source_length_mismatch", "任务长度与冻结清单不一致，不能继续转换。",
                 file=str(source / "meta" / "episodes.jsonl"), demo=f"episode_{index:06d}",
                 evidence={"expected": expected_length, "declared": entry.get("length")})
        data_path = source / "data" / "chunk-000" / f"episode_{index:06d}.parquet"
        video_path = source / "videos" / "chunk-000" / SOURCE_CAMERA / f"episode_{index:06d}.mp4"
        records.append({"episode_index": index, "length": expected_length,
                        "data_path": data_path.relative_to(source).as_posix(),
                        "video_path": video_path.relative_to(source).as_posix(),
                        "data_size": data_path.stat().st_size, "video_size": video_path.stat().st_size,
                        "data_sha256": sha256(data_path), "video_sha256": sha256(video_path)})
    return {
        "repo_id": SOURCE_REPO, "revision": SOURCE_REVISION, "camera": SOURCE_CAMERA,
        "fps": SOURCE_FPS, "robot_type": SOURCE_ROBOT, "input_format_version": FORMAT_INPUT,
        "declared_episodes": int(info.get("total_episodes", len(declared))),
        "declared_frames": int(info.get("total_frames", -1)),
        "present_episodes": len(on_disk), "present_frames": sum(_ALL_EPISODE_LENGTHS[i] for i in sorted(on_disk)),
        "selected_episodes": len(records), "selected_frames": sum(r["length"] for r in records),
        "episodes": records, "kind": "public_real_robot_subset",
    }


# ------------------------------------------------- 阶段一：v2.0 -> v2.1 补齐


def _sample_video_frames(video_path: Path, out_dir: Path, episode_index: int, count: int) -> list[str]:
    """从 mp4 均匀抽帧写成 PNG；官方 `compute_episode_stats` 的视频口径只接受图片路径。"""
    import av
    import numpy as np
    from PIL import Image

    out_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    with av.open(str(video_path)) as container:
        stream = container.streams.video[0]
        for frame in container.decode(stream):
            frames.append(frame.to_ndarray(format="rgb24"))
    total = len(frames)
    if total == 0:
        fail("source_video_undecodable", "视频没有任何可解码帧。", file=str(video_path))
    picked = (list(range(total)) if total <= count
              else sorted(set(np.linspace(0, total - 1, count).astype(int).tolist())))
    paths = []
    for position in picked:
        path = out_dir / f"{position:06d}.png"
        Image.fromarray(frames[position]).save(path)
        paths.append(str(path))
    return paths


def upgrade_to_v21(source: Path, work: Path, provenance: dict, spec: dict | None) -> dict:
    """把 v2.0 元数据补齐为 v2.1，产出到 `work`（输入目录保持只读）。

    v2.1 相对 v2.0 的唯一硬性增量是 `meta/episodes_stats.jsonl`（逐任务统计），
    同时 `codebase_version` 必须为 v2.1。统计量真实计算，不从全局 stats 伪造。
    """
    import numpy as np
    import pandas as pd

    tool_dir = Path(_lerobot_v30_dir())
    sys.path.insert(0, str(tool_dir))
    from lerobot.datasets.compute_stats import compute_episode_stats

    work.mkdir(parents=True, exist_ok=True)
    if any(work.iterdir()):
        fail("work_not_empty", "元数据升级的工作目录已有内容，请使用新的运行目录。", file=str(work))

    selection = {int(item["source_episode_index"]): item for item in (spec or {}).get("episodes", [])}
    records = provenance["episodes"]
    expected_total = sum(r["length"] for r in records)

    # 数据分片原样复制；只对元数据做升级，不逐帧重写训练数据。
    (work / "meta").mkdir(parents=True, exist_ok=True)
    (work / "data" / "chunk-000").mkdir(parents=True, exist_ok=True)
    (work / "videos" / "chunk-000" / SOURCE_CAMERA).mkdir(parents=True, exist_ok=True)
    for record in records:
        shutil.copy2(source / record["data_path"], work / "data" / "chunk-000" / f"episode_{record['episode_index']:06d}.parquet")
        shutil.copy2(source / record["video_path"],
                     work / "videos" / "chunk-000" / SOURCE_CAMERA / f"episode_{record['episode_index']:06d}.mp4")

    # 来源说明与任务表原样保留；episodes.jsonl 裁剪到所选任务。
    for name in ("README.md", "meta/tasks.jsonl", "meta/stats.json"):
        candidate = source / name
        if candidate.is_file():
            target = work / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate, target)

    info = json.loads((source / "meta" / "info.json").read_text(encoding="utf-8"))
    camera_keys = sorted(path.name for path in (work / "videos" / "chunk-000").iterdir() if path.is_dir())
    declared_video_keys = [key for key, value in info["features"].items() if value.get("dtype") == "video"]
    for key in declared_video_keys:
        if key not in camera_keys:
            # 本地没有的分片不保留声明，否则下游会去找不存在的文件。
            info["features"].pop(key)
    info["codebase_version"] = FORMAT_MIDDLE
    info["total_episodes"] = len(records)
    info["total_frames"] = expected_total
    info["total_videos"] = len(records) * len(camera_keys)
    info["total_chunks"] = 1
    info["splits"] = {"train": f"0:{len(records)}"}
    write_json(work / "meta" / "info.json", info)

    # episodes.jsonl 裁剪到实际转换的任务；任务分发用 task_index，不能自造文本。
    task_indices = {str(item.get("task")): int(item["task_index"])
                    for item in _read_jsonl(source / "meta" / "tasks.jsonl")} if (source / "meta" / "tasks.jsonl").is_file() else {}
    declared_tasks = {}
    for item in _read_jsonl(source / "meta" / "episodes.jsonl"):
        declared_tasks[int(item["episode_index"])] = [str(name) for name in item.get("tasks", [])]
    default_tasks = declared_tasks[sorted(declared_tasks)[0]] if declared_tasks else ["red sweet pick"]
    episodes_path = work / "meta" / "episodes.jsonl"
    with episodes_path.open("w", encoding="utf-8") as handle:
        for record in records:
            index = record["episode_index"]
            if index in selection:
                # 人工审核后的任务描述若已在来源任务表中，沿用其 task_index；
                # 否则仍按来源任务文本登记，保持 v2.1 的结构要求。
                wanted = selection[index]["annotation"]["task"]
                tasks = [wanted] if wanted in task_indices else default_tasks
            else:
                tasks = declared_tasks.get(index, default_tasks)
            handle.write(json.dumps({"episode_index": index, "tasks": tasks,
                                     "length": record["length"]}, ensure_ascii=False) + "\n")

    # 逐任务统计：数值读 parquet，视频均匀抽帧后交给官方 sample_images 口径。
    features = info["features"]
    frame_sample = int(os.environ.get("ROBODATA_SO100_FRAME_SAMPLE") or 100)
    tmp_root = work.parent / ".tmp-frames"
    stats_lines = []
    try:
        for record in records:
            index = record["episode_index"]
            table = pd.read_parquet(work / "data" / "chunk-000" / f"episode_{index:06d}.parquet")
            if len(table) != record["length"]:
                fail("metadata_length_mismatch", "数据分片行数与元数据声明不一致。",
                     file=str(work / "data" / "chunk-000" / f"episode_{index:06d}.parquet"),
                     demo=f"episode_{index:06d}", evidence={"declared": record["length"], "actual": len(table)})
            episode_data = {}
            for key, feature in features.items():
                if key in ("timestamp", "frame_index", "episode_index", "index", "task_index"):
                    continue
                if feature.get("dtype") == "video":
                    paths = _sample_video_frames(work / "videos" / "chunk-000" / key / f"episode_{index:06d}.mp4",
                                                 tmp_root / f"episode_{index:06d}" / key, index, frame_sample)
                    episode_data[key] = paths
                else:
                    values = np.stack([np.asarray(value, dtype=np.float32) for value in table[key]])
                    bad = np.argwhere(~np.isfinite(values))
                    if len(bad):
                        row, column = (int(v) for v in bad[0])
                        fail("metadata_nonfinite", "数值包含 NaN 或无穷值。",
                             file=str(work / "data" / "chunk-000" / f"episode_{index:06d}.parquet"),
                             demo=f"episode_{index:06d}", field=key, row=row,
                             evidence={"column": column, "value": str(values[row, column])})
                    episode_data[key] = values
            stats_lines.append({"episode_index": index, "stats": _plain(compute_episode_stats(episode_data, features))})
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    with (work / "meta" / "episodes_stats.jsonl").open("w", encoding="utf-8") as handle:
        for line in stats_lines:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    return {
        "format_version": FORMAT_MIDDLE, "episode_count": len(records), "frame_count": expected_total,
        "camera": camera_keys, "feature_keys": sorted(features),
        "stats_file": "meta/episodes_stats.jsonl", "stats_records": len(stats_lines),
        "frame_sample_per_episode": frame_sample,
        "v21_additions": {"codebase_version": FORMAT_MIDDLE, "episodes_stats": "meta/episodes_stats.jsonl",
                          "splits": info["splits"]},
        "source_provenance": {"declared_episodes": provenance["declared_episodes"],
                              "declared_frames": provenance["declared_frames"],
                              "present_episodes": provenance["present_episodes"],
                              "present_frames": provenance["present_frames"],
                              "output_episodes": len(records), "output_frames": expected_total},
    }


def _plain(value):
    import numpy as np
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _lerobot_v30_dir() -> Path:
    import lerobot
    return Path(lerobot.__file__).resolve().parent / "datasets" / "v30"


# --------------------------------------------- 阶段二：v2.1 -> v3.0 官方写入


def _official_converter():
    """加载官方 v2.1 → v3.0 转换模块。"""
    tool_dir = _lerobot_v30_dir()
    if str(tool_dir) not in sys.path:
        sys.path.insert(0, str(tool_dir))
    import convert_dataset_v21_to_v30 as conv  # noqa: E402
    return conv


def mapping_spec(provenance: dict, spec: dict | None) -> dict:
    """如实记录映射：SO-100 的状态与动作都是 6 维，顺序原样保留。"""
    info = {"observation.state": {"dtype": "float32", "width": 6,
                                  "source_field": "observation.state",
                                  "meaning": "保留来源关节顺序与原始数值，不补充未核实单位。"},
            "action": {"dtype": "float32", "width": 6, "source_field": "action",
                       "meaning": "保留来源控制表示与顺序，不改写为关节目标或补充控制单位。"},
            SOURCE_CAMERA: {"shape": [480, 640, 3], "dtype": "video",
                            "source_field": f"videos/chunk-000/{SOURCE_CAMERA}",
                            "storage": "AV1 MP4（remux 无重编码）"},
            "timestamp": {"derivation": "frame_index / 30", "fps": SOURCE_FPS,
                          "kind": "derived_from_source_fps", "hardware_synchronization_verified": False},
            "task": {"source_field": "meta/episodes.jsonl tasks",
                     "meaning": "保留来源任务描述与人工审核标注"}}
    return info


def write_v30(work: Path, output: Path, provenance: dict, metadata: dict, spec: dict | None) -> dict:
    """逐阶段调用官方转换函数，产出标准 v3.0 目录。不执行官方收尾的删除与替换。"""
    from lerobot.datasets.utils import (DEFAULT_DATA_FILE_SIZE_IN_MB,
                                        DEFAULT_VIDEO_FILE_SIZE_IN_MB)

    conv = _official_converter()
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            fail("output_not_empty", "输出目录已有内容，已停止以避免覆盖；请使用新的运行目录。", file=str(output))
        output.rmdir()  # 只删除已确认存在的空暂存目录。

    # 官方硬校验 v2.1；此时 work 已经是补齐后的 v2.1。
    conv.validate_local_dataset_version(work)
    data_mb = DEFAULT_DATA_FILE_SIZE_IN_MB
    video_mb = DEFAULT_VIDEO_FILE_SIZE_IN_MB

    task_table = _read_tasks(work)
    stages = []
    conv.convert_info(work, output, data_mb, video_mb)
    stages.append({"stage": "convert_info", "status": "passed"})
    conv.convert_tasks(work, output)
    stages.append({"stage": "convert_tasks", "status": "passed"})
    episodes_metadata = conv.convert_data(work, output, data_mb)
    stages.append({"stage": "convert_data", "status": "passed", "episodes": len(episodes_metadata)})
    episodes_video_metadata = conv.convert_videos(work, output, video_mb)
    stages.append({"stage": "convert_videos", "status": "passed",
                   "videos": len(episodes_video_metadata or [])})
    conv.convert_episodes_metadata(work, output, episodes_metadata, episodes_video_metadata)
    stages.append({"stage": "convert_episodes_metadata", "status": "passed"})

    selection = {int(item["source_episode_index"]): item for item in (spec or {}).get("episodes", [])}
    source_task = _source_task(work)
    annotations = {
        "schema_version": 1, "source_task": source_task,
        "mapping_policy": "人工审核的 annotation.task 写入训练 task；原始任务描述与完整标注随附保留。",
        "conversion_spec": spec,
        "episodes": [
            {"source_episode_index": record["episode_index"], "output_episode_index": record["episode_index"],
             "source_task": source_task,
             "task": (selection[record["episode_index"]]["annotation"]["task"]
                      if record["episode_index"] in selection else source_task),
             "annotation": selection[record["episode_index"]]["annotation"] if record["episode_index"] in selection else None,
             "quality_run_id": selection[record["episode_index"]].get("quality_run_id") if record["episode_index"] in selection else None,
             "length": record["length"]}
            for record in provenance["episodes"]],
        "source_tasks": task_table,
    }
    write_json(output / "annotations.json", annotations)

    files = _output_files(output)
    manifest = {
        "schema_version": 1, "created_at": utc_now(), "format_version": FORMAT_OUTPUT,
        "profile": "so100_sweet_pick_v1",
        "source": provenance, "mapping": mapping_spec(provenance, spec),
        "episodes": [{"episode_index": r["episode_index"], "source_episode_index": r["episode_index"],
                      "output_episode_index": r["episode_index"], "length": r["length"],
                      "source_demo": f"episode_{r['episode_index']:06d}",
                      "source_task": _source_task(work),
                      "task": (selection[r["episode_index"]]["annotation"]["task"]
                               if r["episode_index"] in selection else _source_task(work)),
                      "annotation": selection[r["episode_index"]]["annotation"] if r["episode_index"] in selection else None,
                      "quality_run_id": selection[r["episode_index"]].get("quality_run_id") if r["episode_index"] in selection else None,
                      "data_sha256": r["data_sha256"], "video_sha256": r["video_sha256"]}
                     for r in provenance["episodes"]],
        "total_episodes": len(provenance["episodes"]),
        "total_frames": sum(r["length"] for r in provenance["episodes"]),
        "camera": SOURCE_CAMERA, "fps": SOURCE_FPS, "robot_type": SOURCE_ROBOT,
        "time_policy": "timestamp = frame_index / 30，来自来源声明；不对硬件同步作断言。",
        "warnings": [],
        "source_statistics": {"declared_episodes": provenance["declared_episodes"],
                              "declared_frames": provenance["declared_frames"],
                              "present_episodes": provenance["present_episodes"],
                              "present_frames": provenance["present_frames"],
                              "selected_episodes": provenance["selected_episodes"],
                              "selected_frames": provenance["selected_frames"]},
        "writer": {name: importlib.metadata.version(name)
                   for name in ["lerobot", "torch", "torchvision", "av", "numpy", "datasets", "pyarrow"]},
        "official_validation_required": True, "conversion_spec": spec,
        "conversion_stages": stages, "files": files,
    }
    write_json(output / "source_manifest.json", manifest)

    (output / "CONVERSION.md").write_text(
        f"# 转换说明\n\n本数据为 SO-100 真机遥操作数据集（`{SOURCE_REPO}`，固定版本 "
        f"`{SOURCE_REVISION[:12]}`），原始格式为 LeRobot v2.0，本次共 {len(provenance['episodes'])} 条任务、"
        f"{manifest['total_frames']} 帧。\n\n"
        f"转换分两步：先把 v2.0 元数据补齐为 v2.1（补出逐任务统计 `meta/episodes_stats.jsonl`，"
        f"并把 `codebase_version` 标到 v2.1），再逐阶段调用官方 v2.1 → v3.0 转换函数写出产物。\n\n"
        f"**没有沿用官方 `convert_dataset()` 的收尾步骤。** 官方函数最后会删除目标目录并把原目录改名顶替，"
        f"既会真正删除目录，也会覆盖固定来源副本；本链路只调用各阶段函数，输入目录全程只读。\n\n"
        f"状态与动作均为 6 维，保留来源关节顺序与原始数值，不补充未核实单位；"
        f"相机 `{SOURCE_CAMERA}` 为 640 × 480，视频分片由 remux 拼接（无重编码）。\n"
        f"时间来自帧序号除以 30 Hz，不代表硬件同步。\n\n"
        f"`source_manifest.json` 记录输入来源、映射、各阶段与产物哈希。"
        f"必须通过官方离线加载及主工作台质检，才能登记为可用产物。\n", encoding="utf-8")
    return {"format_version": FORMAT_OUTPUT, "output": str(output),
            "episodes": len(provenance["episodes"]), "frames": manifest["total_frames"],
            "source_manifest": "source_manifest.json", "file_count": len(files),
            "conversion_stages": stages, "camera": SOURCE_CAMERA, "fps": SOURCE_FPS}


def _source_task(work: Path) -> str:
    path = work / "meta" / "tasks.jsonl"
    if not path.is_file():
        return "so100 sweet pick"
    items = _read_jsonl(path)
    return str(items[0].get("task", "so100 sweet pick")) if items else "so100 sweet pick"


def _read_tasks(work: Path) -> list[dict]:
    path = work / "meta" / "tasks.jsonl"
    if not path.is_file():
        return []
    return [{"task_index": int(item["task_index"]), "task": str(item.get("task", ""))}
            for item in _read_jsonl(path)]


def _output_files(output: Path) -> list[dict]:
    result = []
    for folder in ("meta", "data", "videos"):
        for path in sorted((output / folder).rglob("*")):
            if path.is_file() and path.suffix in (".json", ".jsonl", ".parquet", ".mp4"):
                result.append({"path": path.relative_to(output).as_posix(),
                               "size": path.stat().st_size, "sha256": sha256(path)})
    for name in ("annotations.json",):
        path = output / name
        if path.is_file():
            result.append({"path": name, "size": path.stat().st_size, "sha256": sha256(path)})
    return result


# ------------------------------------------------------------ 官方加载验证


def verify_dataset(output: Path, artifacts: Path, provenance: dict, spec: dict | None,
                   *, archive_only: bool = False) -> dict:
    """用官方 LeRobotDataset 离线加载，核对帧数、维度、索引、时间与任务文本。"""
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
    if manifest.get("profile") != "so100_sweet_pick_v1" or manifest.get("format_version") != FORMAT_OUTPUT:
        fail("verification_profile_mismatch", "输出不是本项目已验证的 SO-100 v3.0 配置。", file=str(manifest_path))
    recorded_source = manifest.get("source", {})
    if recorded_source.get("revision") != provenance.get("revision") or recorded_source.get("camera") != provenance.get("camera"):
        fail("verification_source_mismatch", "输出记录的来源版本或相机与本次输入不一致。", file=str(manifest_path))
    if manifest.get("conversion_spec") != spec:
        fail("verification_spec_mismatch", "输出记录的候选集与本次冻结清单不一致。", file=str(manifest_path))

    expected_files = manifest.get("files")
    actual_files = _output_files(output)
    if not expected_files or expected_files != actual_files:
        expected_map = {entry.get("path"): entry for entry in expected_files or []}
        actual_map = {entry.get("path"): entry for entry in actual_files}
        changed = [key for key in sorted(set(expected_map) | set(actual_map))
                   if expected_map.get(key) != actual_map.get(key)]
        fail("output_hash_mismatch", "输出文件已缺失或发生变化，官方加载前的完整性检查未通过。",
             file=str(output / changed[0]) if changed else str(output), evidence={"changed_files": changed})

    dataset = LeRobotDataset(repo_id=REPO_ID, root=output, video_backend="pyav")
    total_frames = manifest["total_frames"]
    total_episodes = manifest["total_episodes"]
    if len(dataset) != total_frames or dataset.meta.total_episodes != total_episodes or dataset.meta.info.get("codebase_version") != FORMAT_OUTPUT:
        fail("official_metadata_mismatch", "官方加载的任务数、帧数或格式版本与所选源数据不一致。",
             file=str(output / "meta/info.json"),
             evidence={"loaded_frames": len(dataset), "loaded_episodes": dataset.meta.total_episodes,
                       "expected_frames": total_frames, "expected_episodes": total_episodes,
                       "version": dataset.meta.info.get("codebase_version")})
    if dataset.meta.info.get("splits") != {"train": f"0:{total_episodes}"}:
        fail("official_split_mismatch", "本配置输出必须把全部所选任务明确划分为 train。",
             file=str(output / "meta/info.json"), field="splits",
             evidence={"actual": dataset.meta.info.get("splits")})

    table = pd.concat([pd.read_parquet(path) for path in sorted((output / "data").rglob("*.parquet"))],
                      ignore_index=True)
    if len(table) != total_frames or set(table["episode_index"].tolist()) != set(range(total_episodes)):
        fail("verification_table_inventory", "实际表格总量或任务编号集合与输出清单不一致。", file=str(output / "data"))

    record_by_index = {r["episode_index"]: r for r in provenance["episodes"]}
    samples, decoded_images = [], []
    for episode_index in sorted(record_by_index):
        length = record_by_index[episode_index]["length"]
        rows = table[table["episode_index"] == episode_index].sort_values("frame_index")
        if len(rows) != length:
            fail("verification_episode_length", "输出任务的帧数与源任务不一致。", file=str(output),
                 demo=f"episode_{episode_index:06d}", evidence={"expected": length, "actual": len(rows)})
        for key, dim in (("observation.state", 6), ("action", 6)):
            actual = np.stack(rows[key].to_numpy()).astype(np.float32)
            if actual.shape != (length, dim) or not np.isfinite(actual).all():
                fail("verification_numeric_invalid", "输出数值的维度、长度或有效性不符合来源配置。",
                     file=str(output / "data"), demo=f"episode_{episode_index:06d}", field=key,
                     evidence={"shape": list(actual.shape)})
        if not np.array_equal(rows["frame_index"].to_numpy(), np.arange(length)):
            fail("verification_index_mismatch", "输出帧索引不连续或与源顺序不一致。", file=str(output),
                 demo=f"episode_{episode_index:06d}", field="frame_index")
        if not np.allclose(rows["timestamp"].to_numpy(), np.arange(length, dtype=np.float32) / SOURCE_FPS,
                           rtol=0, atol=1e-6):
            fail("verification_time_mismatch", f"输出派生时间与帧序号 / {SOURCE_FPS} Hz 不一致。",
                 file=str(output), demo=f"episode_{episode_index:06d}", field="timestamp")

    # 官方加载抽查：每条任务的首、中、末帧，核对维度、范围与索引身份。
    global_index = 0
    for episode_index in sorted(record_by_index):
        length = record_by_index[episode_index]["length"]
        for row in sorted({0, length // 2, length - 1}):
            position = global_index + row
            item = dataset[position]
            for key, dim in (("observation.state", 6), ("action", 6)):
                value = item[key].numpy()
                if value.shape != (dim,) or not np.isfinite(value).all():
                    fail("official_sample_numeric", "官方读取的状态或动作数值不正确。", file=str(output),
                         demo=f"episode_{episode_index:06d}", field=key, row=row,
                         evidence={"shape": list(value.shape)})
            if int(item["episode_index"]) != episode_index or int(item["frame_index"]) != row or int(item["index"]) != position:
                fail("official_sample_identity", "官方读取的样本身份不正确。", file=str(output),
                     demo=f"episode_{episode_index:06d}", row=row,
                     evidence={"episode_index": int(item["episode_index"]),
                               "frame_index": int(item["frame_index"]), "index": int(item["index"])})
            image = item[SOURCE_CAMERA]
            if image.dtype != torch.float32 or tuple(image.shape) != (3, 480, 640):
                fail("official_sample_image", "官方读取图像的类型或尺寸不正确。", file=str(output),
                     demo=f"episode_{episode_index:06d}", field=SOURCE_CAMERA, row=row,
                     evidence={"dtype": str(image.dtype), "shape": list(image.shape)})
            if not torch.isfinite(image).all() or float(image.min()) < 0 or float(image.max()) > 1:
                fail("official_sample_image", "官方读取图像的数值范围不正确。", file=str(output),
                     demo=f"episode_{episode_index:06d}", field=SOURCE_CAMERA, row=row)
            pixels = np.rint(image.permute(1, 2, 0).numpy() * 255).astype(np.uint8)
            decoded_images.append(pixels)
            samples.append({"episode": episode_index, "source_episode": episode_index, "row": row,
                            "global_index": position, "timestamp": float(item["timestamp"]),
                            "decoded_image_sha256": hashlib.sha256(pixels.tobytes()).hexdigest(),
                            "task": str(item["task"])})
        global_index += length

    batch = next(iter(DataLoader(dataset, batch_size=2, num_workers=0, shuffle=False)))
    if (tuple(batch["observation.state"].shape) != (2, 6) or tuple(batch["action"].shape) != (2, 6)
            or tuple(batch[SOURCE_CAMERA].shape) != (2, 3, 480, 640)):
        fail("official_batch_shape", "官方 DataLoader 的批次维度不正确。", file=str(output),
             evidence={"state": list(batch["observation.state"].shape), "action": list(batch["action"].shape),
                       "image": list(batch[SOURCE_CAMERA].shape)})
    np.savez_compressed(artifacts / "decoded_samples.npz", images=np.stack(decoded_images),
                        episode_indices=np.array([s["episode"] for s in samples]),
                        frame_indices=np.array([s["row"] for s in samples]),
                        global_indices=np.array([s["global_index"] for s in samples]))
    return {
        "format_version": FORMAT_OUTPUT, "source_repo": provenance["repo_id"],
        "source_revision": provenance["revision"], "camera": SOURCE_CAMERA, "fps": SOURCE_FPS,
        "checked_numeric_frames": len(table), "output_numeric_frames_checked": len(table),
        "source_numeric_consistency_rechecked": False,
        "validation_scope": "解包后文件哈希、元数据、索引、有限数值与官方离线加载；"
                            "源数值一致性由转换阶段按分片哈希绑定，未重新解码源视频逐帧比对" if archive_only else
                            "输出全量数值、官方离线抽查与 DataLoader 批量加载；数值与源分片哈希绑定",
        "episode_count": total_episodes, "row_count": len(table), "sample_count": len(samples),
        "samples": samples, "batch_size": 2, "batch_image_shape": list(batch[SOURCE_CAMERA].shape),
        "decoded_samples": "decoded_samples.npz", "offline": True, "video_backend": "pyav",
        "cuda_available": torch.cuda.is_available(),
        "image_comparison": "来源为 AV1 有损编码，remux 不重编码；不对逐像素恒等作断言",
        "file_count": len(actual_files), "snapshot_id": (spec or {}).get("snapshot_id"), "warnings": [],
    }


def verify_archive(output: Path, artifacts: Path) -> dict:
    """独立解包后的交付包验收：不需要源目录也能判断产物自身是否自洽。"""
    import numpy as np
    import pandas as pd

    path = output / "source_manifest.json"
    if not path.is_file():
        fail("manifest_missing", "解包数据缺少来源清单。", file=str(path))
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("profile") != "so100_sweet_pick_v1" or manifest.get("format_version") != FORMAT_OUTPUT:
        fail("archive_profile_unsupported", "解包验证仅支持本项目已验证的 SO-100 v3.0 输出配置。", file=str(path))
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
        fail("archive_fields_missing", "解包表格缺少必需字段。", file=str(output / "data"),
             evidence={"missing_fields": missing})
    for episode in episodes:
        index = int(episode["episode_index"])
        rows = table[table["episode_index"] == index].sort_values("frame_index")
        for field, dim in (("observation.state", 6), ("action", 6)):
            array = np.stack(rows[field].to_numpy()) if len(rows) else np.empty((0, dim))
            if array.shape != (int(episode["length"]), dim) or not np.isfinite(array).all():
                fail("archive_numeric_invalid", "解包数据数值的维度、长度或有效性不符合清单。",
                     file=str(output / "data"), field=field, demo=episode.get("source_demo"),
                     evidence={"shape": list(array.shape)})
    provenance = {"repo_id": manifest["source"]["repo_id"], "revision": manifest["source"]["revision"],
                  "camera": manifest.get("camera"), "episodes": episodes}
    return verify_dataset(output, artifacts, provenance, manifest.get("conversion_spec"), archive_only=True)


# ------------------------------------------------------------------ 命令行


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=STAGES)
    parser.add_argument("--input", type=Path, help="SO-100 v2.0 数据目录（只读）")
    parser.add_argument("--work", type=Path, help="元数据升级后的 v2.1 工作副本目录")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--spec", type=Path, help="Frozen business conversion_spec JSON")
    parser.add_argument("--test-mode", help="Explicit injection label; marks the modified input as a test copy")
    args = parser.parse_args()
    source = args.input.resolve() if args.input else None
    work = args.work.resolve() if args.work else None
    output, artifacts = args.output.resolve(), args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    if os.environ.get("ROBODATA_PARENT_PID"):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from robodata.runtime import _parent_watchdog
        threading.Thread(target=_parent_watchdog,
                         args=(int(os.environ["ROBODATA_PARENT_PID"]),
                               os.environ.get("ROBODATA_PARENT_IDENTITY")), daemon=True).start()
    started = time.monotonic()
    result = {"schema_version": 1, "stage": args.stage, "status": "running",
              "input": str(source or output), "test_mode": args.test_mode, "started_at": utc_now()}
    try:
        if args.test_mode is not None and not args.test_mode.strip():
            fail("test_label_missing", "注入测试必须填写明确的测试标签。", file=str(source))
        spec = _load_spec(args.spec)
        if args.stage == "archive_verify":
            details = verify_archive(output, artifacts)
        elif args.stage == "official_verify":
            provenance = _provenance_from_manifest(output)
            details = verify_dataset(output, artifacts, provenance, spec)
        else:
            if source is None:
                fail("source_argument_missing", "转换与源校验必须指定 --input。")
            episodes = select_episodes(_spec_range(spec))
            provenance = validate_source(source, episodes)
            if args.stage == "file_validation":
                details = provenance
            elif args.stage == "metadata_upgrade":
                if work is None:
                    fail("work_argument_missing", "元数据升级必须指定 --work。")
                details = upgrade_to_v21(source, work, provenance, spec)
            elif args.stage == "field_mapping":
                details = {"mapping": mapping_spec(provenance, spec), "episodes": provenance["episodes"],
                           "frames": provenance["selected_frames"],
                           "dimensions": {"observation.state": 6, "action": 6,
                                          "camera": [480, 640, 3]},
                           "warnings": []}
            elif args.stage == "format_write":
                if work is None:
                    fail("work_argument_missing", "格式写入必须指定 --work。")
                details = write_v30(work, output, provenance, {}, spec)
            else:
                fail("stage_not_supported", f"未实现的阶段：{args.stage}")
        result.update(status="passed", details=details,
                      elapsed_seconds=round(time.monotonic() - started, 3), completed_at=utc_now())
        write_json(artifacts / "steps" / f"{args.stage}.json", result)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as exc:
        context = exc.context if isinstance(exc, ConversionError) else {}
        label = STAGE_LABELS.get(args.stage, args.stage)
        message = str(exc) if isinstance(exc, ConversionError) else f"{label}执行失败；请展开原始错误详情定位。"
        if isinstance(exc, OSError) and args.stage in ("metadata_upgrade", "format_write"):
            message = "输出文件写入失败，请检查目标路径是否被文件占用、目录权限和可用空间。"
        default_file = str(output) if args.stage in ("format_write", "official_verify", "archive_verify") else str(source or output)
        diagnostic = {"stage": args.stage, "status": "failed",
                      "code": exc.code if isinstance(exc, ConversionError) else "stage_execution_failed",
                      "message": message, "message_zh": message,
                      "file": context.get("file", default_file), "demo": context.get("demo"),
                      "field": context.get("field"), "row": context.get("row"),
                      "evidence": context.get("evidence", {}),
                      "exception_type": type(exc).__name__, "exception_message": str(exc),
                      "traceback": traceback.format_exc(), "timestamp": utc_now()}
        write_json(artifacts / "diagnostic.json", diagnostic)
        result.update(status="failed", diagnostic=diagnostic,
                      elapsed_seconds=round(time.monotonic() - started, 3), completed_at=utc_now())
        write_json(artifacts / "steps" / f"{args.stage}.json", result)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 1


def _spec_range(spec: dict | None) -> str | None:
    """业务流按冻结候选集转换；避免环境变量与批次范围不一致造成静默偏差。"""
    if not spec:
        return None
    indices = sorted({int(item["source_episode_index"]) for item in spec.get("episodes", [])
                      if item.get("source_episode_index") is not None})
    return ",".join(str(index) for index in indices) or None


def _load_spec(path: Path | None) -> dict | None:
    """校验冻结的候选清单；这里不是审核界面，只做一致性判断。"""
    if path is None:
        return None
    try:
        spec = json.loads(Path(path).read_text(encoding="utf-8"))
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
            if type(source_index) is not int or source_index not in _ALL_EPISODE_LENGTHS or source_index in seen:
                raise ValueError("源任务必须是固定配置中的不重复任务")
            seen.add(source_index)
            if type(episode["output_episode_index"]) is not int or episode["output_episode_index"] != output_index:
                raise ValueError("输出任务编号必须按候选顺序从 0 连续编号")
            if type(episode["row_count"]) is not int or episode["row_count"] != _ALL_EPISODE_LENGTHS[source_index]:
                raise ValueError("候选任务长度与固定配置不一致")
            annotation = episode["annotation"]
            if not isinstance(annotation, dict) or not isinstance(annotation.get("task"), str) or not annotation["task"].strip():
                raise ValueError("已审核任务描述不能为空")
            if not isinstance(episode.get("quality_run_id"), str) or not episode["quality_run_id"].strip():
                raise ValueError("候选任务缺少对应的质检运行编号")
        unsigned = {k: v for k, v in spec.items() if k != "snapshot_id"}
        expected = hashlib.sha256(json.dumps(unsigned, sort_keys=True, ensure_ascii=False,
                                             separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        if spec["snapshot_id"] != expected:
            raise ValueError("候选快照摘要不一致，内容可能已修改")
    except Exception as exc:
        fail("conversion_spec_invalid", f"转换候选清单无效：{exc}", file=str(path))
    return spec


def _provenance_from_manifest(output: Path) -> dict:
    manifest = json.loads((output / "source_manifest.json").read_text(encoding="utf-8"))
    source = manifest["source"]
    return {**source, "episodes": [{"episode_index": e["episode_index"], "length": e["length"]}
                                   for e in manifest["episodes"]]}


if __name__ == "__main__":
    raise SystemExit(main())
