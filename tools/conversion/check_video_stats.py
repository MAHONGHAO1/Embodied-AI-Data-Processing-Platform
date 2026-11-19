"""Prove the uint8 video-stat fix on a non-uniform synthetic array.

Run inside the isolated conversion environment:
    python tools/conversion/check_video_stats.py
"""

from __future__ import annotations

import json

import numpy as np

from lerobot_stats import patch_running_quantile_stats


def main() -> int:
    from lerobot.datasets.compute_stats import get_feature_stats
    batch = np.asarray([[[[0, 255]]], [[[255, 0]]]], dtype=np.uint8)
    before = get_feature_stats(batch, axis=(0, 2, 3), keepdims=True)
    active = patch_running_quantile_stats()
    after = get_feature_stats(batch, axis=(0, 2, 3), keepdims=True)
    print(json.dumps({
        "patched": active,
        "before_std": np.asarray(before["std"]).reshape(-1).tolist(),
        "after_std": np.asarray(after["std"]).reshape(-1).tolist(),
        "before_mean": np.asarray(before["mean"]).reshape(-1).tolist(),
        "after_mean": np.asarray(after["mean"]).reshape(-1).tolist(),
    }, ensure_ascii=False))
    return 0 if active and float(np.asarray(after["std"]).max()) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
