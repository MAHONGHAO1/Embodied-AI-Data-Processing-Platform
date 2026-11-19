"""Finalize a Matching Cable preview into an annotated LeRobot v3 delivery."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import traceback

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from robodata.matching import (CAMERAS, CAMERA_SOURCE_FIELDS, FPS, OPTIONAL_FEATURE_KEYS,
                               OPTIONAL_NUMERIC_FIELDS, SOURCE_KIND, raw_frames)

PROFILE = "matching_cable_frames_v1"
REUSE_SCHEMA_VERSION = 1


class PreviewReuseUnavailable(Exception):
    """The preview cannot be reused safely; the caller must fall back to re-encoding."""


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_spec(path: Path | None) -> dict | None:
    if path is None:
        return None
    spec = json.loads(path.read_text(encoding="utf-8"))
    if not spec.get("episodes"):
        raise ValueError("候选集为空")
    return spec


def source_manifest(source: Path) -> dict:
    manifest = json.loads((source / "source_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("profile") != PROFILE or manifest.get("format_version") != "v3.0":
        raise ValueError("输入不是 Matching Cable 预览产物")
    if manifest.get("source", {}).get("kind") != SOURCE_KIND:
        raise ValueError("输入来源类型不是 Matching Cable")
    cameras = manifest.get("source", {}).get("cameras")
    if not isinstance(cameras, list) or not cameras or len(set(cameras)) != len(cameras):
        raise ValueError("Matching Cable 清单必须声明至少一路不重复的 RGB 相机")
    if any(camera not in CAMERAS for camera in cameras):
        raise ValueError("Matching Cable 清单包含未知相机")
    if manifest.get("source", {}).get("camera") != cameras[0]:
        raise ValueError("Matching Cable 默认相机与相机列表不一致")
    if set(manifest.get("camera_metadata", {})) != set(cameras):
        raise ValueError("Matching Cable 相机元数据与相机列表不一致")
    return manifest


def candidate_rows(spec: dict, manifest: dict) -> dict[int, int]:
    rows = {int(item["episode_index"]): int(item["length"]) for item in manifest["episodes"]}
    result = {}
    output_indices = set()
    for item in spec["episodes"]:
        source_index = int(item["source_episode_index"])
        output_index = int(item["output_episode_index"])
        if source_index not in rows:
            raise ValueError(f"候选 Episode {source_index} 不在预览产物中")
        if int(item["row_count"]) != rows[source_index]:
            raise ValueError(f"候选 Episode {source_index} 帧数不一致")
        if source_index in result:
            raise ValueError(f"候选集重复包含源 Episode {source_index}")
        if output_index in output_indices:
            raise ValueError(f"候选集重复使用输出 Episode {output_index}")
        output_indices.add(output_index)
        result[source_index] = rows[source_index]
    return result


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _shape(value) -> tuple[int, ...]:
    return tuple(int(item) for item in value)


def _record_path(root: Path, record: dict, label: str) -> Path:
    root = root.resolve()
    path = (root / str(record.get("path"))).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise PreviewReuseUnavailable(f"{label}缺失或路径越界：{record.get('path')}")
    return path


def _verify_file_record(root: Path, record: dict, label: str) -> Path:
    path = _record_path(root, record, label)
    if path.stat().st_size != int(record["size"]):
        raise PreviewReuseUnavailable(f"{label}大小已变化：{record.get('path')}")
    if sha256(path) != record.get("sha256"):
        raise PreviewReuseUnavailable(f"{label}哈希已变化：{record.get('path')}")
    return path


def _verify_selected_source(manifest: dict, source_root: Path, selected_ids: set[int]) -> list[dict]:
    """Hash every selected source frame again before trusting preview provenance."""
    root = source_root.resolve()
    expected = [item for item in manifest.get("files", [])
                if isinstance(item, dict) and int(item.get("episode_id", -1)) in selected_ids]
    if not expected:
        raise ValueError("预览清单缺少已选 Episode 的源文件记录")
    for item in expected:
        path = (root / str(item.get("path"))).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError(f"Matching Cable 原始帧文件缺失或路径越界：{item.get('path')}")
        if (path.stat().st_size != int(item["size"]) or path.stat().st_mtime_ns != int(item["mtime_ns"])
                or sha256(path) != item.get("sha256")):
            raise ValueError("Matching Cable 原始帧文件已变化，请重新导入；当前检查和产物不可继续使用")
    return expected


def _preview_meta(preview: Path) -> dict[int, dict]:
    import pandas as pd

    files = sorted((preview / "meta" / "episodes").rglob("*.parquet"))
    if not files:
        raise PreviewReuseUnavailable("预览缺少 meta/episodes 元数据")
    table = pd.concat([pd.read_parquet(path) for path in files], ignore_index=True)
    return {int(row["episode_index"]): row for _, row in table.iterrows()}


def _preview_reuse_context(preview: Path, manifest: dict, records: dict[int, dict], raw_ids: dict[int, int],
                           candidates: list[dict], cameras: list[str], capabilities: dict,
                           shapes: dict[str, tuple[int, int, int]]) -> dict:
    import numpy as np
    from matching_preview import feature_definitions

    reuse = manifest.get("reuse")
    if not isinstance(reuse, dict):
        raise PreviewReuseUnavailable("预览产物没有单集视频复用清单；请用 v0.13 重新导入")
    if int(reuse.get("schema_version", -1)) != REUSE_SCHEMA_VERSION:
        raise PreviewReuseUnavailable("预览复用清单版本不兼容")
    if reuse.get("layout") != "one_video_per_episode":
        raise PreviewReuseUnavailable("预览不是一集一相机一个 MP4 的布局")
    if float(reuse.get("fps", 0)) != float(FPS):
        raise PreviewReuseUnavailable("预览复用清单帧率不是 20 FPS")

    for item in reuse.get("meta_files", []):
        _verify_file_record(preview, item, "预览元数据")

    info = _load_json(preview / "meta/info.json")
    expected_features = feature_definitions(shapes, capabilities["optional_present"])
    expected_keys = set(expected_features) | {"timestamp", "frame_index", "episode_index", "index", "task_index"}
    actual_keys = set(info.get("features", {}))
    if actual_keys != expected_keys:
        raise PreviewReuseUnavailable("预览特征集与当前源字段不一致")
    for key, expected in expected_features.items():
        actual = info["features"].get(key, {})
        if actual.get("dtype") != expected["dtype"] or _shape(actual.get("shape", [])) != _shape(expected["shape"]):
            raise PreviewReuseUnavailable(f"预览特征 {key} 的 dtype/shape 与当前源不一致")
    if float(info.get("fps", 0)) != float(FPS):
        raise PreviewReuseUnavailable("预览 info.fps 不是 20 FPS")
    if int(info.get("total_episodes", -1)) != int(manifest["total_episodes"]):
        raise PreviewReuseUnavailable("预览 info.total_episodes 与清单不一致")
    if int(info.get("total_frames", -1)) != int(manifest["total_frames"]):
        raise PreviewReuseUnavailable("预览 info.total_frames 与清单不一致")

    entries = {int(item["episode_index"]): item for item in reuse.get("episodes", [])}
    meta = _preview_meta(preview)
    tables = {}
    selected = {}
    seen_videos = set()
    for candidate in candidates:
        source_index = int(candidate["source_episode_index"])
        output_index = int(candidate["output_episode_index"])
        record = records[source_index]
        entry = entries.get(source_index)
        if entry is None:
            raise PreviewReuseUnavailable(f"预览复用清单缺少 Episode {source_index}")
        if int(entry["length"]) != int(record["length"]):
            raise PreviewReuseUnavailable(f"预览 Episode {source_index} 长度与清单不一致")
        if int(entry["source_episode_id"]) != int(raw_ids[source_index]):
            raise PreviewReuseUnavailable(f"预览 Episode {source_index} 源编号与清单不一致")
        meta_row = meta.get(source_index)
        if meta_row is None:
            raise PreviewReuseUnavailable(f"预览 meta/episodes 缺少 Episode {source_index}")
        if int(meta_row["length"]) != int(record["length"]):
            raise PreviewReuseUnavailable(f"预览 meta/episodes Episode {source_index} 长度不一致")

        data_path = _verify_file_record(preview, entry["data"], "预览数值 parquet")
        expected_data = f"data/chunk-{int(meta_row['data/chunk_index']):03d}/file-{int(meta_row['data/file_index']):03d}.parquet"
        if entry["data"]["path"] != expected_data:
            raise PreviewReuseUnavailable(f"预览 Episode {source_index} 数值 parquet 元数据不一致")

        videos = entry.get("videos")
        if not isinstance(videos, dict) or set(videos) != set(cameras):
            raise PreviewReuseUnavailable(f"预览 Episode {source_index} 相机视频不完整")
        for camera in cameras:
            video = videos[camera]
            relative = video["path"]
            if relative in seen_videos:
                raise PreviewReuseUnavailable(f"预览视频被多个 Episode 共用：{relative}")
            seen_videos.add(relative)
            expected_video = f"videos/{camera}/chunk-{int(meta_row[f'videos/{camera}/chunk_index']):03d}/" \
                             f"file-{int(meta_row[f'videos/{camera}/file_index']):03d}.mp4"
            if relative != expected_video:
                raise PreviewReuseUnavailable(f"预览 Episode {source_index} {camera} 视频路径元数据不一致")
            if float(video["from_timestamp"]) != 0.0 or float(video["to_timestamp"]) <= 0.0:
                raise PreviewReuseUnavailable(f"预览 Episode {source_index} {camera} 视频不是单集")
            if float(video["to_timestamp"]) != float(meta_row[f"videos/{camera}/to_timestamp"]):
                raise PreviewReuseUnavailable(f"预览 Episode {source_index} {camera} 视频终点时间不一致")
            if int(video["frame_count"]) != int(record["length"]):
                raise PreviewReuseUnavailable(f"预览 Episode {source_index} {camera} 视频帧数不一致")
            expected_height, expected_width = [int(value) for value in shapes[camera][:2]]
            if (int(video["height"]), int(video["width"])) != (expected_height, expected_width):
                raise PreviewReuseUnavailable(f"预览 Episode {source_index} {camera} 视频分辨率不一致")
            if video.get("has_audio"):
                raise PreviewReuseUnavailable(f"预览 Episode {source_index} {camera} 视频含音频轨")
            for key in ("min", "max", "mean", "std", "count", "q01", "q10", "q50", "q90", "q99"):
                if f"stats/{camera}/{key}" not in meta_row:
                    raise PreviewReuseUnavailable(f"预览 Episode {source_index} {camera} 缺 {key} 统计")

        try:
            import pandas as pd
            table = pd.read_parquet(data_path)
        except Exception as exc:
            raise PreviewReuseUnavailable(f"预览 Episode {source_index} 数值 parquet 无法读取：{exc}") from exc
        table = table[table["episode_index"].astype(int) == source_index].reset_index(drop=True)
        if len(table) != int(record["length"]):
            raise PreviewReuseUnavailable(f"预览 Episode {source_index} 数值行数不一致")
        frame_index = table["frame_index"].to_numpy(dtype="int64")
        if not np.array_equal(frame_index, np.arange(len(table), dtype="int64")):
            raise PreviewReuseUnavailable(f"预览 Episode {source_index} frame_index 不连续")
        expected_timestamp = frame_index.astype("float64") / float(FPS)
        # The preview stores timestamps as float32; at ~100 s the spacing is
        # already ~8e-6 s, so a 1e-6 s check would reject valid long episodes.
        if not (abs(table["timestamp"].to_numpy(dtype="float64") - expected_timestamp) <= 1e-4).all():
            raise PreviewReuseUnavailable(f"预览 Episode {source_index} timestamp 不是 20 FPS 派生值")
        if table["index"].duplicated().any():
            raise PreviewReuseUnavailable(f"预览 Episode {source_index} 全局 index 重复")
        for key, feature in expected_features.items():
            if feature["dtype"] == "video":
                continue
            if key not in table:
                raise PreviewReuseUnavailable(f"预览 Episode {source_index} 缺少字段 {key}")
            try:
                arrays = np.stack([np.asarray(value, dtype=np.float32) for value in table[key].to_numpy()])
            except ValueError as exc:
                raise PreviewReuseUnavailable(f"预览 Episode {source_index} 字段 {key} 形状不一致") from exc
            if not np.isfinite(arrays).all():
                raise PreviewReuseUnavailable(f"预览 Episode {source_index} 字段 {key} 含非有限值")
        tables[source_index] = table
        selected[source_index] = {"candidate": candidate, "entry": entry, "meta": meta_row,
                                  "data_path": data_path, "table": table, "output_index": output_index}
    return {"reuse": reuse, "info": info, "entries": entries, "meta": meta, "selected": selected,
            "tables": tables}


def _feature_dtype(feature) -> str:
    import datasets
    if isinstance(feature, datasets.Value):
        return feature.dtype
    if isinstance(feature, (datasets.Sequence, datasets.List)):
        return feature.feature.dtype
    return feature.dtype


def _episode_parquet(output: Path, output_index: int, table, hf_features, task_index: int,
                     global_index: int) -> dict:
    import datasets
    import numpy as np

    ep_dict = {}
    for key, feature in hf_features.items():
        dtype = np.dtype(_feature_dtype(feature))
        if key == "episode_index":
            values = np.full(len(table), output_index, dtype=dtype)
        elif key == "index":
            values = np.arange(global_index, global_index + len(table), dtype=dtype)
        elif key == "task_index":
            values = np.full(len(table), task_index, dtype=dtype)
        elif isinstance(feature, datasets.Value):
            values = np.asarray(table[key].to_numpy(), dtype=dtype).reshape(-1)
        else:
            values = np.stack([np.asarray(value, dtype=dtype) for value in table[key].to_numpy()])
        ep_dict[key] = values
    dataset = datasets.Dataset.from_dict(ep_dict, features=hf_features, split="train")
    chunk, file_index = divmod(output_index, 1000)
    relative = f"data/chunk-{chunk:03d}/file-{file_index:03d}.parquet"
    path = output / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    dataset.to_parquet(str(path))
    return {"path": relative, "size": path.stat().st_size, "sha256": sha256(path)}


def _preview_video_stats(meta_row, cameras: list[str]) -> dict:
    import numpy as np

    def scalar(value) -> float:
        current = value
        while isinstance(current, (np.ndarray, list, tuple)):
            flattened = np.asarray(current, dtype=object).reshape(-1)
            if len(flattened) != 1:
                raise PreviewReuseUnavailable("预览视频统计形状不是 3x1x1")
            current = flattened[0]
        return float(current)

    def channel_stats(value) -> np.ndarray:
        raw = np.asarray(value, dtype=object)
        if raw.dtype == object:
            if raw.size != 3:
                raise PreviewReuseUnavailable("预览视频统计形状不是 3x1x1")
            return np.asarray([scalar(item) for item in raw], dtype=np.float64).reshape(3, 1, 1)
        return np.asarray(value, dtype=np.float64).reshape(3, 1, 1)

    stats = {}
    for camera in cameras:
        stats[camera] = {}
        for key in ("min", "max", "mean", "std", "count", "q01", "q10", "q50", "q90", "q99"):
            value = meta_row[f"stats/{camera}/{key}"]
            if key == "count":
                array = np.asarray(value, dtype=np.int64).reshape(1)
                if int(array[0]) <= 0:
                    raise PreviewReuseUnavailable(f"预览 {camera} 视频统计计数无效")
            else:
                array = channel_stats(value)
                if not np.isfinite(array).all():
                    raise PreviewReuseUnavailable(f"预览 {camera} 视频统计含非有限值")
            stats[camera][key] = array
    return stats


def _numeric_stats(table, features: dict) -> dict:
    import numpy as np
    from lerobot.datasets.compute_stats import compute_episode_stats

    stats_features = {key: value for key, value in features.items() if value.get("dtype") != "video"}
    episode_data = {}
    for key, feature in stats_features.items():
        shape = _shape(feature.get("shape", []))
        dtype = np.dtype(feature["dtype"])
        if shape == (1,):
            episode_data[key] = np.asarray(table[key].to_numpy(), dtype=dtype).reshape(-1, 1)
        else:
            episode_data[key] = np.stack([np.asarray(value, dtype=dtype) for value in table[key].to_numpy()])
    return compute_episode_stats(episode_data, stats_features)


def _copy_video(preview: Path, output: Path, output_index: int, camera: str, video: dict,
                length: int) -> dict:
    from matching_preview import probe_video

    source = _record_path(preview, video, "预览视频")
    if sha256(source) != video.get("sha256"):
        raise PreviewReuseUnavailable(f"预览视频哈希已变化：{video['path']}")
    chunk, file_index = divmod(output_index, 1000)
    relative = f"videos/{camera}/chunk-{chunk:03d}/file-{file_index:03d}.mp4"
    target = output / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    digest = sha256(target)
    if digest != video.get("sha256"):
        raise PreviewReuseUnavailable(f"预览视频复制后哈希不一致：{video['path']}")
    probe = probe_video(target, length)
    if probe["frame_count"] != length:
        raise PreviewReuseUnavailable(f"复用视频帧数不一致：{relative}")
    if (probe["height"], probe["width"]) != (int(video["height"]), int(video["width"])):
        raise PreviewReuseUnavailable(f"复用视频分辨率不一致：{relative}")
    if probe["fps"] is not None and abs(probe["fps"] - FPS) > 0.01:
        raise PreviewReuseUnavailable(f"复用视频帧率不一致：{relative}")
    if probe["has_audio"]:
        raise PreviewReuseUnavailable(f"复用视频含音频轨：{relative}")
    return {"path": relative, "size": target.stat().st_size, "sha256": digest,
            "preview_path": video["path"], "preview_sha256": video["sha256"]}


def _conversion_document(capabilities: dict, cameras: list[str], strategy: str, fallback_reason=None) -> str:
    missing_cameras = ", ".join(capabilities["missing_cameras"]) or "none"
    missing_depth = ", ".join(capabilities["depth_missing"]) or "none"
    missing_optional = ", ".join(capabilities["optional_missing"]) or "none"
    reuse = ("Videos are copied from the finalized preview; no second video encode was performed. "
             "Numeric rows are reused from the preview after every selected source HDF5 frame was re-read "
             "and compared.")
    if strategy != "preview_video_reuse":
        reuse = (f"Preview video reuse was unavailable ({fallback_reason or 'unknown reason'}); "
                 "the output was re-encoded from source with the legacy path.")
    return (
        "# Matching Cable conversion\n\n"
        "Master joint position is written as action; puppet joint position is written as observation state. "
        "Each source episode is one LeRobot episode at 20 FPS. "
        f"Present RGB streams: {', '.join(cameras)}. Missing RGB streams: {missing_cameras}. "
        f"Present depth streams are validated but not written; missing depth streams: {missing_depth}. "
        f"Missing optional numeric fields: {missing_optional}. Tactile fields are not modelled. "
        f"{reuse} Hardware synchronization is not claimed.\n"
    )


def _write_dataset_reuse(preview: Path, output: Path, spec: dict, manifest: dict,
                         source_root: Path, records: dict[int, dict], raw_ids: dict[int, int],
                         candidates: list[dict], cameras: list[str], capabilities: dict,
                         shapes: dict[str, tuple[int, int, int]]) -> dict:
    import h5py
    import numpy as np
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    from matching_preview import (ACTION_FIELD, STATE_FIELD, assert_lerobot_compatibility,
                                  feature_definitions, read_numeric)

    assert_lerobot_compatibility()
    context = _preview_reuse_context(preview, manifest, records, raw_ids, candidates, cameras, capabilities, shapes)
    selected = context["selected"]
    selected_ids = {int(raw_ids[int(candidate["source_episode_index"])]) for candidate in candidates}
    verified_source = _verify_selected_source(manifest, source_root, selected_ids)
    source_by_path = {str(item["path"]): item for item in verified_source}

    temp = output.with_name(output.name + ".reuse")
    if temp.exists():
        shutil.rmtree(temp)
    temp.parent.mkdir(parents=True, exist_ok=True)
    try:
        dataset = LeRobotDataset.create(
            repo_id="local/matching-cable-reviewed", root=temp, fps=FPS,
            features=feature_definitions(shapes, capabilities["optional_present"]),
            robot_type="dual_arm_teleoperation", video_backend="pyav", vcodec="h264",
            image_writer_processes=0, image_writer_threads=0, batch_encoding_size=1,
            metadata_buffer_size=1, streaming_encoding=False,
        )
        dataset.meta.update_chunk_settings(data_files_size_in_mb=0.001, video_files_size_in_mb=0.001)
        tasks = []
        for candidate in candidates:
            task = str(candidate["annotation"]["task"]).strip()
            if not task:
                raise ValueError("审核后的任务描述不能为空")
            if task not in tasks:
                tasks.append(task)
        dataset.meta.save_episode_tasks(tasks)

        output_episodes = []
        source_files = []
        data_reuse = []
        video_reuse = []
        global_index = 0
        previous_ms = {}
        for candidate in candidates:
            source_index = int(candidate["source_episode_index"])
            output_index = int(candidate["output_episode_index"])
            source_id = int(raw_ids[source_index])
            record = records[source_index]
            length = int(record["length"])
            task = str(candidate["annotation"]["task"]).strip()
            task_index = dataset.meta.get_task_index(task)
            if task_index is None:
                raise PreviewReuseUnavailable(f"任务索引缺失：{task}")
            frames = raw_frames(source_root, source_id, length)
            table = selected[source_index]["table"]
            if len(table) != len(frames):
                raise PreviewReuseUnavailable(f"Episode {source_index} 源帧数与预览不一致")
            previous = previous_ms.get(source_id)
            for row_index, frame_path in enumerate(frames):
                relative = frame_path.relative_to(source_root).as_posix()
                source_record = source_by_path.get(relative)
                if source_record is None:
                    raise PreviewReuseUnavailable(f"源文件记录缺失：{relative}")
                with h5py.File(frame_path, "r") as handle:
                    state = read_numeric(handle, STATE_FIELD, 16)
                    action = read_numeric(handle, ACTION_FIELD, 16)
                    if "timestamps" not in handle or np.asarray(handle["timestamps"]).size != 1:
                        raise ValueError(f"{frame_path.name} 缺少单值 /timestamps")
                    source_ms = float(handle["timestamps"][0])
                    if not math.isfinite(source_ms) or source_ms < 0:
                        raise ValueError(f"{frame_path.name} 的时间戳无效")
                    if previous is not None and abs((source_ms - previous) - 50.0) > 10.0:
                        raise ValueError(
                            f"Episode {source_id} 的 {frame_path.name} 时间戳间隔为 "
                            f"{source_ms - previous:.1f} ms，超出 50±10 ms；不静默按 20 FPS 继续")
                    previous = source_ms
                    row = table.iloc[row_index]
                    preview_state = np.asarray(row["observation.state"], dtype=np.float32)
                    preview_action = np.asarray(row["action"], dtype=np.float32)
                    preview_ms = float(np.asarray(row["source.timestamp_ms"], dtype=np.float32).reshape(-1)[0])
                    if not np.array_equal(state, preview_state):
                        raise PreviewReuseUnavailable(f"Episode {source_index} 行 {row_index} state 与源不一致")
                    if not np.array_equal(action, preview_action):
                        raise PreviewReuseUnavailable(f"Episode {source_index} 行 {row_index} action 与源不一致")
                    if np.float32(source_ms) != np.float32(preview_ms):
                        raise PreviewReuseUnavailable(f"Episode {source_index} 行 {row_index} 时间戳与源不一致")
                    for field in capabilities["optional_present"]:
                        if field not in handle:
                            raise ValueError(f"可选字段 /{field} 在首帧存在，但 {frame_path.name} 缺失")
                        current = read_numeric(handle, field, OPTIONAL_NUMERIC_FIELDS[field])
                        preview_value = np.asarray(row[OPTIONAL_FEATURE_KEYS[field]], dtype=np.float32)
                        if not np.array_equal(current, preview_value):
                            raise PreviewReuseUnavailable(
                                f"Episode {source_index} 行 {row_index} 可选字段 {field} 与源不一致")
                    for field in capabilities["optional_missing"]:
                        if field in handle:
                            raise ValueError(f"可选字段 /{field} 在首帧缺失，但 {frame_path.name} 出现")
                    for camera in cameras:
                        if CAMERA_SOURCE_FIELDS[camera] not in handle:
                            raise ValueError(f"{camera} 在首帧存在，但 {frame_path.name} 缺失")
                    for camera in capabilities["missing_cameras"]:
                        if CAMERA_SOURCE_FIELDS[camera] in handle:
                            raise ValueError(f"{camera} 在首帧缺失，但 {frame_path.name} 出现")
                    for depth_field in capabilities["depth_present"]:
                        if depth_field not in handle:
                            raise ValueError(f"深度字段 /{depth_field} 在首帧存在，但 {frame_path.name} 缺失")
                    for depth_field in capabilities["depth_missing"]:
                        if depth_field in handle:
                            raise ValueError(f"深度字段 /{depth_field} 在首帧缺失，但 {frame_path.name} 出现")
                source_files.append(dict(source_record))
            previous_ms[source_id] = previous

            data_record = _episode_parquet(temp, output_index, table, dataset.hf_features, task_index, global_index)
            data_reuse.append({"output_episode_index": output_index, "source_episode_index": source_index,
                               "path": data_record["path"], "sha256": data_record["sha256"],
                               "preview_path": selected[source_index]["entry"]["data"]["path"],
                               "preview_sha256": selected[source_index]["entry"]["data"]["sha256"]})
            episode_metadata = {
                "data/chunk_index": output_index // 1000,
                "data/file_index": output_index % 1000,
            }
            for camera in cameras:
                video = _copy_video(preview, temp, output_index, camera,
                                    selected[source_index]["entry"]["videos"][camera], length)
                video_reuse.append({"output_episode_index": output_index, "source_episode_index": source_index,
                                    "source_episode_id": source_id, "camera": camera, **video})
                episode_metadata.update({
                    f"videos/{camera}/chunk_index": output_index // 1000,
                    f"videos/{camera}/file_index": output_index % 1000,
                    f"videos/{camera}/from_timestamp": 0.0,
                    f"videos/{camera}/to_timestamp": float(selected[source_index]["entry"]["videos"][camera]["to_timestamp"]),
                })
            stats_table = table.copy()
            stats_table["episode_index"] = output_index
            stats_table["index"] = np.arange(global_index, global_index + length, dtype=np.int64)
            stats_table["task_index"] = task_index
            numeric_stats = _numeric_stats(stats_table, context["info"]["features"])
            video_stats = _preview_video_stats(selected[source_index]["meta"], cameras)
            episode_stats = {**numeric_stats, **video_stats}
            dataset.meta.save_episode(output_index, length, [task], episode_stats, episode_metadata)
            output_episodes.append({"episode_index": output_index, "source_episode_index": source_index,
                                    "source_episode_id": source_id, "length": length, "task": task})
            global_index += length

        for camera in cameras:
            dataset.meta.update_video_info(camera)
        dataset.finalize()

        final_manifest = {
            "schema_version": 1, "profile": PROFILE, "format_version": "v3.0",
            "source": {key: value for key, value in manifest["source"].items() if key != "source_root"},
            "episodes": output_episodes, "total_episodes": len(output_episodes),
            "total_frames": sum(item["length"] for item in output_episodes),
            "camera_metadata": manifest["camera_metadata"], "source_files": source_files,
            "input_fingerprint": manifest.get("input_fingerprint"),
            "timestamp_warnings": manifest.get("timestamp_warnings", []),
            "limitations": manifest.get("limitations", []), "conversion_spec": spec,
            "conversion_strategy": {
                "name": "preview_video_reuse", "schema_version": REUSE_SCHEMA_VERSION,
                "source_numeric_consistency_rechecked": True,
                "source_numeric_check": "full per-frame source HDF5 vs preview parquet",
                "preview_video_hashes_verified": True, "preview_data_hashes_verified": True,
                "video_files_reused": len(video_reuse), "fallback_reason": None,
                "preview_manifest_sha256": sha256(preview / "source_manifest.json"),
            },
            "data_reuse": data_reuse, "video_reuse": video_reuse,
        }
        final_manifest["source"]["episode_mapping"] = {
            str(item["episode_index"]): item["source_episode_id"] for item in output_episodes}
        write_json(temp / "source_manifest.json", final_manifest)
        write_json(temp / "annotations.json", {"episodes": spec["episodes"]})
        (temp / "CONVERSION.md").write_text(
            _conversion_document(capabilities, cameras, "preview_video_reuse"), encoding="utf-8")
        if output.exists():
            output.rmdir()
        temp.replace(output)
        return {
            "format_version": "v3.0", "episodes": len(output_episodes),
            "frames": sum(item["length"] for item in output_episodes),
            "tasks": sorted({item["task"] for item in output_episodes}),
            "cameras": cameras, "missing_cameras": capabilities["missing_cameras"],
            "depth_source_cameras": capabilities["depth_present"],
            "missing_depth_source_cameras": capabilities["depth_missing"],
            "optional_fields": manifest["source"].get("optional_fields", {}),
            "source_fingerprint": manifest.get("input_fingerprint"),
            "strategy": "preview_video_reuse", "reuse_schema_version": REUSE_SCHEMA_VERSION,
            "source_numeric_consistency_rechecked": True,
            "preview_video_hashes_verified": True, "preview_data_hashes_verified": True,
            "video_files_reused": len(video_reuse), "fallback_reason": None,
            "preview_manifest_sha256": sha256(preview / "source_manifest.json"),
        }
    finally:
        if temp.exists():
            shutil.rmtree(temp, ignore_errors=True)


def _write_dataset_legacy(preview: Path, output: Path, spec: dict, manifest: dict,
                          source_root: Path, records: dict[int, dict], raw_ids: dict[int, int],
                          candidates: list[dict], cameras: list[str], capabilities: dict,
                          shapes: dict[str, tuple[int, int, int]], fallback_reason: str | None = None) -> dict:
    import h5py
    import numpy as np
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    from matching_preview import (ACTION_FIELD, CAMERA_SOURCE_FIELDS, ENCODER_THREADS, FPS, STATE_FIELD,
                                  VCODEC, assert_lerobot_compatibility, decode_rgb, feature_definitions,
                                  read_numeric, validate_depth)

    assert_lerobot_compatibility()
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise FileExistsError("输出目录已有内容，不能覆盖")
        output.rmdir()
    output.parent.mkdir(parents=True, exist_ok=True)
    dataset = LeRobotDataset.create(
        repo_id="local/matching-cable-reviewed", root=output, fps=FPS,
        features=feature_definitions(shapes, capabilities["optional_present"]), robot_type="dual_arm_teleoperation",
        video_backend="pyav", vcodec=VCODEC, image_writer_processes=0,
        image_writer_threads=0, batch_encoding_size=1, metadata_buffer_size=1,
        streaming_encoding=True, encoder_threads=ENCODER_THREADS,
    )
    dataset.meta.update_chunk_settings(data_files_size_in_mb=0.001, video_files_size_in_mb=0.001)
    output_episodes = []
    source_files = []
    for candidate in candidates:
        source_index = int(candidate["source_episode_index"])
        output_index = int(candidate["output_episode_index"])
        source_id = int(raw_ids[source_index])
        length = int(records[source_index]["length"])
        frames = raw_frames(source_root, source_id, length)
        task = str(candidate["annotation"]["task"]).strip()
        if not task:
            raise ValueError("审核后的任务描述不能为空")
        for frame_path in frames:
            relative = frame_path.relative_to(source_root).as_posix()
            digest = sha256(frame_path)
            source_files.append({"path": relative, "size": frame_path.stat().st_size,
                                 "mtime_ns": frame_path.stat().st_mtime_ns, "episode_id": source_id,
                                 "sha256": digest})
            with h5py.File(frame_path, "r") as handle:
                state = read_numeric(handle, STATE_FIELD, 16)
                action = read_numeric(handle, ACTION_FIELD, 16)
                if "timestamps" not in handle:
                    raise ValueError(f"{frame_path.name} 缺少 /timestamps")
                source_ms = float(handle["timestamps"][0])
                if not np.isfinite(source_ms) or source_ms < 0:
                    raise ValueError(f"{frame_path.name} 的时间戳无效")
                frame = {
                    "task": task,
                    "observation.state": state,
                    "action": action,
                    "source.timestamp_ms": np.asarray([source_ms], dtype=np.float32),
                }
                for field in capabilities["optional_present"]:
                    if field not in handle:
                        raise ValueError(f"可选字段 /{field} 在首帧存在，但 {frame_path.name} 缺失")
                    frame[OPTIONAL_FEATURE_KEYS[field]] = read_numeric(
                        handle, field, OPTIONAL_NUMERIC_FIELDS[field])
                for field in capabilities["optional_missing"]:
                    if field in handle:
                        raise ValueError(f"可选字段 /{field} 在首帧缺失，但 {frame_path.name} 出现")
                for camera in cameras:
                    source_field = CAMERA_SOURCE_FIELDS[camera]
                    if source_field not in handle:
                        raise ValueError(f"{camera} 在首帧存在，但 {frame_path.name} 缺失")
                    frame[camera] = decode_rgb(handle[source_field][0], source_field, shapes[camera])
                for camera in capabilities["missing_cameras"]:
                    if CAMERA_SOURCE_FIELDS[camera] in handle:
                        raise ValueError(f"{camera} 在首帧缺失，但 {frame_path.name} 出现")
                for depth_field in capabilities["depth_present"]:
                    if depth_field not in handle:
                        raise ValueError(f"深度字段 /{depth_field} 在首帧存在，但 {frame_path.name} 缺失")
                    validate_depth(handle[depth_field][0], depth_field.split("/")[-1])
                for depth_field in capabilities["depth_missing"]:
                    if depth_field in handle:
                        raise ValueError(f"深度字段 /{depth_field} 在首帧缺失，但 {frame_path.name} 出现")
                dataset.add_frame(frame)
        dataset.save_episode(parallel_encoding=False)
        output_episodes.append({"episode_index": output_index, "source_episode_index": source_index,
                                "source_episode_id": source_id, "length": length, "task": task})
    dataset.finalize()
    final_manifest = {
        "schema_version": 1, "profile": PROFILE, "format_version": "v3.0",
        "source": {key: value for key, value in manifest["source"].items() if key != "source_root"},
        "episodes": output_episodes, "total_episodes": len(output_episodes),
        "total_frames": sum(item["length"] for item in output_episodes),
        "camera_metadata": manifest["camera_metadata"], "source_files": source_files,
        "input_fingerprint": manifest.get("input_fingerprint"),
        "timestamp_warnings": manifest.get("timestamp_warnings", []),
        "limitations": manifest.get("limitations", []),
        "conversion_spec": spec,
        "conversion_strategy": {
            "name": "legacy_reencode", "schema_version": 1,
            "source_numeric_consistency_rechecked": True,
            "source_numeric_check": "full per-frame source HDF5 values written to output",
            "preview_video_hashes_verified": False, "preview_data_hashes_verified": False,
            "video_files_reused": 0, "fallback_reason": fallback_reason,
        },
    }
    final_manifest["source"]["episode_mapping"] = {
        str(item["episode_index"]): item["source_episode_id"] for item in output_episodes}
    write_json(output / "source_manifest.json", final_manifest)
    write_json(output / "annotations.json", {"episodes": spec["episodes"]})
    (output / "CONVERSION.md").write_text(
        _conversion_document(capabilities, cameras, "legacy_reencode", fallback_reason), encoding="utf-8")
    return {"format_version": "v3.0", "episodes": len(output_episodes),
            "frames": sum(item["length"] for item in output_episodes),
            "tasks": sorted({item["task"] for item in output_episodes}),
            "cameras": cameras, "missing_cameras": capabilities["missing_cameras"],
            "depth_source_cameras": capabilities["depth_present"],
            "missing_depth_source_cameras": capabilities["depth_missing"],
            "optional_fields": manifest["source"].get("optional_fields", {}),
            "source_fingerprint": manifest.get("input_fingerprint"),
            "strategy": "legacy_reencode", "reuse_schema_version": REUSE_SCHEMA_VERSION,
            "source_numeric_consistency_rechecked": True,
            "preview_video_hashes_verified": False, "preview_data_hashes_verified": False,
            "video_files_reused": 0, "fallback_reason": fallback_reason}


def write_dataset(preview: Path, output: Path, spec: dict, manifest: dict, allow_reuse: bool = True) -> dict:
    from matching_preview import inspect_frame_capabilities, inspect_shapes

    source_root = Path(manifest["source"]["source_root"])
    records = {int(item["episode_index"]): item for item in manifest["episodes"]}
    raw_ids = {int(key): int(value) for key, value in manifest["source"]["episode_mapping"].items()}
    candidates = spec["episodes"]
    cameras = manifest["source"]["cameras"]
    optional_present = [field for field, status in manifest["source"].get("optional_fields", {}).items()
                        if status == "provided"]
    first_source = int(candidates[0]["source_episode_index"])
    first_frames = raw_frames(source_root, raw_ids[first_source], int(records[first_source]["length"]))
    capabilities = inspect_frame_capabilities(first_frames[0])
    if capabilities["cameras"] != cameras:
        raise ValueError("源相机与预览清单不一致；源数据已变化")
    if capabilities["optional_present"] != optional_present:
        raise ValueError("源可选字段与预览清单不一致；源数据已变化")
    if capabilities["depth_present"] != manifest["source"].get("depth_source_cameras", []):
        raise ValueError("源深度字段与预览清单不一致；源数据已变化")
    shapes = inspect_shapes(first_frames[0], cameras)
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise FileExistsError("输出目录已有内容，不能覆盖")
        output.rmdir()
    output.parent.mkdir(parents=True, exist_ok=True)

    fallback_reason = None
    if allow_reuse:
        try:
            return _write_dataset_reuse(preview, output, spec, manifest, source_root, records, raw_ids,
                                        candidates, cameras, capabilities, shapes)
        except PreviewReuseUnavailable as exc:
            fallback_reason = str(exc)
    return _write_dataset_legacy(preview, output, spec, manifest, source_root, records, raw_ids,
                                 candidates, cameras, capabilities, shapes, fallback_reason)


def verify_dataset(output: Path, artifacts: Path, *, archive_only: bool = False) -> dict:
    import numpy as np
    import pandas as pd
    import torch
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    from torch.utils.data import DataLoader

    manifest = source_manifest(output)
    episodes = manifest["episodes"]
    cameras = manifest["source"]["cameras"]
    expected_rows = sum(int(item["length"]) for item in episodes)
    tables = pd.concat([pd.read_parquet(path) for path in sorted((output / "data").rglob("*.parquet"))],
                       ignore_index=True)
    tables["index"] = tables["index"].astype(int)
    tables = tables.set_index("index", drop=False)
    if len(tables) != expected_rows or tables["episode_index"].nunique() != len(episodes):
        raise ValueError("输出表格数量与源清单不一致")
    numeric = ["observation.state", "action", "source.timestamp_ms"]
    numeric += [OPTIONAL_FEATURE_KEYS[field] for field, status in manifest["source"].get("optional_fields", {}).items()
                if status == "provided"]
    for field in numeric:
        if field not in tables:
            raise ValueError(f"输出缺少字段 {field}")
        if not np.isfinite(np.stack(tables[field].to_numpy())).all():
            raise ValueError(f"输出字段 {field} 包含非有限值")
    dataset = LeRobotDataset(repo_id="local/matching-cable-reviewed", root=output, video_backend="pyav")
    if len(dataset) != expected_rows or dataset.meta.total_episodes != len(episodes):
        raise ValueError("官方加载器的任务数或帧数不一致")
    samples = []
    for episode in episodes:
        episode_index = int(episode["episode_index"])
        rows = tables[tables["episode_index"] == episode_index].reset_index(drop=True)
        for row_index in (0, len(rows) // 2, len(rows) - 1):
            row = rows.iloc[row_index]
            item = dataset[int(row["index"])]
            expected_state = np.asarray(row["observation.state"], dtype=np.float32)
            actual_state = item["observation.state"].numpy()
            expected_action = np.asarray(row["action"], dtype=np.float32)
            actual_action = item["action"].numpy()
            if not np.array_equal(actual_state, expected_state) or not np.array_equal(actual_action, expected_action):
                raise ValueError(f"Episode {episode_index} row {row_index} 官方数值不一致")
            if int(item["episode_index"]) != episode_index or int(item["frame_index"]) != row_index:
                raise ValueError(f"Episode {episode_index} row {row_index} 官方索引不一致")
            for camera in cameras:
                image = item[camera]
                height, width = [int(value) for value in manifest["camera_metadata"][camera]["resolution"][:2]]
                if tuple(image.shape) != (3, height, width) or not torch.isfinite(image).all():
                    raise ValueError(f"Episode {episode_index} {camera} 官方图像形状无效")
                pixels = np.rint(image.permute(1, 2, 0).numpy() * 255).astype(np.uint8)
                samples.append({"episode": episode_index, "row": row_index, "camera": camera,
                                "global_index": int(row["index"]), "timestamp": float(item["timestamp"]),
                                "decoded_image_sha256": hashlib.sha256(pixels.tobytes()).hexdigest()})
    batch = next(iter(DataLoader(dataset, batch_size=min(2, len(dataset)), num_workers=0, shuffle=False)))
    if tuple(batch["observation.state"].shape)[1:] != (16,) or tuple(batch["action"].shape)[1:] != (16,):
        raise ValueError("官方 DataLoader 状态或动作维度不正确")
    for camera in cameras:
        height, width = [int(value) for value in manifest["camera_metadata"][camera]["resolution"][:2]]
        if tuple(batch[camera].shape)[1:] != (3, height, width):
            raise ValueError(f"官方 DataLoader {camera} 图像维度不正确")
    strategy = manifest.get("conversion_strategy") or {}
    if strategy:
        if strategy.get("source_numeric_consistency_rechecked") is not True:
            raise ValueError("输出清单缺少源数值一致性证据")
        if strategy.get("name") == "preview_video_reuse":
            if int(strategy.get("video_files_reused", -1)) != len(episodes) * len(cameras):
                raise ValueError("输出清单的视频复用数量不完整")
        elif strategy.get("name") != "legacy_reencode":
            raise ValueError(f"未知转换策略：{strategy.get('name')}")
    snapshot_id = (manifest.get("conversion_spec") or {}).get("snapshot_id")
    return {"format_version": "v3.0", "episode_count": len(episodes), "row_count": expected_rows,
            "checked_numeric_frames": 0 if archive_only else expected_rows,
            "output_numeric_frames_checked": expected_rows,
            "output_loader_consistency_checked": True,
            "source_numeric_consistency_rechecked": False,
            "verification_scope": "输出 parquet 与官方加载器一致；源数值一致性由 format_write 阶段记录，本阶段未重读源 HDF5",
            "cameras": cameras, "missing_cameras": manifest["source"].get("missing_cameras", []),
            "missing_depth_source_cameras": manifest["source"].get("missing_depth_source_cameras", []),
            "optional_fields": manifest["source"].get("optional_fields", {}),
            "source_fingerprint": manifest.get("input_fingerprint"), "snapshot_id": snapshot_id,
            "sample_count": 3 * len(episodes) * len(cameras), "samples": samples,
            "batch_size": min(2, len(dataset)), "offline": True, "video_backend": "pyav",
            "camera_synchronization_claim": "未声明硬件同步", "file_count": 0,
            "conversion_strategy": strategy}


def validate_preview(source: Path, spec: dict | None) -> tuple[dict, dict, dict[int, int]]:
    import numpy as np
    import pandas as pd

    manifest = source_manifest(source)
    if spec is None:
        raise ValueError("转换需要冻结候选清单")
    rows = candidate_rows(spec, manifest)
    info = json.loads((source / "meta/info.json").read_text(encoding="utf-8"))
    cameras = [key for key, feature in info["features"].items() if feature.get("dtype") == "video"]
    if cameras != manifest["source"]["cameras"] or float(info["fps"]) != float(FPS):
        raise ValueError("预览元数据的相机或帧率与清单不一致")
    tables = pd.concat([pd.read_parquet(path) for path in sorted((source / "data").rglob("*.parquet"))],
                       ignore_index=True)
    if not np.isfinite(np.stack(tables["observation.state"].to_numpy())).all():
        raise ValueError("预览 observation.state 包含非有限值")
    if not np.isfinite(np.stack(tables["action"].to_numpy())).all():
        raise ValueError("预览 action 包含非有限值")
    for field, status in manifest["source"].get("optional_fields", {}).items():
        if status == "provided" and OPTIONAL_FEATURE_KEYS[field] not in tables:
            raise ValueError(f"预览声明提供了 /{field}，但输出缺少 {OPTIONAL_FEATURE_KEYS[field]}")
    return manifest, info, rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True,
                        choices=("file_validation", "matching_validation", "field_mapping", "format_write",
                                 "official_verify", "archive_verify"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--no-reuse", action="store_true", help="benchmark/fallback: force legacy re-encoding")
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    try:
        if args.stage == "archive_verify":
            details = verify_dataset(args.output.resolve(), artifacts, archive_only=True)
        else:
            if args.input is None:
                raise ValueError("该阶段需要 --input")
            spec = load_spec(args.spec) if args.spec else None
            manifest, info, rows = validate_preview(args.input.resolve(), spec)
            cameras = manifest["source"]["cameras"]
            optional_fields = manifest["source"].get("optional_fields", {})
            if args.stage == "file_validation":
                details = {"episode_count": len(rows), "row_count": sum(rows.values()),
                           "source_fingerprint": manifest.get("input_fingerprint"),
                           "cameras": cameras, "fps": FPS,
                           "missing_cameras": manifest["source"].get("missing_cameras", []),
                           "missing_depth_source_cameras": manifest["source"].get("missing_depth_source_cameras", []),
                           "optional_fields": optional_fields,
                           "preview_video_layout": (manifest.get("reuse") or {}).get("layout"),
                           "reuse_schema_version": (manifest.get("reuse") or {}).get("schema_version")}
            elif args.stage == "matching_validation":
                details = {"episode_count": len(rows), "row_count": sum(rows.values()),
                           "state_field": manifest["source"]["state_field"],
                           "action_field": manifest["source"]["action_field"],
                           "cameras": cameras,
                           "missing_cameras": manifest["source"].get("missing_cameras", []),
                           "depth_source_cameras": manifest["source"]["depth_source_cameras"],
                           "missing_depth_source_cameras": manifest["source"].get("missing_depth_source_cameras", []),
                           "optional_fields": optional_fields,
                           "tactile": manifest["source"].get("tactile"),
                           "timestamp_rule": "source milliseconds, 50 ± 10 ms/frame -> 20 FPS; out-of-tolerance frames fail"}
            elif args.stage == "field_mapping":
                details = {"observation.state": {"source": "puppet/joint_position", "shape": [16]},
                           "action": {"source": "master/joint_position", "shape": [16]},
                           "cameras": cameras, "fps": FPS,
                           "optional_fields": optional_fields}
            elif args.stage == "format_write":
                details = write_dataset(args.input.resolve(), args.output.resolve(), spec, manifest,
                                        allow_reuse=not args.no_reuse)
            else:
                details = verify_dataset(args.output.resolve(), artifacts)
        write_json(artifacts / "steps" / f"{args.stage}.json",
                   {"stage": args.stage, "status": "passed", "details": details})
        return 0
    except Exception as exc:
        details = {"stage": args.stage, "status": "failed", "message_zh": str(exc),
                   "exception_type": type(exc).__name__, "exception_message": str(exc),
                   "traceback": traceback.format_exc()}
        write_json(artifacts / "diagnostic.json", details)
        write_json(artifacts / "steps" / f"{args.stage}.json", {"stage": args.stage, "status": "failed",
                                                                 "diagnostic": details})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
