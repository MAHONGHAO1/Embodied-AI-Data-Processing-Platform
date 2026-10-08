#!/usr/bin/env python3
"""Validate single MCAP -> QRDF -> LeRobot two-stage conversion pipeline and profile elapsed time."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT / "vendor" / "qrdf"))
sys.path.insert(0, str(BACKEND_ROOT))

from data.services.conversion import convert_mcap_to_qrdf, convert_qrdf_to_lerobot


def main() -> None:
    parser = argparse.ArgumentParser(description="MCAP → QRDF → LeRobot 两段转换验证")
    parser.add_argument(
        "mcap_path",
        nargs="?",
        default="/home/ubuntu/data/bag/20260528180553/20260528184104_20260528184141_10.mcap",
    )
    parser.add_argument("--output-root", type=str, default=None, help="输出根目录")
    parser.add_argument("--image-workers", type=int, default=None)
    parser.add_argument(
        "--fps", type=float, default=None, help="LeRobot 对齐帧率（默认从 QRDF 推断）"
    )
    parser.add_argument("--lerobot-version", type=str, default="v3.0")
    parser.add_argument(
        "--preview", action="store_true", help="生成与 LeRobot 同帧率的 preview.mp4"
    )
    args = parser.parse_args()

    mcap_path = Path(args.mcap_path).expanduser().resolve()
    if not mcap_path.is_file():
        raise SystemExit(f"MCAP 不存在: {mcap_path}")

    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_root = (
        Path(args.output_root)
        if args.output_root
        else (BACKEND_ROOT / "runtime" / "storage" / "hot" / f"pipeline_{mcap_path.stem}_{stamp}")
    )
    qrdf_dir = out_root / "qrdf"
    lerobot_dir = out_root / "lerobot"
    report_path = out_root / "pipeline_timing.json"

    if out_root.exists():
        shutil.rmtree(out_root)
    qrdf_dir.mkdir(parents=True)

    size_bytes = mcap_path.stat().st_size
    size_gb = size_bytes / (1024**3)
    img_workers = args.image_workers

    print(f"MCAP: {mcap_path.name} ({size_gb:.2f} GB)")
    print(f"输出: {out_root}")
    if img_workers:
        print(f"image_workers={img_workers}")

    print("\n[1/2] MCAP → QRDF ...")
    qrdf_result = convert_mcap_to_qrdf(
        mcap_path=str(mcap_path),
        output_dir=str(qrdf_dir),
        episode_id="episode_000010",
        task_name=mcap_path.stem,
        dataset_name=f"pipeline_{mcap_path.stem}",
        image_workers=img_workers,
        generate_preview=args.preview,
    )
    mcap_to_qrdf_sec = qrdf_result["timing"]["duration_sec"]
    import_timing = {
        k: qrdf_result["timing"].get(k)
        for k in ("state_pass_sec", "camera_pass_sec")
        if qrdf_result["timing"].get(k) is not None
    }
    print(
        f"  完成: {mcap_to_qrdf_sec:.2f}s "
        f"(state={import_timing.get('state_pass_sec')}s "
        f"camera={import_timing.get('camera_pass_sec')}s) "
        f"validate={qrdf_result['quality'].get('ok')}"
    )

    print("\n[2/2] QRDF → LeRobot ...")
    dataset_fps = args.fps
    if dataset_fps:
        print(f"  fps={dataset_fps}")
    lerobot_result = convert_qrdf_to_lerobot(
        storage_path=str(qrdf_dir),
        output_dir=str(lerobot_dir),
        fps=dataset_fps,
        lerobot_version=args.lerobot_version,
    )
    qrdf_to_lerobot_sec = lerobot_result["timing"]["duration_sec"]
    dataset_fps = lerobot_result["lerobot_fps"]
    if not args.fps:
        print(f"  fps={dataset_fps}")
    print(f"  完成: {qrdf_to_lerobot_sec:.2f}s validate={lerobot_result['quality']['ok']}")

    total_sec = mcap_to_qrdf_sec + qrdf_to_lerobot_sec
    report = {
        "mcap_path": str(mcap_path),
        "size_bytes": size_bytes,
        "size_gb": round(size_gb, 3),
        "output_root": str(out_root),
        "qrdf_path": str(qrdf_dir),
        "lerobot_path": str(lerobot_dir),
        "episode_path": qrdf_result.get("episode_path"),
        "image_workers": qrdf_result.get("image_workers"),
        "lerobot_fps": dataset_fps,
        "lerobot_version": args.lerobot_version,
        "timing": {
            "mcap_to_qrdf_sec": mcap_to_qrdf_sec,
            "mcap_to_qrdf_detail": import_timing,
            "qrdf_to_lerobot_sec": qrdf_to_lerobot_sec,
            "total_sec": round(total_sec, 3),
        },
        "quality": {
            "qrdf": qrdf_result["quality"],
            "lerobot": lerobot_result["quality"],
        },
        "finished_at": datetime.utcnow().isoformat(),
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n=== 耗时统计 ===")
    print(f"  MCAP → QRDF:      {mcap_to_qrdf_sec:.2f}s")
    print(f"  QRDF → LeRobot:   {qrdf_to_lerobot_sec:.2f}s")
    print(f"  合计:             {total_sec:.2f}s")
    print(f"报告: {report_path}")


if __name__ == "__main__":
    main()
