"""Run a reproducible check and inspect first/middle/last video positions.

Outputs local evidence, not a claim about hardware synchronization or training suitability.
"""
from pathlib import Path
import json
import sys
import time

import numpy as np
from PIL import Image, ImageDraw

from robodata.config import DEFAULT_DATA_ROOT, DEFAULT_REPORT_ROOT, EPISODES, PROJECT_ROOT
from robodata.dataset import load_episode, dataset_fingerprint
from robodata.quality import run_batch
from robodata.reports import write_report
from robodata.video import read_video_frame, inspect_video


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    started = time.perf_counter()
    frames = []
    observations = []
    for episode_id in EPISODES:
        episode = load_episode(DEFAULT_DATA_ROOT, episode_id)
        assert len(episode.table) == episode.expected_length
        video = inspect_video(episode.video_path)
        assert video["frame_count"] == len(episode.table)
        assert video["width"] == 640 and video["height"] == 480
        timestamps = np.asarray(episode.table["timestamp"], dtype=float)
        indices = np.asarray(episode.table["frame_index"], dtype=float)
        points = []
        for row in [0, len(episode.table) // 2, len(episode.table) - 1]:
            timestamp = float(timestamps[row])
            frame = read_video_frame(episode.video_path, timestamp)
            assert abs(frame["timestamp_error"]) <= 0.5 / episode.fps + 1e-4
            image = Image.fromarray(frame["image"])
            assert image.size == (640, 480)
            frames.append((episode_id, row, timestamp, image))
            points.append({"row": row, "timestamp": timestamp, "video_pts": frame["pts_time"], "error_seconds": frame["timestamp_error"]})
        observations.append({
            "episode_index": episode_id, "rows": len(episode.table),
            "decoded_frames": video["frame_count"], "codec": video.get("codec"),
            "nominal_timestamp_max_error": float(np.max(np.abs(timestamps - indices / episode.fps))),
            "points": points,
        })
    report = run_batch(DEFAULT_DATA_ROOT)
    paths = write_report(report, DEFAULT_REPORT_ROOT)
    repeat = run_batch(DEFAULT_DATA_ROOT)
    assert repeat["result_digest"] == report["result_digest"], "Repeated checks must agree"
    sheet = Image.new("RGB", (1200, 5 * 344 + 64), "#edf3f5")
    draw = ImageDraw.Draw(sheet)
    draw.text((20, 18), "SO-100 / episodes 0-4 / first, middle, last / public source: jmrog/so100_sweet_pick", fill="#152e3a")
    for item, (episode_id, row, timestamp, image) in enumerate(frames):
        x, y = (item % 3) * 400 + 8, (item // 3) * 344 + 64
        image.thumbnail((384, 288))
        sheet.paste(image, (x, y))
        draw.text((x + 6, y + 296), f"Episode {episode_id:02d} / row {row} / t={timestamp:.3f}s", fill="#152e3a")
    assets = PROJECT_ROOT / "docs" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    sheet.save(assets / "sample-contact-sheet.jpg", quality=90)
    evidence = {
        "source": report["source"], "input_fingerprint": dataset_fingerprint(DEFAULT_DATA_ROOT),
        "summary": report["summary"], "points": observations,
        "repeat_result_digest_equal": True, "result_digest": report["result_digest"],
        "elapsed_seconds": time.perf_counter() - started,
        "reports": {k: str(v) for k, v in paths.items()},
    }
    destination = DEFAULT_REPORT_ROOT / "verification.json"
    destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
