"""Synthetic compatibility gate. These frames are not real robot samples."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time


CAMERA = "observation.images.test_camera"
LENGTHS = (8, 10)
FPS = 20
REPO_ID = "local/synthetic-compatibility-probe"


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def original_frame(episode: int, frame: int) -> dict:
    import numpy as np

    yy, xx = np.indices((64, 64))
    image = np.stack(
        [np.full((64, 64), episode * 80 + frame * 10), xx * 3, yy * 3], axis=-1
    ).astype(np.uint8)
    return {
        "task": f"Synthetic compatibility probe episode {episode}; not robot data",
        "observation.state": np.array([episode, frame, frame / FPS], dtype=np.float32),
        "action": np.array([frame / 10, -frame / 10], dtype=np.float32),
        CAMERA: image,
    }


def create_dataset(output: Path) -> None:
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    features = {
        "observation.state": {
            "dtype": "float32", "shape": (3,), "names": ["episode", "frame", "time"]
        },
        "action": {"dtype": "float32", "shape": (2,), "names": ["positive", "negative"]},
        CAMERA: {"dtype": "video", "shape": (64, 64, 3), "names": ["height", "width", "channels"]},
    }
    dataset = LeRobotDataset.create(
        repo_id=REPO_ID,
        root=output / "dataset",
        fps=FPS,
        features=features,
        robot_type="synthetic-compatibility-probe-not-a-robot",
        video_backend="pyav",
        vcodec="h264",
        image_writer_processes=0,
        image_writer_threads=0,
        batch_encoding_size=1,
    )
    try:
        for episode, length in enumerate(LENGTHS):
            for frame in range(length):
                dataset.add_frame(original_frame(episode, frame))
            dataset.save_episode(parallel_encoding=False)
    finally:
        dataset.finalize()
    write_json(output / "create.json", {"episodes": len(LENGTHS), "frames": sum(LENGTHS)})


def validate_dataset(output: Path) -> None:
    import numpy as np
    import torch
    from torch.utils.data import DataLoader
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    if os.environ.get("HF_HUB_OFFLINE") != "1":
        raise RuntimeError("Compatibility validation requires offline mode")
    dataset = LeRobotDataset(repo_id=REPO_ID, root=output / "dataset", video_backend="pyav")
    assert len(dataset) == sum(LENGTHS), (len(dataset), sum(LENGTHS))
    assert dataset.meta.total_episodes == len(LENGTHS)
    assert dataset.meta.info["codebase_version"] == "v3.0"
    samples = []
    start = 0
    for episode, length in enumerate(LENGTHS):
        for frame in sorted({0, length // 2, length - 1}):
            item = dataset[start + frame]
            expected = original_frame(episode, frame)
            np.testing.assert_allclose(item["observation.state"].numpy(), expected["observation.state"], atol=1e-6)
            np.testing.assert_allclose(item["action"].numpy(), expected["action"], atol=1e-6)
            assert int(item["episode_index"]) == episode
            assert int(item["frame_index"]) == frame
            assert int(item["index"]) == start + frame
            assert abs(float(item["timestamp"]) - frame / FPS) < 1e-5
            assert item["task"] == expected["task"]
            image = item[CAMERA]
            assert image.dtype == torch.float32 and tuple(image.shape) == (3, 64, 64)
            assert 0.0 <= float(image.min()) <= float(image.max()) <= 1.0
            decoded = image.permute(1, 2, 0).numpy() * 255.0
            mean_error = float(np.abs(decoded - expected[CAMERA].astype(np.float32)).mean())
            # H.264 is lossy. The distinctive probe colors also test episode video offsets.
            assert mean_error <= 8.0, (episode, frame, mean_error)
            samples.append({"episode": episode, "frame": frame, "global_index": start + frame, "image_mean_absolute_error": mean_error})
        start += length
    batch = next(iter(DataLoader(dataset, batch_size=2, num_workers=0, shuffle=False)))
    assert tuple(batch["observation.state"].shape) == (2, 3)
    assert tuple(batch["action"].shape) == (2, 2)
    assert tuple(batch[CAMERA].shape) == (2, 3, 64, 64)
    write_json(output / "validate.json", {
        "offline": True,
        "official_dataset_class": "lerobot.datasets.lerobot_dataset.LeRobotDataset",
        "format_version": dataset.meta.info["codebase_version"],
        "video_backend": dataset.video_backend,
        "sample_checks": samples,
        "batch_size": 2,
        "batch_image_shape": list(batch[CAMERA].shape),
        "cuda_available": torch.cuda.is_available(),
    })


def run_gate(output: Path) -> int:
    output.mkdir(parents=True, exist_ok=False)
    record = {
        "purpose": "合成兼容探针；不是机器人真实样本，不属于作品集真实数据验收",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version,
        "executable": sys.executable,
        "packages": {name: importlib.metadata.version(name) for name in ["lerobot", "torch", "torchvision", "av", "h5py", "numpy", "datasets"]},
        "status": "running",
        "child_environment": {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1"},
        "phases": [],
    }
    write_json(output / "compatibility.json", record)
    env = os.environ.copy()
    env.update({"HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "WANDB_MODE": "disabled", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    for phase in ["create", "validate"]:
        started = time.monotonic()
        try:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--output", str(output), "--phase", phase],
                env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
            )
        except subprocess.TimeoutExpired as exc:
            for stream, content in [("stdout", exc.stdout), ("stderr", exc.stderr)]:
                if isinstance(content, bytes):
                    content = content.decode("utf-8", errors="replace")
                (output / f"{phase}.{stream}.log").write_text(content or "", encoding="utf-8")
            record["status"] = "failed"
            record["phases"].append({"name": phase, "error": "进程超过 180 秒，已终止", "duration_seconds": round(time.monotonic() - started, 3)})
            write_json(output / "compatibility.json", record)
            print(json.dumps(record, ensure_ascii=False))
            return 1
        (output / f"{phase}.stdout.log").write_text(result.stdout, encoding="utf-8")
        (output / f"{phase}.stderr.log").write_text(result.stderr, encoding="utf-8")
        record["phases"].append({"name": phase, "returncode": result.returncode, "duration_seconds": round(time.monotonic() - started, 3)})
        if result.returncode != 0:
            record["status"] = "failed"
            write_json(output / "compatibility.json", record)
            print(json.dumps(record, ensure_ascii=False))
            print(result.stderr, file=sys.stderr)
            return 1
    record["status"] = "passed"
    record["validation"] = json.loads((output / "validate.json").read_text(encoding="utf-8"))
    record["completed_at"] = datetime.now(timezone.utc).isoformat()
    write_json(output / "compatibility.json", record)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase", choices=["create", "validate"])
    args = parser.parse_args()
    output = args.output.resolve()
    if args.phase == "create":
        create_dataset(output)
    elif args.phase == "validate":
        validate_dataset(output)
    else:
        return run_gate(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
