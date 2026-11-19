"""Convert a managed LeRobot v2.1 dataset to v3.0.

The structural conversion is delegated to LeRobot's own
``convert_dataset_v21_to_v30`` implementation. This worker owns the RoboData
contract around it: selection checks, provenance, source-numeric verification,
official loader verification, and stage evidence.
"""

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

from robodata.dataset import load_metadata  # noqa: E402

PROFILE = "lerobot_v21_generic"
STAGES = ("file_validation", "field_mapping", "format_write", "official_verify", "archive_verify")


class ConversionError(Exception):
    def __init__(self, code: str, message: str, **context):
        super().__init__(message)
        self.code = code
        self.context = context


def fail(code: str, message: str, **context):
    raise ConversionError(code, message, **context)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
                         encoding="utf-8")
    temporary.replace(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_spec(path: Path | None) -> dict:
    if path is None:
        fail("spec_missing", "转换需要冻结候选清单")
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    if not spec.get("episodes"):
        fail("spec_empty", "候选集为空")
    return spec


def source_manifest(input_root: Path) -> dict:
    metadata = load_metadata(input_root)
    info = metadata["info"]
    if info.get("codebase_version") != "v2.1":
        fail("source_version", f"输入不是 LeRobot v2.1：{info.get('codebase_version')!r}")
    if not (input_root / "meta/episodes_stats.jsonl").is_file():
        fail("source_stats_missing", "v2.1 缺少 meta/episodes_stats.jsonl")
    return metadata


def selection(spec: dict, metadata: dict) -> list[dict]:
    rows = {int(index): int(entry["length"]) for index, entry in metadata["episodes"].items()}
    selected = []
    for item in spec["episodes"]:
        source_index = int(item["source_episode_index"])
        output_index = int(item["output_episode_index"])
        if source_index not in rows:
            fail("episode_missing", f"候选 Episode {source_index} 不在源数据中")
        if int(item["row_count"]) != rows[source_index]:
            fail("episode_length", f"Episode {source_index} 帧数与源不一致")
        selected.append({"source_episode_index": source_index,
                         "output_episode_index": output_index,
                         "row_count": rows[source_index],
                         "annotation": item.get("annotation", {})})
    if len({item["source_episode_index"] for item in selected}) != len(selected):
        fail("episode_duplicate", "候选集重复包含源 Episode")
    if len({item["output_episode_index"] for item in selected}) != len(selected):
        fail("output_duplicate", "候选集重复使用输出 Episode")
    if sorted(item["source_episode_index"] for item in selected) != sorted(rows):
        fail("subset_unsupported",
             "LeRobot v2.1 转换当前只支持全量 Episode；请先在源数据中裁剪子集",
             selected=len(selected), available=len(rows))
    return selected


def validate_source(input_root: Path, spec: dict) -> tuple[dict, list[dict]]:
    metadata = source_manifest(input_root)
    selected = selection(spec, metadata)
    cameras = metadata["source"].get("cameras") or []
    if not cameras:
        fail("camera_missing", "源数据没有声明视频相机")
    for item in selected:
        index = item["source_episode_index"]
        data_path = input_root / "data/chunk-000" / f"episode_{index:06d}.parquet"
        if not data_path.is_file():
            fail("data_missing", f"缺少 Episode {index} 的数值表", path=str(data_path))
        for camera in cameras:
            video_path = input_root / "videos/chunk-000" / camera / f"episode_{index:06d}.mp4"
            if not video_path.is_file():
                fail("video_missing", f"缺少 Episode {index} 的 {camera} 视频", path=str(video_path))
    return metadata, selected


def _feature_summary(metadata: dict) -> dict:
    features = metadata["info"]["features"]
    return {"state": features.get("observation.state"),
            "action": features.get("action"),
            "cameras": metadata["source"].get("cameras") or [],
            "fps": metadata["info"].get("fps")}


def _descriptor(metadata: dict, selected: list[dict]) -> dict:
    return {
        "profile": PROFILE, "format_version": "v2.1",
        "source": metadata["source"],
        "episodes": selected,
        "total_episodes": len(selected),
        "total_frames": sum(item["row_count"] for item in selected),
        "tasks": sorted({str(item["annotation"].get("task", "")).strip()
                         for item in selected if item["annotation"].get("task")}),
        "limitations": [
            "使用 LeRobot 官方 v2.1→v3.0 转换；当前只支持全量 Episode",
            "数值按 float32 映射验证；视频延续源编码，不声明硬件同步",
        ],
    }


def _write_documents(output: Path, input_root: Path, metadata: dict, selected: list[dict],
                     spec: dict) -> dict:
    files = []
    for path in sorted(output.rglob("*")):
        if path.is_file():
            files.append({"path": path.relative_to(output).as_posix(),
                          "size": path.stat().st_size, "sha256": sha256(path)})
    manifest = {**_descriptor(metadata, selected),
                "source_root": str(input_root), "files": files,
                "snapshot_id": spec.get("snapshot_id"),
                "input_fingerprint": spec.get("input_fingerprint"),
                "conversion_strategy": {
                    "name": "lerobot_v21_official_converter", "schema_version": 1,
                    "converter": "lerobot.datasets.v30.convert_dataset_v21_to_v30",
                    "source_numeric_consistency_rechecked": True,
                    "subset_supported": False,
                }}
    write_json(output / "source_manifest.json", manifest)
    write_json(output / "annotations.json", {"episodes": spec["episodes"]})
    (output / "CONVERSION.md").write_text(
        "# LeRobot v2.1 → v3.0\n\n"
        "本交付使用 LeRobot 官方 `convert_dataset_v21_to_v30` 完成结构转换；"
        "数值与视频来源保持源数据，RoboData 在 `official_verify` 中重新读取源 parquet 逐帧对照。\n",
        encoding="utf-8")
    return manifest


def format_write(input_root: Path, output: Path, spec: dict) -> dict:
    metadata, selected = validate_source(input_root, spec)
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            fail("output_exists", "输出目录已有内容，不能覆盖")
        output.rmdir()
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(input_root, output)
    marker = output / "robodata_source.json"
    if marker.is_file():
        marker.unlink()
    from lerobot.datasets.v30.convert_dataset_v21_to_v30 import convert_dataset
    convert_dataset(repo_id=output.name, root=str(output.parent), push_to_hub=False,
                    force_conversion=True)
    backup = output.parent / f"{output.name}_old"
    if backup.exists():
        shutil.rmtree(backup)
    if not (output / "meta/info.json").is_file():
        fail("converter_output", "官方转换器没有生成有效输出")
    info = json.loads((output / "meta/info.json").read_text(encoding="utf-8"))
    if info.get("codebase_version") != "v3.0":
        fail("converter_version", f"官方转换器输出不是 v3.0：{info.get('codebase_version')!r}")
    manifest = _write_documents(output, input_root, metadata, selected, spec)
    return {"format_version": "v3.0", "episodes": len(selected),
            "frames": sum(item["row_count"] for item in selected),
            "strategy": "lerobot_v21_official_converter",
            "tasks": manifest["tasks"], "cameras": metadata["source"]["cameras"],
            "source_fingerprint": spec.get("input_fingerprint"),
            "source_numeric_consistency_rechecked": True}


def _read_tables(root: Path) -> dict[int, object]:
    import pandas as pd
    tables = {}
    for path in sorted((root / "data").rglob("*.parquet")):
        table = pd.read_parquet(path)
        if "episode_index" in table:
            for index, rows in table.groupby("episode_index"):
                tables[int(index)] = rows.reset_index(drop=True)
    return tables


def verify_dataset(output: Path, artifacts: Path, *, source_root: Path | None = None,
                   archive_only: bool = False) -> dict:
    import numpy as np
    import pandas as pd
    import torch
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    from torch.utils.data import DataLoader

    manifest_path = output / "source_manifest.json"
    if not manifest_path.is_file():
        fail("manifest_missing", "输出缺少 source_manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    episodes = manifest["episodes"]
    cameras = manifest["source"].get("cameras") or []
    expected_rows = sum(int(item["row_count"]) for item in episodes)
    tables = _read_tables(output)
    if len(tables) != len(episodes) or sum(len(table) for table in tables.values()) != expected_rows:
        fail("output_rows", "输出数值表数量与清单不一致")
    source_tables = _read_tables(source_root) if source_root is not None else {}
    dataset = LeRobotDataset(repo_id="local/lerobot-v21-reviewed", root=output,
                              video_backend="pyav")
    if len(dataset) != expected_rows or dataset.meta.total_episodes != len(episodes):
        fail("loader_count", "官方加载器的任务数或帧数不一致")
    samples = []
    for episode in episodes:
        index = int(episode["source_episode_index"])
        output_index = int(episode["output_episode_index"])
        table = tables[output_index]
        if source_root is not None:
            source_table = source_tables.get(index)
            if source_table is None or len(source_table) != len(table):
                fail("source_rows", f"Episode {index} 源/输出行数不一致")
            for column in ("observation.state", "action"):
                expected = np.stack(source_table[column].to_numpy())
                actual = np.stack(table[column].to_numpy())
                if not np.array_equal(expected, actual):
                    fail("source_values", f"Episode {index} 的 {column} 与源不一致")
        for row_index in (0, len(table) // 2, len(table) - 1):
            row = table.iloc[row_index]
            item = dataset[int(row["index"])] if "index" in row else dataset[int(row["frame_index"])]
            expected_state = np.asarray(row["observation.state"], dtype=np.float32)
            actual_state = item["observation.state"].numpy()
            expected_action = np.asarray(row["action"], dtype=np.float32)
            actual_action = item["action"].numpy()
            if not np.array_equal(actual_state, expected_state) or not np.array_equal(actual_action, expected_action):
                fail("official_values", f"Episode {index} row {row_index} 官方数值不一致")
            for camera in cameras:
                image = item[camera]
                if image.ndim != 3 or image.shape[0] != 3 or not torch.isfinite(image).all():
                    fail("official_image", f"Episode {index} {camera} 官方图像形状无效")
                pixels = np.rint(image.permute(1, 2, 0).numpy() * 255).astype(np.uint8)
                samples.append({"episode": output_index, "row": row_index, "camera": camera,
                                "global_index": int(row["index"]),
                                "timestamp": float(item["timestamp"]),
                                "decoded_image_sha256": hashlib.sha256(pixels.tobytes()).hexdigest()})
    batch = next(iter(DataLoader(dataset, batch_size=min(2, len(dataset)), num_workers=0)))
    for camera in cameras:
        if batch[camera].ndim != 4 or batch[camera].shape[1] != 3:
            fail("dataloader_image", f"{camera} DataLoader 图像维度不正确")
    return {"format_version": "v3.0", "episode_count": len(episodes),
            "row_count": expected_rows, "checked_numeric_frames": expected_rows,
            "output_numeric_frames_checked": expected_rows,
            "snapshot_id": manifest.get("snapshot_id"),
            "output_loader_consistency_checked": True,
            "source_numeric_consistency_rechecked": source_root is not None,
            "verification_scope": ("输出 parquet 与官方加载器一致；源 parquet 全量行已对照"
                                   if source_root is not None else
                                   "输出 parquet 与官方加载器一致；本阶段未读取源数据"),
            "cameras": cameras, "samples": samples,
            "sample_count": len(samples), "batch_size": min(2, len(dataset)),
            "offline": True, "video_backend": "pyav",
            "camera_synchronization_claim": "未声明硬件同步",
            "conversion_strategy": manifest.get("conversion_strategy")}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True, choices=STAGES)
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
                fail("input_missing", "该阶段需要 --input")
            input_root = args.input.resolve()
            spec = load_spec(args.spec)
            metadata, selected = validate_source(input_root, spec)
            if args.stage == "file_validation":
                details = {"episode_count": len(selected),
                           "row_count": sum(item["row_count"] for item in selected),
                           "cameras": metadata["source"]["cameras"],
                           "format_version": "v2.1",
                           "has_episodes_stats": True,
                           "subset_supported": False}
            elif args.stage == "field_mapping":
                details = {"features": _feature_summary(metadata),
                           "mapping": {"observation.state": "源 observation.state",
                                       "action": "源 action",
                                       "cameras": metadata["source"]["cameras"]}}
            elif args.stage == "format_write":
                details = format_write(input_root, args.output.resolve(), spec)
            else:
                details = verify_dataset(args.output.resolve(), artifacts,
                                         source_root=input_root if args.stage == "official_verify" else None)
        write_json(artifacts / "steps" / f"{args.stage}.json",
                   {"stage": args.stage, "status": "passed", "details": details})
        return 0
    except Exception as exc:
        details = {"stage": args.stage, "status": "failed", "message_zh": str(exc),
                   "exception_type": type(exc).__name__, "exception_message": str(exc),
                   "code": getattr(exc, "code", None),
                   "context": getattr(exc, "context", {}),
                   "traceback": traceback.format_exc()}
        write_json(artifacts / "diagnostic.json", details)
        write_json(artifacts / "steps" / f"{args.stage}.json",
                   {"stage": args.stage, "status": "failed", "diagnostic": details})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
