"""Probe a converted LeRobot v3.0 dataset through the official training loader.

This script must run inside the isolated conversion environment
(``tools/conversion/.venv``) because it imports PyTorch and LeRobot. It answers
one narrow question that a metadata check cannot:

    Can the delivered directory be loaded by ``lerobot.LeRobotDataset`` and
    consumed by a real ``torch.utils.data.DataLoader`` with finite, correctly
    shaped action/state/image tensors?

It does not train a policy, measure task success or claim GPU throughput.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# The probe is offline by construction: a missing file must fail, not download.
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

BOUNDARIES = [
    "This proves the delivered dataset is consumable by the official loader; it does not train a policy.",
    "Batch shapes are checked for this dataset's configured camera and 6-D state/action only.",
    "Throughput is a single local CPU measurement, not a GPU or production training benchmark.",
    "No task success, hardware synchronization or generalization claim is made.",
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def tensor_summary(tensor) -> dict:
    return {"shape": list(tensor.shape), "dtype": str(tensor.dtype),
            "finite": bool(tensor.isfinite().all()), "min": float(tensor.min()),
            "max": float(tensor.max()), "mean": float(tensor.mean())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path, help="converted v3.0 output directory")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--num-workers", type=int, default=0)
    parser.add_argument("--batches", type=int, default=4, help="batches to inspect; 0 means every batch")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    started = time.perf_counter()
    report = {
        "schema_version": 1,
        "probe": "lerobot-dataloader",
        "started_at_utc": now(),
        "dataset": str(args.dataset.resolve()),
        "loader": {"batch_size": args.batch_size, "num_workers": args.num_workers,
                   "shuffle": False, "drop_last": False},
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "processor": platform.processor(),
            "torch": None,
            "lerobot": None,
        },
        "boundaries": BOUNDARIES,
    }
    try:
        import numpy as np
        import torch
        from torch.utils.data import DataLoader
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
        import lerobot

        report["environment"]["torch"] = torch.__version__
        report["environment"]["lerobot"] = getattr(lerobot, "__version__", "unknown")

        manifest_path = args.dataset / "source_manifest.json"
        check(manifest_path.is_file(), f"missing source manifest: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        check(manifest.get("format_version") == "v3.0", "dataset is not LeRobot v3.0")
        check(manifest.get("official_validation_required") is True,
              "dataset manifest does not require official validation")
        report["provenance"] = {
            "repo_id": manifest["source"]["repo_id"],
            "revision": manifest["source"]["revision"],
            "episodes": [item["episode_index"] for item in manifest["episodes"]],
            "total_frames": manifest["total_frames"],
            "manifest_sha256": sha256(manifest_path),
        }

        load_started = time.perf_counter()
        dataset = LeRobotDataset(repo_id=manifest["source"]["repo_id"],
                                 root=args.dataset, video_backend="pyav")
        report["dataset_load_seconds"] = round(time.perf_counter() - load_started, 3)
        check(len(dataset) == manifest["total_frames"],
              f"loaded frames {len(dataset)} != manifest {manifest['total_frames']}")
        check(dataset.meta.total_episodes == manifest["total_episodes"],
              f"loaded episodes {dataset.meta.total_episodes} != manifest {manifest['total_episodes']}")
        report["loaded"] = {"frames": len(dataset), "episodes": dataset.meta.total_episodes,
                            "fps": dataset.fps,
                            "cameras": sorted(key for key in dataset.features
                                              if key.startswith("observation.images."))}
        check(len(report["loaded"]["cameras"]) == 1,
              f"expected exactly one camera, found {report['loaded']['cameras']}")

        loader = DataLoader(dataset, batch_size=args.batch_size, shuffle=False,
                            num_workers=args.num_workers, drop_last=False)
        reference_first = dataset[0]["action"].clone()
        reference_last = dataset[len(dataset) - 1]["action"].clone()
        batches, seen_episodes, first_action, last_action = [], set(), None, None
        decode_started = time.perf_counter()
        for number, batch in enumerate(loader):
            action, state = batch["action"], batch["observation.state"]
            check(action.dtype == torch.float32 and state.dtype == torch.float32,
                  "action/state must be float32")
            check(action.shape[1:] == (6,) and state.shape[1:] == (6,),
                  f"action/state shape {tuple(action.shape)} {tuple(state.shape)}")
            check(torch.isfinite(action).all() and torch.isfinite(state).all(),
                  "action/state contains non-finite values")
            image_key = report["loaded"]["cameras"][0]
            image = batch[image_key]
            check(image.dtype == torch.float32, f"image dtype is {image.dtype}, expected float32")
            check(image.ndim == 4 and image.shape[1] == 3 and image.shape[2:] == (480, 640),
                  f"image shape {tuple(image.shape)} is not [B, 3, 480, 640]")
            check(torch.isfinite(image).all(), "image contains non-finite values")
            check(float(image.min()) >= 0.0 and float(image.max()) <= 1.0,
                  f"image range [{float(image.min())}, {float(image.max())}] is outside [0, 1]")
            tasks = batch["task"]
            check(isinstance(tasks, list) and tasks and all(isinstance(item, str) and item for item in tasks),
                  "task batch is not a non-empty list of strings")
            if first_action is None:
                first_action = action[0].clone()
            last_action = action[-1].clone()
            seen_episodes.update(int(value) for value in batch["episode_index"].flatten().tolist())
            if args.batches <= 0 or number < args.batches:
                batches.append({
                    "index": number,
                    "batch_size": int(action.shape[0]),
                    "action": tensor_summary(action),
                    "state": tensor_summary(state),
                    "image": tensor_summary(image),
                    "episodes": sorted({int(value) for value in batch["episode_index"].flatten().tolist()}),
                })
        elapsed = time.perf_counter() - decode_started
        samples = min(len(dataset), args.batch_size * (len(batches) if args.batches > 0 else len(loader)))
        check(first_action is not None, "loader produced no batches")
        check(torch.equal(first_action, reference_first),
              "first shuffled-free batch action does not match dataset[0]")
        check(torch.equal(last_action, reference_last),
              "last batch action does not match dataset[-1]")
        report["batches"] = batches
        report["sampled"] = {"batches": len(batches), "samples": samples,
                             "episodes_seen": sorted(seen_episodes)}
        report["dataload_seconds"] = round(elapsed, 3)
        report["samples_per_second"] = round(samples / elapsed, 2) if elapsed > 0 else None
        report["checks"] = {
            "manifest_matches_loaded": True,
            "action_state_float32_6d_finite": True,
            "image_float32_3x480x640_unit_range": True,
            "task_is_text": True,
            "shuffle_free_order_preserved": True,
        }
        report["summary"] = {
            "passed": True,
            "frames": len(dataset),
            "episodes": dataset.meta.total_episodes,
            "batches_inspected": len(batches),
            "samples_inspected": samples,
            "samples_per_second": report["samples_per_second"],
            "scope": "official LeRobotDataset + torch DataLoader, local CPU, offline",
        }
    except Exception as exc:
        report["summary"] = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
        report["finished_at_utc"] = now()
        report["total_seconds"] = round(time.perf_counter() - started, 3)
        write_json(args.output, report)
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        return 1

    report["finished_at_utc"] = now()
    report["total_seconds"] = round(time.perf_counter() - started, 3)
    write_json(args.output, report)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"report: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
