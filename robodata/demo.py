"""Create explicitly marked fault-injection copies of the pinned public sample."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile

import numpy as np

from .config import CAMERA, DEFAULT_DATA_ROOT, EPISODES, PROJECT_ROOT, REPO_ID, REVISION
from .dataset import dataset_fingerprint, load_episode, load_metadata
from .source import DEMO_MANIFEST_NAME, MANIFEST_NAME, SAMPLE_FILES, file_sha256

DEMO_VERSION = 1
SCENARIOS = ("anomalies", "missing_video")
DEFAULT_DEMO_ROOT = PROJECT_ROOT / "work" / "failure_demo"
DEFAULT_MISSING_VIDEO_ROOT = PROJECT_ROOT / "work" / "missing_video_demo"
DEMO_README = "故障注入说明.md"


def read_demo_manifest(root: Path) -> dict | None:
    """Return the explicit demo label, or None for a directory without one."""
    path = Path(root) / DEMO_MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError("演示副本标记无法读取，请使用新的目标目录重新生成") from exc
    if (not isinstance(manifest, dict) or manifest.get("source_kind") != "injected_test"
            or manifest.get("demo_version") != DEMO_VERSION
            or manifest.get("scenario") not in SCENARIOS
            or not isinstance(manifest.get("injections"), list)):
        raise ValueError("演示副本标记不符合当前版本，请使用新的目标目录重新生成")
    return manifest


def _verified_source(source: Path) -> dict:
    if read_demo_manifest(source) is not None:
        raise ValueError("源目录必须是下载的原始公开样本，不能从演示副本再次注入")
    metadata = load_metadata(source)
    manifest = metadata["manifest"]
    if manifest.get("repo_id") != REPO_ID or manifest.get("revision") != REVISION:
        raise ValueError("源目录缺少匹配固定版本的下载清单，请先获取原始样本")
    hashes = {item.get("path"): item.get("sha256") for item in manifest.get("files", [])
              if isinstance(item, dict)}
    for relative in SAMPLE_FILES:
        path = source / relative
        if not path.is_file() or file_sha256(path) != hashes.get(relative):
            raise ValueError(f"原始样本缺失或与下载清单不一致：{relative}。请先重新获取样本")
    return metadata


def _reuse_existing(target: Path, upstream_fingerprint: str, scenario: str) -> bool:
    if not target.exists():
        return False
    if not target.is_dir():
        raise ValueError("演示目标已存在且不是文件夹，请指定新的空目录")
    if not any(target.iterdir()):
        return False
    marker = read_demo_manifest(target)
    if marker is None:
        raise ValueError("演示目标目录中已有其他文件；不会覆盖，请指定新的空目录")
    if (marker.get("scenario") != scenario
            or marker.get("upstream", {}).get("input_fingerprint") != upstream_fingerprint):
        raise ValueError("已有演示副本的来源或情景不同；不会覆盖，请指定新的目标目录")
    entries = marker.get("generated_files", [])
    expected = {relative for relative in SAMPLE_FILES}
    if scenario == "missing_video":
        expected.remove(f"videos/chunk-000/{CAMERA}/episode_000000.mp4")
    expected.update((MANIFEST_NAME, DEMO_README))
    if not isinstance(entries, list) or {item.get("path") for item in entries if isinstance(item, dict)} != expected:
        raise ValueError("已有演示副本的文件清单无效，请指定新的目标目录")
    for item in entries:
        path = target / item["path"]
        if not path.is_file() or file_sha256(path) != item.get("sha256"):
            raise ValueError("已有演示副本被修改或文件缺失；为保留你的改动，请指定新的目标目录")
    if scenario == "missing_video" and (target / f"videos/chunk-000/{CAMERA}/episode_000000.mp4").exists():
        raise ValueError("已有视频缺失演示副本中出现了视频文件，请指定新的目标目录")
    return True


def _inject_anomalies(stage: Path) -> list[dict]:
    episode = load_episode(stage, 0)
    table = episode.table.copy(deep=True)
    if len(table) <= 151 or "action" not in table or "timestamp" not in table:
        raise ValueError("任务 0 的字段或行数不满足当前故障演示要求")
    vector = np.asarray(table.iloc[50]["action"]).copy()
    if vector.shape != (6,) or not np.isfinite(vector).all():
        raise ValueError("任务 0 第 50 行不是有效的原始 6 维动作，无法生成指定演示")
    before_action = float(vector[2])
    vector[2] = np.nan
    table.at[50, "action"] = vector
    before_reversal = float(table.iloc[100]["timestamp"])
    after_reversal = float(table.iloc[99]["timestamp"]) - 1 / episode.fps
    table.loc[100, "timestamp"] = after_reversal
    before_gap = float(table.iloc[150]["timestamp"])
    after_gap = before_gap + 0.6 / episode.fps
    table.loc[150, "timestamp"] = after_gap
    # Record the stored float32 values, not the higher-precision assignment intent.
    after_reversal = float(table.iloc[100]["timestamp"])
    after_gap = float(table.iloc[150]["timestamp"])
    # Write a separate file, never a hard link to the downloaded original.
    table.to_parquet(episode.data_path, index=False)
    return [
        {"episode_index": 0, "row_index": 50, "field": "action", "component_index": 2,
         "rule": "numeric_finite", "description": "动作第 3 维注入 NaN，验证非有限数值能准确定位",
         "before": before_action, "after": "nan"},
        {"episode_index": 0, "row_index": 100, "field": "timestamp", "rule": "timestamp_monotonic",
         "description": "时间改为上一行时间减 1 个采样间隔；同时影响第 101 行间隔和本行视频时间对应",
         "before": before_reversal, "after": after_reversal},
        {"episode_index": 0, "row_index": 150, "field": "timestamp", "rule": "timestamp_interval",
         "description": "时间增加 0.6 个采样间隔；第 150、151 行间隔异常，并影响本行视频时间对应",
         "before": before_gap, "after": after_gap},
    ]


def create_failure_demo(source_root: Path = DEFAULT_DATA_ROOT, target_root: Path | None = None,
                        *, scenario: str = "anomalies") -> Path:
    """Create or verify/reuse a demo copy. Refuse to overwrite unrelated/edited files.

    The public download manifest stays unchanged, so reports can show exactly
    which inputs differ. An additional marker identifies the copy as injected_test.
    """
    if scenario not in SCENARIOS:
        raise ValueError("不支持的演示情景，请选择数据异常或视频缺失")
    source = Path(source_root).resolve()
    default_target = DEFAULT_DEMO_ROOT if scenario == "anomalies" else DEFAULT_MISSING_VIDEO_ROOT
    target = Path(target_root if target_root is not None else default_target).resolve()
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("演示目录必须与原始样本目录分开，不能相同或互相包含")
    _verified_source(source)
    original_fingerprint = dataset_fingerprint(source)
    if _reuse_existing(target, original_fingerprint, scenario):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    # A private temporary sibling is the only directory cleaned up on failure.
    # Existing target content is never deleted or recursively replaced.
    with tempfile.TemporaryDirectory(prefix=".robodata-demo-", dir=target.parent) as temporary:
        stage = Path(temporary) / "sample"
        stage.mkdir()
        for relative in (*SAMPLE_FILES, MANIFEST_NAME):
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / relative, destination)
        if scenario == "anomalies":
            injections = _inject_anomalies(stage)
            description = "数据异常演示：仅任务 0 注入 3 处问题，其余 4 条任务保持原样。"
        else:
            relative = f"videos/chunk-000/{CAMERA}/episode_000000.mp4"
            (stage / relative).unlink()
            injections = [{"episode_index": 0, "row_index": None, "field": "video",
                           "rule": "video_decode", "description": "移除副本中的任务 0 主相机视频，验证任务 1–4 继续检查",
                           "before": relative, "after": "文件缺失"}]
            description = "视频缺失演示：仅任务 0 的主相机视频缺失，其余文件保持原样。"
        lines = ["# 故障注入测试副本", "", "本目录不是未经修改的公开样本。仅用于验证质检流程，不用于训练或评估原数据质量。",
                 "", description, "", f"上游：{REPO_ID}", f"固定版本：{REVISION}",
                 "", "行位置从 0 开始。源样本和原始下载清单未修改。", ""]
        for entry in injections:
            lines.append(f"- 任务 {entry['episode_index']}，行 {entry['row_index'] if entry['row_index'] is not None else '整段视频'}：{entry['description']}")
        lines.extend(["", "时间问题可触发多条规则，问题记录条数不等于注入点数量。", "视频缺失时应看到读取失败和未检查项，其余任务仍可完成并导出报告。", ""])
        (stage / DEMO_README).write_text("\n".join(lines), encoding="utf-8")
        generated = [{"path": path.relative_to(stage).as_posix(), "sha256": file_sha256(path)}
                     for path in sorted(stage.rglob("*")) if path.is_file()]
        manifest = {"demo_version": DEMO_VERSION, "source_kind": "injected_test", "scenario": scenario,
                    "description": description,
                    "upstream": {"repo_id": REPO_ID, "revision": REVISION, "input_fingerprint": original_fingerprint},
                    "episodes": list(EPISODES), "injections": injections, "generated_files": generated}
        (stage / DEMO_MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        if dataset_fingerprint(source) != original_fingerprint:
            raise ValueError("复制期间原始样本发生变化，本次演示副本未发布，请检查来源后重试")
        if target.exists():
            target.rmdir()  # Only an empty target can be removed.
        stage.rename(target)
    return target
