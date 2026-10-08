#!/usr/bin/env python3
"""Batch split MCAPs according to atom_segments.json and execute three-stage conversion: MCAP -> QRDF -> LeRobot."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent.parent
sys.path.insert(0, str(BACKEND_ROOT / "vendor" / "qrdf"))
sys.path.insert(0, str(BACKEND_ROOT))

from data.services.atom_segment_pipeline import process_data_root, process_source_folder


def main() -> None:
    parser = argparse.ArgumentParser(
        description="按 atom_segments.json 拆分 MCAP，并执行 QRDF / LeRobot 转换",
    )
    parser.add_argument(
        "--data-root",
        type=str,
        default=str(REPO_ROOT / "data"),
        help="原始 bag 数据根目录（默认: 仓库 data/）",
    )
    parser.add_argument(
        "--output-root",
        type=str,
        default=str(REPO_ROOT / "data"),
        help="三阶段输出根目录，将生成 MCAP/ QRDF/ LEROBOT/ 子目录",
    )
    parser.add_argument(
        "--folder",
        action="append",
        dest="folders",
        help="仅处理指定子目录名，可重复传入（默认处理全部）",
    )
    parser.add_argument("--image-workers", type=int, default=None, help="单 MCAP 图像转码线程数")
    parser.add_argument(
        "--fps", type=float, default=None, help="LeRobot 对齐帧率（默认使用 manifest.fps）"
    )
    parser.add_argument("--lerobot-version", type=str, default="v3.0")
    parser.add_argument(
        "--preview", action="store_true", help="生成与 LeRobot 同帧率的 preview.mp4"
    )
    parser.add_argument(
        "--episode-limit", type=int, default=None, help="每个目录仅处理前 N 个 episode"
    )
    parser.add_argument(
        "--skip-mcap-split", action="store_true", help="跳过 MCAP 拆分（要求输出已存在）"
    )
    parser.add_argument("--skip-qrdf", action="store_true", help="跳过 MCAP → QRDF")
    parser.add_argument("--skip-lerobot", action="store_true", help="跳过 QRDF → LeRobot")
    args = parser.parse_args()

    data_root = Path(args.data_root).expanduser().resolve()
    output_root = Path(args.output_root).expanduser().resolve()

    def log(message: str) -> None:
        line = f"[{datetime.utcnow().isoformat()}] {message}"
        print(line, flush=True)

    log(f"数据根目录: {data_root}")
    log(f"输出根目录: {output_root}")
    if args.folders:
        log(f"限定目录: {', '.join(args.folders)}")

    if args.folders and len(args.folders) == 1:
        source_dir = data_root / args.folders[0]
        report = process_source_folder(
            source_dir,
            output_root=output_root,
            image_workers=args.image_workers,
            fps=args.fps,
            lerobot_version=args.lerobot_version,
            generate_preview=args.preview,
            skip_mcap_split=args.skip_mcap_split,
            skip_qrdf=args.skip_qrdf,
            skip_lerobot=args.skip_lerobot,
            episode_limit=args.episode_limit,
            log=log,
        )
        report_path = output_root / f"{source_dir.name}_pipeline_report.json"
        import json

        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        log(f"报告已写入: {report_path}")
    else:
        process_data_root(
            data_root,
            output_root=output_root,
            folder_names=args.folders,
            image_workers=args.image_workers,
            fps=args.fps,
            lerobot_version=args.lerobot_version,
            generate_preview=args.preview,
            skip_mcap_split=args.skip_mcap_split,
            skip_qrdf=args.skip_qrdf,
            skip_lerobot=args.skip_lerobot,
            episode_limit=args.episode_limit,
            log=log,
        )


if __name__ == "__main__":
    main()
