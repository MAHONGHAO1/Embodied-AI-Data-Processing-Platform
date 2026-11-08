"""Convert the pinned ALOHA Coffee v3.0 sample into a reviewed v3.0 subset.

The input is read-only. Output videos are re-encoded through the official
LeRobot writer so that only approved episodes and camera streams are delivered.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import sys
import traceback

import av
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robodata.aloha import ALOHA_CAMERAS, ALOHA_EPISODES, ALOHA_REVISION  # noqa: E402
from robodata.dataset import load_episode, load_metadata  # noqa: E402
from robodata.source import file_sha256  # noqa: E402

FORMAT = "v3.0"
PROFILE = "aloha_static_coffee_reviewed_v1"
AUTO_FIELDS = {"index", "episode_index", "frame_index", "timestamp", "task_index"}
VIDEO_FIELDS = {"dtype": "video"}


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _hash(path: Path) -> str:
    return file_sha256(path)


def _files(root: Path) -> list[dict]:
    return [{"path": path.relative_to(root).as_posix(), "size": path.stat().st_size, "sha256": _hash(path)}
            for path in sorted(root.rglob("*")) if path.is_file() and path.name != "source_manifest.json"]


def _spec(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    episodes = value.get("episodes")
    if not isinstance(episodes, list) or not episodes:
        raise ValueError("转换候选集为空")
    for key in ("batch_id", "snapshot_id", "input_fingerprint"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ValueError(f"转换候选集缺少 {key}")
    seen = set()
    for position, episode in enumerate(episodes):
        source_index = episode.get("source_episode_index")
        if type(source_index) is not int or source_index not in ALOHA_EPISODES or source_index in seen:
            raise ValueError("ALOHA 候选任务只能是 Episode 0–4 且不能重复")
        if episode.get("output_episode_index") != position:
            raise ValueError("输出任务编号必须按候选顺序从 0 连续排列")
        if type(episode.get("row_count")) is not int or episode["row_count"] < 1:
            raise ValueError("候选任务帧数无效")
        annotation = episode.get("annotation")
        if not isinstance(annotation, dict) or not isinstance(annotation.get("task"), str) or not annotation["task"].strip():
            raise ValueError("候选任务缺少已审核的任务描述")
        if not isinstance(episode.get("quality_run_id"), str) or not episode["quality_run_id"].strip():
            raise ValueError("候选任务缺少基础质检运行编号")
        seen.add(source_index)
    unsigned = {key: item for key, item in value.items() if key != "snapshot_id"}
    digest = hashlib.sha256(json.dumps(unsigned, sort_keys=True, ensure_ascii=False,
                                       separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    if digest != value["snapshot_id"]:
        raise ValueError("冻结候选清单摘要不一致")
    return value


def validate_source(source: Path, spec: dict | None = None) -> tuple[dict, list[dict]]:
    metadata = load_metadata(source)
    manifest_path = source / "source_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError("固定 ALOHA 来源清单缺失")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("source", {}).get("revision") != ALOHA_REVISION:
        raise ValueError("ALOHA 输入 revision 不符合固定版本")
    if manifest.get("source", {}).get("repo_id") != "lerobot/aloha_static_coffee":
        raise ValueError("ALOHA 输入仓库与固定来源不一致")
    actual_records = []
    for record in manifest.get("files", []):
        path = source / record["path"]
        if not path.is_file() or path.stat().st_size != record.get("size") or _hash(path) != record.get("sha256"):
            raise ValueError(f"固定来源文件缺失或哈希变化：{record.get('path')}")
        actual_records.append({"path": record["path"], "size": path.stat().st_size, "sha256": _hash(path)})
    if {item["path"] for item in actual_records} != {item["path"] for item in manifest.get("files", [])}:
        raise ValueError("固定来源文件清单不完整")
    indices = [index for index in ALOHA_EPISODES if index in metadata["episodes"]]
    if tuple(indices) != ALOHA_EPISODES:
        raise ValueError("ALOHA Episode 0–4 元数据不完整")
    camera_features = [key for key, feature in metadata["info"]["features"].items()
                       if isinstance(feature, dict) and feature.get("dtype") == "video"]
    if set(camera_features) != set(ALOHA_CAMERAS):
        raise ValueError("ALOHA 相机字段与固定四路配置不一致")
    candidates = []
    selected = spec["episodes"] if spec else [
        {"source_episode_index": index, "output_episode_index": pos,
         "row_count": int(metadata["episodes"][index]["length"]),
         "annotation": {"task": metadata["tasks"][int(metadata["episodes"][index]["tasks"][0])]["task"],
                        "outcome": "success", "tags": []}, "quality_run_id": "source-validation"}
        for pos, index in enumerate(ALOHA_EPISODES)
    ]
    for entry in selected:
        index = entry["source_episode_index"]
        episode = load_episode(source, index)
        if len(episode.table) != entry["row_count"]:
            raise ValueError(f"Episode {index} 帧数与冻结候选清单不一致")
        for field in ("observation.state", "action"):
            if field not in episode.table:
                raise ValueError(f"Episode {index} 缺少数值字段 {field}")
            array = np.stack(episode.table[field].to_numpy())
            if not np.isfinite(array).all():
                raise ValueError(f"Episode {index} 字段 {field} 含非有限值")
        candidates.append({**entry, "episode": episode})
    return {"metadata": metadata, "source_manifest": manifest,
            "source_manifest_sha256": _hash(manifest_path),
            "source_records": actual_records, "camera_features": camera_features,
            "fps": float(metadata["info"]["fps"])}, candidates


def _decode_camera_frames(path: Path, start: float, end: float,
                          timestamps: np.ndarray, fps: float, camera: str) -> list[np.ndarray]:
    """Decode one episode segment once and map every row to the nearest PTS."""
    images: list[np.ndarray] = []
    pts: list[float] = []
    with av.open(str(path)) as container:
        if not container.streams.video:
            raise ValueError(f"相机 {camera} 文件中没有视频轨道")
        stream = container.streams.video[0]
        if stream.time_base is None:
            raise ValueError(f"相机 {camera} 视频缺少时间基")
        container.seek(max(0, int(start / float(stream.time_base))), stream=stream,
                       backward=True, any_frame=False)
        for frame in container.decode(stream):
            if frame.pts is None or frame.time_base is None:
                raise ValueError(f"相机 {camera} 视频帧缺少 PTS")
            value = float(frame.pts * frame.time_base)
            if value < start - 1e-5:
                continue
            if value >= end - 1e-6:
                break
            pts.append(value)
            images.append(frame.to_ndarray(format="rgb24"))
    targets = timestamps.astype(float) + start
    if len(pts) != len(targets):
        raise ValueError(f"相机 {camera} 片段帧数 {len(pts)} 与表格帧数 {len(targets)} 不一致")
    tolerance = max(0.001, 0.5 / fps)
    errors = np.abs(np.asarray(pts) - targets)
    if not np.isfinite(errors).all() or np.any(errors > tolerance):
        row = int(np.argmax(errors))
        raise ValueError(f"相机 {camera} 第 {row} 行时间映射偏差 {errors[row]:.6f}s 超过 {tolerance:.6f}s")
    return images


def _normalize_numeric_feature(value, feature: dict) -> np.ndarray:
    """Match scalar/vector parquet values to the declared LeRobot array shape."""
    array = np.asarray(value, dtype=feature["dtype"])
    shape = tuple(feature.get("shape", ()))
    if array.shape != shape:
        expected_size = math.prod(shape) if shape else 1
        if array.size != expected_size:
            raise ValueError(
                f"特征 {feature.get('name', '<unknown>')} 的源元素数 {array.size} "
                f"与声明形状 {shape} 不匹配"
            )
        array = array.reshape(shape)
    return array


def write_dataset(source: Path, output: Path, artifacts: Path, spec: dict,
                  provenance: dict, candidates: list[dict]) -> dict:
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    info = provenance["metadata"]["info"]
    cameras = provenance["camera_features"]
    features = {}
    source_fields = [key for key in info["features"] if key not in AUTO_FIELDS]
    for key in source_fields:
        feature = dict(info["features"][key])
        if "shape" in feature:
            feature["shape"] = tuple(feature["shape"])
        features[key] = feature
    if set(cameras) - set(features):
        raise ValueError("输出特征表缺少固定 ALOHA 相机字段")
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise FileExistsError("输出目录已有内容，不能覆盖既有数据")
        output.rmdir()
    output.parent.mkdir(parents=True, exist_ok=True)
    robot_type = str(info.get("robot_type") or provenance["source_manifest"]["source"].get("robot") or "aloha")
    dataset = LeRobotDataset.create(
        repo_id="local/aloha-static-coffee-reviewed", root=output,
        fps=int(provenance["fps"]), features=features, robot_type=robot_type,
        video_backend="pyav", vcodec="h264", image_writer_processes=0,
        image_writer_threads=0, batch_encoding_size=1, streaming_encoding=True,
    )
    task_by_index = provenance["metadata"]["tasks"]
    selection = []
    try:
        for candidate in candidates:
            episode = candidate["episode"]
            rows = episode.table.reset_index(drop=True)
            camera_frames = {}
            for camera in cameras:
                segment = (episode.camera_segments or {}).get(camera)
                if not segment:
                    raise ValueError(f"Episode {episode.episode_index} 缺少相机 {camera} 的时间段")
                camera_frames[camera] = _decode_camera_frames(
                    episode.camera_paths[camera], float(segment["from_timestamp"]),
                    float(segment["to_timestamp"]), rows["timestamp"].to_numpy(),
                    provenance["fps"], camera)
            task_text = candidate["annotation"]["task"].strip()
            for row_index, (_, row) in enumerate(rows.iterrows()):
                frame = {"task": task_text}
                for field in source_fields:
                    if field in cameras:
                        frame[field] = camera_frames[field][row_index]
                    elif field in row:
                        value = row[field]
                        dtype = info["features"][field].get("dtype")
                        if dtype and dtype not in {"image", "video", "string"}:
                            value = _normalize_numeric_feature(value, info["features"][field])
                        frame[field] = value
                    else:
                        raise ValueError(f"Episode {episode.episode_index} 缺少保留字段 {field}")
                dataset.add_frame(frame)
            dataset.save_episode(parallel_encoding=False)
            selection.append({key: candidate[key] for key in (
                "source_episode_index", "output_episode_index", "row_count", "annotation", "quality_run_id")})
    finally:
        dataset.finalize()

    annotations = {"schema_version": 1, "source_repo": "lerobot/aloha_static_coffee",
                   "source_revision": ALOHA_REVISION, "mapping_policy": "保留来源状态、动作及全部相机特征；已审核任务描述写入 LeRobot task。",
                   "conversion_spec": spec, "episodes": selection}
    _write(output / "annotations.json", annotations)
    (output / "CONVERSION.md").write_text(
        "# ALOHA Coffee 子集转换说明\n\n"
        f"来源固定为 `lerobot/aloha_static_coffee@{ALOHA_REVISION}`，仅输出人工审核后保留的 Episode 0–4 子集。\n"
        f"保留 {len(cameras)} 路来源视频特征，帧率 {provenance['fps']:g} FPS。视频经 LeRobot 官方 writer 重新编码为 H.264；不声明硬件同步。\n"
        "状态与动作按来源特征定义映射，不推断物理单位或控制含义。\n", encoding="utf-8")
    manifest = {
        "schema_version": 1, "format_version": FORMAT, "profile": PROFILE,
        "source": provenance["source_manifest"]["source"],
        "source_manifest_sha256": provenance["source_manifest_sha256"],
        "source_files": provenance["source_records"], "camera_features": cameras,
        "camera_metadata": {camera: {"fps": provenance["fps"],
            "resolution": list(info["features"][camera].get("shape", [])),
            "source_path": info["video_path"], "encoding": "H.264 output re-encode"} for camera in cameras},
        "fps": provenance["fps"], "episodes": [{**entry,
            "source_episode_index": entry["source_episode_index"],
            "episode_index": entry["output_episode_index"], "length": entry["row_count"]}
            for entry in selection],
        "total_episodes": len(selection), "total_frames": sum(entry["row_count"] for entry in selection),
        "conversion_spec": spec,
        "time_policy": "保留 LeRobot 输出的 episode-relative 时间；逐相机文件内时间对应检查，不声明硬件同步。",
        "writer": {name: importlib.metadata.version(name) for name in
                   ("lerobot", "torch", "torchvision", "av", "numpy", "datasets")},
        "files": _files(output),
    }
    _write(output / "source_manifest.json", manifest)
    return {"output_path": str(output), "episode_count": manifest["total_episodes"],
            "row_count": manifest["total_frames"], "camera_features": cameras,
            "source_manifest_sha256": provenance["source_manifest_sha256"],
            "source_numeric_frames_checked": manifest["total_frames"]}


def verify_dataset(output: Path, artifacts: Path, *, archive_only: bool = False,
                   spec: dict | None = None) -> dict:
    import torch
    from torch.utils.data import DataLoader
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    if os.environ.get("HF_HUB_OFFLINE") != "1" or os.environ.get("HF_DATASETS_OFFLINE") != "1":
        raise ValueError("官方加载验收必须在离线模式运行")
    manifest_path = output / "source_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("profile") != PROFILE or manifest.get("format_version") != FORMAT:
        raise ValueError("转换产物 profile 或格式版本不匹配")
    if spec is not None and manifest.get("conversion_spec") != spec:
        raise ValueError("转换产物中的业务快照与本次请求不一致")
    expected_files = manifest.get("files")
    actual_files = _files(output)
    if not expected_files or expected_files != actual_files:
        expected_map = {entry.get("path"): entry for entry in expected_files or []}
        actual_map = {entry.get("path"): entry for entry in actual_files}
        changed = sorted(path for path in set(expected_map) | set(actual_map)
                         if expected_map.get(path) != actual_map.get(path))
        raise ValueError(f"输出文件清单或哈希不匹配：{changed[:5]}")
    cameras = manifest.get("camera_features", [])
    if cameras != list(ALOHA_CAMERAS):
        raise ValueError("输出未保留完整的固定四路相机特征")
    dataset = LeRobotDataset(repo_id="local/aloha-static-coffee-reviewed", root=output,
                             video_backend="pyav")
    total_frames = int(manifest["total_frames"])
    episodes = manifest["episodes"]
    if (len(dataset) != total_frames or dataset.meta.total_episodes != len(episodes)
            or dataset.meta.info.get("codebase_version") != FORMAT):
        raise ValueError("官方 LeRobot loader 的格式、任务数或帧数与清单不匹配")
    feature_map = dataset.meta.info.get("features", {})
    if [key for key, feature in feature_map.items() if feature.get("dtype") == "video"] != cameras:
        raise ValueError("官方加载到的相机特征与来源顺序不一致")
    table_paths = sorted((output / "data").rglob("*.parquet"))
    table = pd.concat([pd.read_parquet(path) for path in table_paths], ignore_index=True).sort_values("index").reset_index(drop=True)
    if len(table) != total_frames:
        raise ValueError("输出 Parquet 行数与来源清单不一致")
    expected_position = 0
    samples = []
    decoded_images = []
    for episode in episodes:
        index = int(episode["episode_index"])
        length = int(episode["length"])
        rows = table[table["episode_index"] == index].sort_values("frame_index")
        if len(rows) != length or not np.array_equal(rows["frame_index"].to_numpy(), np.arange(length)):
            raise ValueError(f"输出 Episode {index} 的数量或帧索引不一致")
        for field in ("observation.state", "action"):
            values = np.stack(rows[field].to_numpy()).astype(np.float32)
            if not np.isfinite(values).all():
                raise ValueError(f"输出 Episode {index} 字段 {field} 含非有限值")
        for row in sorted({0, length // 2, length - 1}):
            position = expected_position + row
            item = dataset[position]
            if int(item["episode_index"]) != index or int(item["frame_index"]) != row or int(item["index"]) != position:
                raise ValueError(f"官方加载样本身份与索引不匹配：Episode {index} row {row}")
            for camera in cameras:
                image = item[camera]
                expected_shape = tuple(int(value) for value in feature_map[camera]["shape"])
                expected_tensor = (expected_shape[2], expected_shape[0], expected_shape[1])
                if (image.dtype != torch.float32 or tuple(image.shape) != expected_tensor
                        or not torch.isfinite(image).all() or float(image.min()) < 0 or float(image.max()) > 1):
                    raise ValueError(f"官方加载相机 {camera} 的图像类型、尺寸或数值范围不正确")
                pixels = np.rint(image.permute(1, 2, 0).numpy() * 255).astype(np.uint8)
                samples.append({"episode": index, "row": row, "camera": camera,
                                "global_index": position,
                                "decoded_image_sha256": hashlib.sha256(pixels.tobytes()).hexdigest()})
                decoded_images.append(pixels)
        expected_position += length
    batch = next(iter(DataLoader(dataset, batch_size=min(2, len(dataset)), num_workers=0, shuffle=False)))
    camera_batch_shapes = {camera: list(batch[camera].shape) for camera in cameras}
    if any(shape[0] != min(2, len(dataset)) for shape in camera_batch_shapes.values()):
        raise ValueError("官方 DataLoader 未能批量读取所有相机特征")
    if not archive_only:
        np.savez_compressed(artifacts / "decoded_samples.npz", images=np.stack(decoded_images),
                            episode_indices=np.array([sample["episode"] for sample in samples]),
                            frame_indices=np.array([sample["row"] for sample in samples]),
                            global_indices=np.array([sample["global_index"] for sample in samples]))
    return {"format_version": FORMAT, "profile": PROFILE,
            "source_repo": manifest["source"]["repo_id"], "source_revision": manifest["source"]["revision"],
            "camera_features": cameras, "camera_metadata": manifest["camera_metadata"],
            "fps": manifest["fps"], "output_numeric_frames_checked": len(table),
            "checked_numeric_frames": 0 if archive_only else len(table),
            "episode_count": len(episodes), "row_count": len(table),
            "sample_count": len({(item["episode"], item["row"]) for item in samples}),
            "video_sample_count": len(samples), "samples": samples,
            "batch_size": min(2, len(dataset)), "batch_camera_shapes": camera_batch_shapes,
            "offline": True, "video_backend": "pyav", "source_numeric_consistency_rechecked": False,
            "validation_scope": "输出全量数值、四路视频抽样与官方离线加载；来源数值在写入时逐字段核对" if not archive_only
                                else "独立解包后的文件哈希、数值、四路视频抽样与官方离线加载；来源数值绑定到转换清单",
            "snapshot_id": manifest.get("conversion_spec", {}).get("snapshot_id"),
            "file_count": len(actual_files), "camera_synchronization_claim": "未声明硬件同步"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True,
                        choices=("file_validation", "field_mapping", "format_write", "official_verify", "archive_verify"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--spec", type=Path)
    args = parser.parse_args()
    output, artifacts = args.output.resolve(), args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    try:
        if args.stage == "archive_verify":
            details = verify_dataset(output, artifacts, archive_only=True)
        else:
            if args.input is None:
                raise ValueError("该转换阶段必须提供 --input")
            spec = _spec(args.spec) if args.spec else None
            provenance, candidates = validate_source(args.input.resolve(), spec)
            if args.stage == "file_validation":
                details = {"repo_id": "lerobot/aloha_static_coffee", "revision": ALOHA_REVISION,
                           "episode_count": len(candidates),
                           "row_count": sum(item["row_count"] for item in candidates),
                           "source_manifest_sha256": provenance["source_manifest_sha256"],
                           "source_files": provenance["source_records"],
                           "camera_features": provenance["camera_features"], "fps": provenance["fps"]}
            elif args.stage == "field_mapping":
                info = provenance["metadata"]["info"]
                details = {"camera_features": provenance["camera_features"],
                           "camera_metadata": {camera: {"fps": provenance["fps"],
                               "resolution": list(info["features"][camera].get("shape", []))}
                               for camera in provenance["camera_features"]},
                           "preserved_features": list(info["features"]),
                           "fps": provenance["fps"], "episodes": [
                               {"source_episode_index": item["source_episode_index"],
                                "output_episode_index": item["output_episode_index"],
                                "row_count": item["row_count"]} for item in candidates],
                           "time_mapping": "按各相机 episode 时间段选择 PTS；不声明硬件同步"}
            elif args.stage == "format_write":
                if spec is None:
                    raise ValueError("格式写入必须提供冻结候选清单")
                details = write_dataset(args.input.resolve(), output, artifacts, spec, provenance, candidates)
            else:
                details = verify_dataset(output, artifacts, spec=spec)
        _write(artifacts / "steps" / f"{args.stage}.json",
               {"stage": args.stage, "status": "passed", "details": details})
        return 0
    except Exception as exc:
        details = {"stage": args.stage, "status": "failed", "message_zh": str(exc),
                   "exception_type": type(exc).__name__, "exception_message": str(exc),
                   "traceback": traceback.format_exc()}
        _write(artifacts / "diagnostic.json", details)
        _write(artifacts / "steps" / f"{args.stage}.json",
               {"stage": args.stage, "status": "failed", "diagnostic": details})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
