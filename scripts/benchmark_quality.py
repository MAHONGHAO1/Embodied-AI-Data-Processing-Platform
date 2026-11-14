"""Reproducible scale experiment for the existing table-only quality checks.

This measures robodata.quality.run_checks(check_video=False) over deterministic
synthetic Episode tables, with an optional locally available real dataset sample.
It is not an end-to-end conversion or production-capacity benchmark. Synthetic
data construction is excluded from the timed region.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robodata.dataset import load_episode
from robodata.models import Episode
from robodata.quality import run_checks


def make_episode(rows: int) -> Episode:
    frames = np.arange(rows, dtype=np.int64)
    table = pd.DataFrame({
        "episode_index": np.zeros(rows, dtype=np.int64),
        "frame_index": frames,
        "timestamp": frames.astype(np.float64) / 30.0,
        "observation.state": [np.zeros(6, dtype=np.float32) for _ in range(rows)],
        "action": [np.ones(6, dtype=np.float32) for _ in range(rows)],
    })
    return Episode(
        episode_index=0,
        table=table,
        metadata={"info": {"features": {
            "observation.state": {"shape": [6]}, "action": {"shape": [6]},
        }}},
        video_path=ROOT / "__benchmark_video_not_read__.mp4",
        data_path=ROOT / "__synthetic_benchmark_table__.parquet",
        field_names=[f"joint_{index}" for index in range(6)],
        fps=30.0,
        source={"source_kind": "synthetic_benchmark"},
        expected_length=rows,
    )


def measure(rows: int, repeats: int) -> dict:
    episode = make_episode(rows)
    timings = []
    issue_counts = []
    for _ in range(repeats):
        started = time.perf_counter()
        result = run_checks(episode, check_video=False)
        timings.append(time.perf_counter() - started)
        issue_counts.append(sum(item["category"] == "data_issue" for item in result["issues"]))
        if result["status"] != "incomplete" or issue_counts[-1] != 0:
            raise RuntimeError(f"unexpected result at {rows} rows: {result['status']} {result['issues'][:2]}")
    median_seconds = statistics.median(timings)
    return {
        "rows": rows,
        "repeats": repeats,
        "median_seconds": round(median_seconds, 6),
        "min_seconds": round(min(timings), 6),
        "max_seconds": round(max(timings), 6),
        "rows_per_second": round(rows / median_seconds, 2),
        "data_issues": issue_counts,
        "video_checks": "not run",
    }


def measure_real_sample(root: Path, repeats: int) -> dict:
    started = time.perf_counter()
    episode = load_episode(root, 0)
    load_seconds = time.perf_counter() - started
    timings = []
    issue_counts = []
    for _ in range(repeats):
        started = time.perf_counter()
        result = run_checks(episode, check_video=False)
        timings.append(time.perf_counter() - started)
        issue_counts.append(sum(item["category"] == "data_issue" for item in result["issues"]))
        if result["status"] != "incomplete" or issue_counts[-1] != 0:
            raise RuntimeError(f"unexpected result for source Episode 0: {result['status']} {result['issues'][:2]}")
    median_seconds = statistics.median(timings)
    digest = hashlib.sha256()
    with episode.data_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "source": {key: episode.source.get(key) for key in ("repo_id", "revision", "format_version")},
        "episode_index": episode.episode_index,
        "rows": len(episode.table),
        "fps": episode.fps,
        "parquet_file": episode.data_path.name,
        "parquet_sha256": digest.hexdigest(),
        "load_episode_seconds_single_run": round(load_seconds, 6),
        "table_check_repeats": repeats,
        "table_check_median_seconds": round(median_seconds, 6),
        "table_rows_per_second": round(len(episode.table) / median_seconds, 2),
        "data_issues": issue_counts,
        "video_checks": "not run",
        "interpretation": "One real fixed-sample episode; loading time is a single observation, not a stable IO benchmark.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", default="1000,5000,10000", help="Comma-separated synthetic table sizes")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--dataset-root", type=Path, help="Optional local LeRobot dataset root; checks Episode 0 once loaded")
    parser.add_argument("--output", type=Path, help="Optional JSON output path")
    args = parser.parse_args()
    row_counts = [int(value) for value in args.rows.split(",")]
    if args.repeats < 1 or not row_counts or any(rows < 1 for rows in row_counts):
        parser.error("rows and repeats must be positive")
    report = {
        "benchmark": "robodata.quality.run_checks table-only scale experiment",
        "scope": "synthetic fixed-width Episode records; check_video=False; excludes fixture construction, Parquet IO, video decoding, conversion and packaging",
        "seed": "deterministic zero/one vectors; no random values",
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processor": platform.processor() or "unreported",
            "cpu_count": os.cpu_count(),
            "pandas": pd.__version__,
            "numpy": np.__version__,
        },
        "results": [measure(rows, args.repeats) for rows in row_counts],
        "real_sample": measure_real_sample(args.dataset_root, args.repeats) if args.dataset_root else None,
        "interpretation": "Current table-check path only. Does not establish full-pipeline capacity or a before/after speedup.",
    }
    encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
