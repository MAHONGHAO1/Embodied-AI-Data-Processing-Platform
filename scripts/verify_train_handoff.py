"""Train-side handoff check: read a dataset card, verify it, then load data.

This is deliberately not a training script. It demonstrates the only contract
the Train platform needs: verify ``dataset_card.json``, check the quality
verdict, then load the LeRobot dataset and confirm counts.

Run inside the conversion environment:
    python scripts/verify_train_handoff.py --archive work/.../delivery.zip
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
import zipfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from robodata.dataset_card import load_card, verify_card  # noqa: E402


def _safe_extract(archive: Path, target: Path) -> None:
    with zipfile.ZipFile(archive) as stream:
        for entry in stream.infolist():
            name = entry.filename
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name:
                raise ValueError(f"ZIP 包含不安全路径：{name}")
            if entry.is_dir():
                continue
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError(f"ZIP 包含符号链接：{name}")
            destination = target / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            with stream.open(entry) as source, destination.open("wb") as sink:
                shutil.copyfileobj(source, sink)


def _load_dataset(dataset_root: Path, card: dict) -> dict:
    try:
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
    except ModuleNotFoundError as exc:
        raise ValueError("当前 Python 没有 lerobot；请用 tools/conversion 环境运行该脚本") from exc
    dataset = LeRobotDataset(repo_id="local/train-handoff", root=dataset_root, video_backend="pyav")
    expected_rows = int(card["content"]["frame_count"])
    expected_episodes = int(card["content"]["episode_count"])
    if len(dataset) != expected_rows:
        raise ValueError(f"官方加载器帧数 {len(dataset)} 与数据卡 {expected_rows} 不一致")
    if dataset.meta.total_episodes != expected_episodes:
        raise ValueError(f"官方加载器 Episode 数 {dataset.meta.total_episodes} 与数据卡 {expected_episodes} 不一致")
    first = dataset[0]
    cameras = card["content"].get("cameras") or []
    for camera in cameras:
        if camera not in first:
            raise ValueError(f"加载样本缺少数据卡声明的相机：{camera}")
    return {"rows": len(dataset), "episodes": dataset.meta.total_episodes,
            "cameras_checked": cameras, "tasks": card["content"].get("tasks", [])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="交付 ZIP")
    parser.add_argument("--dataset", type=Path, help="已解压的 dataset 目录")
    parser.add_argument("--card", type=Path, help="数据卡；默认从 dataset/dataset_card.json 读取")
    parser.add_argument("--workdir", type=Path, help="解压临时目录")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    if bool(args.archive) == bool(args.dataset):
        parser.error("必须且只能指定 --archive 或 --dataset 之一")
    work = None
    try:
        if args.archive:
            work = Path(args.workdir or tempfile.mkdtemp(prefix="robodata-handoff-"))
            work.mkdir(parents=True, exist_ok=True)
            _safe_extract(args.archive.resolve(), work)
            dataset_root = work / "dataset"
            card_path = args.card or dataset_root / "dataset_card.json"
        else:
            dataset_root = args.dataset.resolve()
            card_path = args.card or dataset_root.parent / "dataset_card.json"
            if args.card is None and not card_path.is_file():
                card_path = dataset_root / "dataset_card.json"
        if not card_path.is_file():
            raise ValueError(f"找不到数据卡：{card_path}")
        card = load_card(card_path)
        valid, message = verify_card(card)
        if not valid:
            raise ValueError(f"数据卡自校验失败：{message}")
        quality = card.get("quality") or {}
        if (quality.get("final_quality") or {}).get("outcome") != "passed":
            raise ValueError("数据卡记录最终质检未通过")
        if (quality.get("official_verify") or {}).get("passed") is not True:
            raise ValueError("数据卡记录官方加载器验证未通过")
        loaded = _load_dataset(dataset_root, card)
        result = {"status": "passed", "card_valid": True, "card_digest": card.get("card_digest"),
                  "dataset_id": card.get("dataset_id"), "version": card.get("version"),
                  "source_kind": (card.get("source") or {}).get("kind"),
                  "content": card.get("content"), "loaded": loaded,
                  "limitations": card.get("limitations", [])}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "message": str(exc)}, ensure_ascii=False, indent=2),
              file=sys.stderr)
        return 1
    finally:
        if work is not None and args.workdir is None:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
