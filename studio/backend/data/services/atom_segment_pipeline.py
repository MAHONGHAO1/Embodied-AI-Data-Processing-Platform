"""Three-stage atom_segments pipeline: MCAP splitting -> QRDF -> LeRobot."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from data.services.atom_segment_mcap import (
    episode_output_stem,
    load_manifest,
    resolve_episode_time_ranges,
    split_episode_mcap,
)
from data.services.conversion import convert_mcap_to_qrdf, convert_qrdf_to_lerobot
from data.utils.formatting import format_api_datetime, normalize_api_fps

LogFn = Callable[[str], None]


def _default_log(message: str) -> None:
    print(message, flush=True)


def stage_output_dirs(output_root: Path, folder_name: str) -> dict[str, Path]:
    return {
        "mcap": output_root / "MCAP" / folder_name,
        "qrdf": output_root / "QRDF" / folder_name,
        "lerobot": output_root / "LEROBOT" / folder_name,
    }


def discover_source_folders(data_root: Path) -> list[Path]:
    if not data_root.is_dir():
        raise FileNotFoundError(f"数据根目录不存在: {data_root}")
    folders: list[Path] = []
    for child in sorted(data_root.iterdir()):
        if not child.is_dir():
            continue
        if (child / "atom_segments.json").is_file() and list(child.glob("*.mcap")):
            folders.append(child)
    return folders


def process_source_folder(
    source_dir: Path,
    *,
    output_root: Path,
    image_workers: int | None = None,
    fps: float | None = None,
    lerobot_version: str = "v3.0",
    generate_preview: bool = False,
    skip_mcap_split: bool = False,
    skip_qrdf: bool = False,
    skip_lerobot: bool = False,
    episode_limit: int | None = None,
    log: LogFn = _default_log,
) -> dict[str, Any]:
    folder_name = source_dir.name
    manifest_path = source_dir / "atom_segments.json"
    manifest = load_manifest(manifest_path)
    dirs = stage_output_dirs(output_root, folder_name)
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    log(f"\n=== 处理 {folder_name} ({len(manifest.episodes)} episodes) ===")
    log(f"源目录: {source_dir}")
    log(f"输出: MCAP={dirs['mcap']} QRDF={dirs['qrdf']} LEROBOT={dirs['lerobot']}")

    episode_ranges = resolve_episode_time_ranges(source_dir, manifest)
    if episode_limit is not None:
        episode_ranges = episode_ranges[:episode_limit]

    episodes_report: list[dict[str, Any]] = []
    split_total = qrdf_total = lerobot_total = 0.0

    for episode_range in episode_ranges:
        episode_index = episode_range.episode_index
        stem = episode_output_stem(folder_name, episode_index)
        episode_report: dict[str, Any] = {
            "episode_index": episode_index,
            "stem": stem,
            "start_frame": episode_range.start_frame,
            "end_frame": episode_range.end_frame,
            "num_frames": episode_range.num_frames,
        }

        mcap_path = dirs["mcap"] / f"{stem}.mcap"
        qrdf_dir = dirs["qrdf"] / stem
        lerobot_dir = dirs["lerobot"] / stem

        if skip_mcap_split and mcap_path.is_file():
            log(f"\n[{folder_name}] episode {episode_index}: 跳过 MCAP 拆分（已存在）")
            episode_report["mcap_path"] = str(mcap_path)
        elif not skip_mcap_split:
            log(f"\n[{folder_name}] episode {episode_index}: MCAP 拆分 ...")
            split_started = time.perf_counter()
            split_result = split_episode_mcap(
                source_dir=source_dir,
                output_dir=dirs["mcap"],
                folder_name=folder_name,
                episode_range=episode_range,
            )
            split_elapsed = time.perf_counter() - split_started
            split_total += split_elapsed
            episode_report["mcap_split"] = {
                **split_result,
                "duration_sec": round(split_elapsed, 3),
            }
            mcap_path = Path(split_result["mcap_path"])
            log(
                f"  完成: {mcap_path.name} "
                f"messages={split_result['message_count']} "
                f"{split_elapsed:.2f}s"
            )
        else:
            raise FileNotFoundError(f"MCAP 不存在且未执行拆分: {mcap_path}")

        if not skip_qrdf:
            log(f"[{folder_name}] episode {episode_index}: MCAP → QRDF ...")
            qrdf_started = time.perf_counter()
            qrdf_result = convert_mcap_to_qrdf(
                mcap_path=str(mcap_path),
                output_dir=str(qrdf_dir),
                episode_id=f"episode_{episode_index:06d}",
                task_name=stem,
                dataset_name=f"{folder_name}_{stem}",
                image_workers=image_workers,
                generate_preview=generate_preview,
            )
            qrdf_elapsed = time.perf_counter() - qrdf_started
            qrdf_total += qrdf_elapsed
            episode_report["qrdf"] = {
                "qrdf_path": qrdf_result["qrdf_path"],
                "duration_sec": round(qrdf_elapsed, 3),
                "quality_ok": qrdf_result["quality"].get("ok"),
            }
            log(f"  完成: {qrdf_elapsed:.2f}s validate={qrdf_result['quality'].get('ok')}")
        else:
            episode_report["qrdf"] = {"skipped": True, "qrdf_path": str(qrdf_dir)}

        if not skip_lerobot:
            log(f"[{folder_name}] episode {episode_index}: QRDF → LeRobot ...")
            lerobot_started = time.perf_counter()
            lerobot_result = convert_qrdf_to_lerobot(
                storage_path=str(qrdf_dir),
                output_dir=str(lerobot_dir),
                fps=fps or manifest.fps,
                lerobot_version=lerobot_version,
            )
            lerobot_elapsed = time.perf_counter() - lerobot_started
            lerobot_total += lerobot_elapsed
            episode_report["lerobot"] = {
                "lerobot_path": lerobot_result["lerobot_path"],
                "duration_sec": round(lerobot_elapsed, 3),
                "lerobot_fps": normalize_api_fps(lerobot_result["lerobot_fps"]),
                "quality_ok": lerobot_result["quality"]["ok"],
            }
            log(
                f"  完成: {lerobot_elapsed:.2f}s "
                f"fps={lerobot_result['lerobot_fps']} "
                f"validate={lerobot_result['quality']['ok']}"
            )
        else:
            episode_report["lerobot"] = {
                "skipped": True,
                "lerobot_path": str(lerobot_dir),
            }

        episodes_report.append(episode_report)

    total_elapsed = time.perf_counter() - started
    return {
        "folder_name": folder_name,
        "source_dir": str(source_dir),
        "manifest_path": str(manifest_path),
        "preview_camera": manifest.preview_camera,
        "fps": normalize_api_fps(manifest.fps),
        "frame_count": manifest.frame_count,
        "episode_count": len(episodes_report),
        "output_dirs": {key: str(path) for key, path in dirs.items()},
        "episodes": episodes_report,
        "timing": {
            "mcap_split_sec": round(split_total, 3),
            "mcap_to_qrdf_sec": round(qrdf_total, 3),
            "qrdf_to_lerobot_sec": round(lerobot_total, 3),
            "total_sec": round(total_elapsed, 3),
        },
        "finished_at": format_api_datetime(datetime.utcnow()),
    }


def process_data_root(
    data_root: Path,
    *,
    output_root: Path,
    folder_names: list[str] | None = None,
    image_workers: int | None = None,
    fps: float | None = None,
    lerobot_version: str = "v3.0",
    generate_preview: bool = False,
    skip_mcap_split: bool = False,
    skip_qrdf: bool = False,
    skip_lerobot: bool = False,
    episode_limit: int | None = None,
    log: LogFn = _default_log,
) -> dict[str, Any]:
    folders = discover_source_folders(data_root)
    if folder_names:
        wanted = set(folder_names)
        folders = [folder for folder in folders if folder.name in wanted]
        missing = sorted(wanted - {folder.name for folder in folders})
        if missing:
            raise FileNotFoundError(f"未找到可处理目录: {', '.join(missing)}")

    if not folders:
        raise FileNotFoundError(f"在 {data_root} 下未找到含 atom_segments.json 的 MCAP 目录")

    output_root.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    folder_reports: list[dict[str, Any]] = []

    for folder in folders:
        folder_reports.append(
            process_source_folder(
                folder,
                output_root=output_root,
                image_workers=image_workers,
                fps=fps,
                lerobot_version=lerobot_version,
                generate_preview=generate_preview,
                skip_mcap_split=skip_mcap_split,
                skip_qrdf=skip_qrdf,
                skip_lerobot=skip_lerobot,
                episode_limit=episode_limit,
                log=log,
            )
        )

    total_elapsed = time.perf_counter() - started
    report = {
        "data_root": str(data_root),
        "output_root": str(output_root),
        "folder_count": len(folder_reports),
        "folders": folder_reports,
        "timing": {
            "total_sec": round(total_elapsed, 3),
            "mcap_split_sec": round(
                sum(item["timing"]["mcap_split_sec"] for item in folder_reports), 3
            ),
            "mcap_to_qrdf_sec": round(
                sum(item["timing"]["mcap_to_qrdf_sec"] for item in folder_reports), 3
            ),
            "qrdf_to_lerobot_sec": round(
                sum(item["timing"]["qrdf_to_lerobot_sec"] for item in folder_reports), 3
            ),
        },
        "finished_at": format_api_datetime(datetime.utcnow()),
    }

    report_path = output_root / "atom_segments_pipeline_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"\n报告已写入: {report_path}")
    return report
