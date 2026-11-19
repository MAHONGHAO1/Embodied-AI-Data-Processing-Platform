"""Finalize a Matching Cable preview into an annotated LeRobot v3 delivery."""

from __future__ import annotations

import argparse
import hashlib
import json
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
    for item in spec["episodes"]:
        source_index = int(item["source_episode_index"])
        output_index = int(item["output_episode_index"])
        if source_index not in rows:
            raise ValueError(f"候选 Episode {source_index} 不在预览产物中")
        if int(item["row_count"]) != rows[source_index]:
            raise ValueError(f"候选 Episode {source_index} 帧数不一致")
        if source_index in result:
            raise ValueError(f"候选集重复包含源 Episode {source_index}")
        if output_index in result.values():
            raise ValueError(f"候选集重复使用输出 Episode {output_index}")
        result[source_index] = output_index
    return result


def write_dataset(preview: Path, output: Path, spec: dict, manifest: dict) -> dict:
    import h5py
    import numpy as np
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    from matching_preview import (ACTION_FIELD, CAMERA_SOURCE_FIELDS, ENCODER_THREADS, FPS, STATE_FIELD,
                                  VCODEC, decode_rgb, feature_definitions, inspect_frame_capabilities,
                                  inspect_shapes, read_numeric, validate_depth)

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
    dataset = LeRobotDataset.create(
        repo_id="local/matching-cable-reviewed", root=output, fps=FPS,
        features=feature_definitions(shapes, capabilities["optional_present"]), robot_type="dual_arm_teleoperation",
        video_backend="pyav", vcodec=VCODEC, image_writer_processes=0,
        image_writer_threads=0, batch_encoding_size=1, streaming_encoding=True,
        encoder_threads=ENCODER_THREADS,
    )
    output_episodes = []
    source_files = []
    for candidate in candidates:
        source_index = int(candidate["source_episode_index"])
        output_index = int(candidate["output_episode_index"])
        source_id = raw_ids[source_index]
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
    }
    final_manifest["source"]["episode_mapping"] = {
        str(item["episode_index"]): item["source_episode_id"] for item in output_episodes}
    write_json(output / "source_manifest.json", final_manifest)
    write_json(output / "annotations.json", {"episodes": spec["episodes"]})
    missing_cameras = ", ".join(capabilities["missing_cameras"]) or "none"
    missing_depth = ", ".join(capabilities["depth_missing"]) or "none"
    missing_optional = ", ".join(capabilities["optional_missing"]) or "none"
    (output / "CONVERSION.md").write_text(
        "# Matching Cable conversion\n\n"
        "Master joint position is written as action; puppet joint position is written as observation state. "
        "Each source episode is one LeRobot episode at 20 FPS. "
        f"Present RGB streams: {', '.join(cameras)}. Missing RGB streams: {missing_cameras}. "
        f"Present depth streams are validated but not written; missing depth streams: {missing_depth}. "
        f"Missing optional numeric fields: {missing_optional}. Tactile fields are not modelled. "
        "Hardware synchronization is not claimed.\n", encoding="utf-8")
    return {"format_version": "v3.0", "episodes": len(output_episodes),
            "frames": sum(item["length"] for item in output_episodes),
            "tasks": sorted({item["task"] for item in output_episodes}),
            "cameras": cameras, "missing_cameras": capabilities["missing_cameras"],
            "depth_source_cameras": capabilities["depth_present"],
            "missing_depth_source_cameras": capabilities["depth_missing"],
            "optional_fields": manifest["source"].get("optional_fields", {}),
            "source_fingerprint": manifest.get("input_fingerprint")}

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
    snapshot_id = (manifest.get("conversion_spec") or {}).get("snapshot_id")
    return {"format_version": "v3.0", "episode_count": len(episodes), "row_count": expected_rows,
            "checked_numeric_frames": 0 if archive_only else expected_rows,
            "output_numeric_frames_checked": expected_rows,
            "output_loader_consistency_checked": True,
            "source_numeric_consistency_rechecked": False,
            "verification_scope": "输出 parquet 与官方加载器一致；本次未重新读取源 HDF5",
            "cameras": cameras, "missing_cameras": manifest["source"].get("missing_cameras", []),
            "missing_depth_source_cameras": manifest["source"].get("missing_depth_source_cameras", []),
            "optional_fields": manifest["source"].get("optional_fields", {}),
            "source_fingerprint": manifest.get("input_fingerprint"), "snapshot_id": snapshot_id,
            "sample_count": 3 * len(episodes) * len(cameras), "samples": samples,
            "batch_size": min(2, len(dataset)), "offline": True, "video_backend": "pyav",
            "camera_synchronization_claim": "未声明硬件同步", "file_count": 0}

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
                           "optional_fields": optional_fields}
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
                details = write_dataset(args.input.resolve(), args.output.resolve(), spec, manifest)
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
