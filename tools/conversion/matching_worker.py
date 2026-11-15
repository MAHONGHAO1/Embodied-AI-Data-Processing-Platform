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

from robodata.matching import CAMERAS, FPS, SOURCE_KIND

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
    if manifest.get("source", {}).get("cameras") != list(CAMERAS):
        raise ValueError("输入相机配置与固定四路 RGB 配置不一致")
    return manifest


def candidate_rows(spec: dict, manifest: dict) -> dict[int, int]:
    rows = {int(item["episode_index"]): int(item["length"]) for item in manifest["episodes"]}
    result = {}
    for item in spec["episodes"]:
        source_index = int(item["source_episode_index"])
        if source_index not in rows:
            raise ValueError(f"候选 Episode {source_index} 不在预览产物中")
        if int(item["row_count"]) != rows[source_index]:
            raise ValueError(f"候选 Episode {source_index} 帧数不一致")
        result[source_index] = int(item["output_episode_index"])
    return result


def _raw_frames(source_root: Path, source_id: int, expected_length: int) -> list[Path]:
    directory = source_root / str(source_id)
    frames = sorted(directory.glob("*.hdf5"), key=lambda path: int(path.stem))
    if len(frames) < expected_length:
        raise ValueError(f"源 Episode {source_id} 帧数不足：期望 {expected_length}，实际 {len(frames)}")
    selected = frames[:expected_length]
    numbers = [int(path.stem) for path in selected]
    if numbers != list(range(numbers[0], numbers[0] + expected_length)):
        raise ValueError(f"源 Episode {source_id} 帧序号不连续")
    return selected


def write_dataset(preview: Path, output: Path, spec: dict, manifest: dict) -> dict:
    import h5py
    import numpy as np
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    from matching_preview import (ACTION_FIELD, CAMERAS, DEPTH_CAMERAS, FPS, STATE_FIELD,
                                  decode_rgb, feature_definitions, inspect_shapes, read_numeric,
                                  validate_depth)

    source_root = Path(manifest["source"]["source_root"])
    records = {int(item["episode_index"]): item for item in manifest["episodes"]}
    raw_ids = {int(key): int(value) for key, value in manifest["source"]["episode_mapping"].items()}
    candidates = spec["episodes"]
    first_source = int(candidates[0]["source_episode_index"])
    first_frames = _raw_frames(source_root, raw_ids[first_source], int(records[first_source]["length"]))
    shapes = inspect_shapes({"frames": first_frames})
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise FileExistsError("输出目录已有内容，不能覆盖")
        output.rmdir()
    output.parent.mkdir(parents=True, exist_ok=True)
    dataset = LeRobotDataset.create(
        repo_id="local/matching-cable-reviewed", root=output, fps=FPS,
        features=feature_definitions(shapes), robot_type="dual_arm_teleoperation",
        video_backend="pyav", vcodec="h264", image_writer_processes=0,
        image_writer_threads=0, batch_encoding_size=1, streaming_encoding=True,
    )
    output_episodes = []
    source_files = []
    for candidate in candidates:
        source_index = int(candidate["source_episode_index"])
        output_index = int(candidate["output_episode_index"])
        source_id = raw_ids[source_index]
        length = int(records[source_index]["length"])
        frames = _raw_frames(source_root, source_id, length)
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
                frame = {
                    "task": task,
                    "observation.state": state,
                    "action": action,
                    "observation.eef": read_numeric(handle, "puppet/eef", 28),
                    "master.eef": read_numeric(handle, "master/eef", 28),
                    "observation.joint_effort": read_numeric(handle, "puppet/joint_effort", 16),
                    "observation.force_torque": read_numeric(handle, "puppet/6f", 12),
                    "source.timestamp_ms": np.asarray([float(handle["timestamps"][0])], dtype=np.float32),
                }
                for index, camera in enumerate(CAMERAS):
                    frame[camera] = decode_rgb(handle[f"observations/rgb_images/camera_{index}"][0],
                                               f"camera_{index}", shapes[camera])
                for depth_field in DEPTH_CAMERAS:
                    validate_depth(handle[depth_field][0], depth_field.split("/")[-1])
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
    (output / "CONVERSION.md").write_text(
        "# Matching Cable conversion\n\n"
        "Master joint position is written as action; puppet joint position is written as observation state. "
        "Each source episode is one LeRobot episode at 20 FPS. Four RGB streams are re-encoded to H.264. "
        "Depth PNGs are validated in the source but are not included in this output. "
        "Hardware synchronization is not claimed.\n", encoding="utf-8")
    return {"format_version": "v3.0", "episodes": len(output_episodes),
            "frames": sum(item["length"] for item in output_episodes),
            "tasks": sorted({item["task"] for item in output_episodes}),
            "source_fingerprint": manifest.get("input_fingerprint")}

def verify_dataset(output: Path, artifacts: Path, *, archive_only: bool = False) -> dict:
    import numpy as np
    import pandas as pd
    import torch
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    from torch.utils.data import DataLoader
    from matching_preview import CAMERAS

    manifest = source_manifest(output)
    episodes = manifest["episodes"]
    expected_rows = sum(int(item["length"]) for item in episodes)
    tables = pd.concat([pd.read_parquet(path) for path in sorted((output / "data").rglob("*.parquet"))],
                       ignore_index=True)
    tables["index"] = tables["index"].astype(int)
    tables = tables.set_index("index", drop=False)
    if len(tables) != expected_rows or tables["episode_index"].nunique() != len(episodes):
        raise ValueError("输出表格数量与源清单不一致")
    numeric = ["observation.state", "action", "observation.eef", "master.eef",
               "observation.joint_effort", "observation.force_torque", "source.timestamp_ms"]
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
            for camera in CAMERAS:
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
    for camera in CAMERAS:
        height, width = [int(value) for value in manifest["camera_metadata"][camera]["resolution"][:2]]
        if tuple(batch[camera].shape)[1:] != (3, height, width):
            raise ValueError(f"官方 DataLoader {camera} 图像维度不正确")
    snapshot_id = (manifest.get("conversion_spec") or {}).get("snapshot_id")
    return {"format_version": "v3.0", "episode_count": len(episodes), "row_count": expected_rows,
            "checked_numeric_frames": 0 if archive_only else expected_rows,
            "output_numeric_frames_checked": expected_rows,
            "source_numeric_consistency_rechecked": not archive_only,
            "source_fingerprint": manifest.get("input_fingerprint"), "snapshot_id": snapshot_id,
            "sample_count": 3 * len(episodes), "samples": samples,
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
    if cameras != list(CAMERAS) or float(info["fps"]) != float(FPS):
        raise ValueError("预览元数据的相机或帧率不符合 Matching Cable 配置")
    tables = pd.concat([pd.read_parquet(path) for path in sorted((source / "data").rglob("*.parquet"))],
                       ignore_index=True)
    if not np.isfinite(np.stack(tables["observation.state"].to_numpy())).all():
        raise ValueError("预览 observation.state 包含非有限值")
    if not np.isfinite(np.stack(tables["action"].to_numpy())).all():
        raise ValueError("预览 action 包含非有限值")
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
            if args.stage == "file_validation":
                details = {"episode_count": len(rows), "row_count": sum(rows.values()),
                           "source_fingerprint": manifest.get("input_fingerprint"),
                           "cameras": list(CAMERAS), "fps": FPS}
            elif args.stage == "matching_validation":
                details = {"episode_count": len(rows), "row_count": sum(rows.values()),
                           "state_field": manifest["source"]["state_field"],
                           "action_field": manifest["source"]["action_field"],
                           "depth_source_cameras": manifest["source"]["depth_source_cameras"],
                           "timestamp_rule": "source milliseconds, 50 ms/frame -> 20 FPS"}
            elif args.stage == "field_mapping":
                details = {"observation.state": {"source": "puppet/joint_position", "shape": [16]},
                           "action": {"source": "master/joint_position", "shape": [16]},
                           "cameras": list(CAMERAS), "fps": FPS}
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
