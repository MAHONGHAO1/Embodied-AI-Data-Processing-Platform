from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
from lerobot.datasets.lerobot_dataset import LeRobotDataset

REPO_ID = "quicrobot/quictrain-pusht-smoke-v1"


def frame_image(episode: int, frame: int, frames_per_episode: int) -> np.ndarray:
    """Create a deterministic PushT-like observation without external assets."""
    image = np.full((96, 96, 3), 245, dtype=np.uint8)
    image[18:22, 24:72] = (190, 215, 235)
    image[18:62, 46:50] = (190, 215, 235)

    phase = frame / max(frames_per_episode - 1, 1)
    x = 8 + int(60 * phase)
    y = 68 - int(24 * phase) + episode
    image[y : y + 10, x : x + 10] = (35, 95, 210)
    return image


def generate(root: Path, episodes: int, frames_per_episode: int) -> dict[str, object]:
    dataset_root = root / "quictrain-pusht-smoke-v1"
    if dataset_root.exists() and any(dataset_root.iterdir()):
        raise FileExistsError(f"Refusing to overwrite non-empty dataset: {dataset_root}")

    dataset = LeRobotDataset.create(
        repo_id=REPO_ID,
        fps=10,
        root=dataset_root,
        robot_type="quictrain-synthetic-pusht",
        use_videos=False,
        features={
            "observation.image": {
                "dtype": "image",
                "shape": (96, 96, 3),
                "names": ["height", "width", "channel"],
            },
            "observation.state": {
                "dtype": "float32",
                "shape": (2,),
                "names": {"motors": ["x", "y"]},
            },
            "action": {
                "dtype": "float32",
                "shape": (2,),
                "names": {"motors": ["x", "y"]},
            },
        },
    )

    for episode in range(episodes):
        for frame in range(frames_per_episode):
            phase = frame / max(frames_per_episode - 1, 1)
            state = np.asarray(
                [phase, 0.5 + 0.25 * math.sin(phase * math.tau + episode)],
                dtype=np.float32,
            )
            action = np.asarray([min(1.0, phase + 1 / frames_per_episode), 0.5], dtype=np.float32)
            dataset.add_frame(
                {
                    "observation.image": frame_image(episode, frame, frames_per_episode),
                    "observation.state": state,
                    "action": action,
                    "task": "Push the synthetic block onto the T-shaped target.",
                }
            )
        dataset.save_episode()

    dataset.finalize()
    summary: dict[str, object] = {
        "repo_id": REPO_ID,
        "root": str(dataset_root),
        "episodes": episodes,
        "frames": episodes * frames_per_episode,
        "files": sum(1 for path in dataset_root.rglob("*") if path.is_file()),
    }
    (dataset_root / "QUICTRAIN_DATASET.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the QuicTrain LeRobot smoke dataset")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--episodes", type=int, default=4)
    parser.add_argument("--frames-per-episode", type=int, default=32)
    args = parser.parse_args()
    if args.episodes < 1 or args.frames_per_episode < 2:
        raise SystemExit("episodes must be positive and frames-per-episode must be at least 2")
    print(
        "QUICTRAIN_DATASET_READY="
        + json.dumps(
            generate(args.root, args.episodes, args.frames_per_episode), separators=(",", ":")
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
