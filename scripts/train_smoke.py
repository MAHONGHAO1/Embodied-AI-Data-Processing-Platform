"""Run a 100-step linear action probe over robot demonstration rows.

This is a *training-input smoke test*, not a policy and not evidence of task
success. It answers one narrow question: can the exported observation/action
arrays drive a finite, decreasing optimization loop on CPU?

For a real LeRobot dataset, pass ``--dataset-root``. Without it, the script
generates a deterministic synthetic table so the loop can be checked in CI.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robodata.dataset import load_episode


def synthetic_rows(seed: int = 7, rows: int = 2048) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    state = rng.normal(size=(rows, 6))
    weights = np.array(
        [
            [0.8, -0.2, 0.1, 0.0, 0.3, -0.1],
            [0.1, 0.7, -0.3, 0.2, 0.0, 0.1],
            [-0.2, 0.1, 0.9, -0.1, 0.2, 0.0],
            [0.0, 0.3, 0.1, 0.6, -0.2, 0.2],
            [0.2, -0.1, 0.0, 0.1, 0.8, -0.3],
            [-0.1, 0.0, 0.2, -0.2, 0.1, 0.7],
        ]
    )
    bias = np.array([0.05, -0.03, 0.02, 0.0, 0.04, -0.01])
    action = state @ weights + bias + rng.normal(scale=0.01, size=(rows, 6))
    return state.astype(np.float32), action.astype(np.float32)


def load_dataset_rows(dataset_root: Path, max_rows: int = 100_000) -> tuple[np.ndarray, np.ndarray, dict]:
    episode = load_episode(dataset_root, 0)
    table = episode.table
    if "observation.state" not in table or "action" not in table:
        raise SystemExit("数据集缺少 observation.state 或 action 字段")
    if len(table) > max_rows:
        table = table.iloc[:max_rows]
    state = np.stack([np.asarray(value, dtype=np.float32) for value in table["observation.state"]])
    action = np.stack([np.asarray(value, dtype=np.float32) for value in table["action"]])
    return state, action, {
        "kind": "lerobot_dataset",
        "dataset_root": str(dataset_root),
        "episode_index": episode.episode_index,
        "rows": int(len(state)),
        "source": {key: episode.source.get(key) for key in ("repo_id", "revision", "format_version")},
    }


def train_linear_probe(
    state: np.ndarray,
    action: np.ndarray,
    *,
    steps: int = 100,
    batch_size: int = 128,
    learning_rate: float = 0.05,
    seed: int = 11,
) -> dict:
    if state.ndim != 2 or action.ndim != 2 or len(state) != len(action):
        raise SystemExit("state/action 必须是行数一致的一维特征表")
    if len(state) < 2:
        raise SystemExit("至少需要 2 行数据")

    mean = state.mean(axis=0, keepdims=True)
    std = state.std(axis=0, keepdims=True)
    std[std == 0] = 1.0
    features = np.concatenate([np.ones((len(state), 1)), (state - mean) / std], axis=1)
    targets = action.astype(np.float64)
    theta = np.zeros((features.shape[1], targets.shape[1]), dtype=np.float64)
    rng = np.random.default_rng(seed)

    def mse(indices: np.ndarray) -> float:
        residual = features[indices] @ theta - targets[indices]
        return float(np.mean(residual * residual))

    all_indices = np.arange(len(state))
    initial_loss = mse(all_indices)
    started = time.perf_counter()
    for _ in range(steps):
        indices = rng.choice(all_indices, size=min(batch_size, len(state)), replace=False)
        residual = features[indices] @ theta - targets[indices]
        gradient = features[indices].T @ residual / len(indices)
        theta -= learning_rate * gradient
    elapsed = time.perf_counter() - started
    final_loss = mse(all_indices)

    return {
        "steps": steps,
        "batch_size": batch_size,
        "learning_rate": learning_rate,
        "rows": int(len(state)),
        "input_dim": int(state.shape[1]),
        "output_dim": int(action.shape[1]),
        "initial_mse": round(initial_loss, 8),
        "final_mse": round(final_loss, 8),
        "loss_ratio": round(final_loss / initial_loss, 6) if initial_loss else None,
        "finite": bool(np.isfinite(theta).all() and np.isfinite(final_loss)),
        "parameter_delta": round(float(np.abs(theta).sum()), 8),
        "seconds": round(elapsed, 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, help="LeRobot dataset root; omit for synthetic rows")
    parser.add_argument("--synthetic", action="store_true", help="Explicitly use deterministic synthetic rows")
    parser.add_argument("--steps", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--learning-rate", type=float, default=0.05)
    parser.add_argument("--output", type=Path, help="Optional JSON output path")
    args = parser.parse_args()

    if args.steps < 1 or args.batch_size < 1 or args.learning_rate <= 0:
        parser.error("steps, batch size and learning rate must be positive")
    if args.dataset_root:
        state, action, dataset = load_dataset_rows(args.dataset_root)
    else:
        state, action = synthetic_rows()
        dataset = {"kind": "synthetic_linear_relation", "rows": int(len(state)), "seed": 7}

    result = train_linear_probe(
        state,
        action,
        steps=args.steps,
        batch_size=args.batch_size,
        learning_rate=args.learning_rate,
    )
    report = {
        "benchmark": "100-step linear action probe",
        "scope": "training-input smoke only; not a policy, not task success, not hardware validation",
        "dataset": dataset,
        "result": result,
        "interpretation": (
            "A finite decreasing loss shows the exported arrays can drive an optimization loop. "
            "It does not show that a robot policy learns or that the task succeeds."
        ),
    }
    encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
