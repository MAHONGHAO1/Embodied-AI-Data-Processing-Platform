"""Prove episode-level preview resume without a real interruption.

Creates a two-episode synthetic Matching Cable fixture, pre-encodes episode 68
as a completed unit, then runs the real preview. Episode 68 must be reused and
only episode 70 encoded; the final preview must still contain both episodes.

Run with the isolated conversion environment:
    python scripts/verify_preview_resume.py --output work/preview-resume
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

sys.path.insert(0, str(PROJECT_ROOT / "tools" / "conversion"))

from matching_preview import (_encode_episode, _frame_records, _records_digest,  # noqa: E402
                              build_preview, inspect_frame_capabilities, inspect_shapes)
from robodata.matching import discover_episodes  # noqa: E402

FIXTURE_CODE = r'''
import io
import sys
from pathlib import Path

import h5py
import numpy as np
from PIL import Image

root = Path(sys.argv[1])
vlen = h5py.vlen_dtype(np.dtype("uint8"))
root.mkdir(parents=True)
for episode_id in (68, 70):
    episode = root / str(episode_id)
    episode.mkdir(parents=True)
    for frame_index in range(2):
        image = io.BytesIO()
        Image.new("RGB", (32, 24), (40 + frame_index * 20, 90 + (episode_id - 68), 140)).save(image, format="JPEG")
        with h5py.File(episode / f"{frame_index}.hdf5", "w") as handle:
            handle.create_dataset("master/joint_position", data=np.arange(16, dtype=np.float32))
            handle.create_dataset("puppet/joint_position", data=np.arange(16, dtype=np.float32) + 1)
            handle.create_dataset("timestamps", data=np.array([frame_index * 50], dtype=np.int32))
            dataset = handle.create_dataset("observations/rgb_images/camera_0", (1,), dtype=vlen)
            dataset[0] = np.frombuffer(image.getvalue(), dtype=np.uint8)
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "work" / "preview-resume")
    args = parser.parse_args()
    work = args.output.resolve()
    if work.exists():
        shutil.rmtree(work)
    fixture = work / "fixture"

    from robodata.conversion import CONVERSION_PYTHON
    import subprocess
    environment = {**dict(__import__("os").environ), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    subprocess.run([str(CONVERSION_PYTHON), "-c", FIXTURE_CODE, str(fixture)],
                   check=True, cwd=PROJECT_ROOT, env=environment)

    episodes = discover_episodes(fixture, None)
    capabilities = inspect_frame_capabilities(episodes[0]["frames"][0])
    cameras = capabilities["cameras"]
    shapes = inspect_shapes(episodes[0]["frames"][0], cameras)
    output = work / "preview"
    units = output.with_name(output.name + ".units")
    units.mkdir(parents=True)
    from robodata.journal import UnitJournal, artifact_record
    journal = UnitJournal(units / "journal.jsonl", root=units)
    first = episodes[0]
    source_id = int(first["source_episode_id"])
    frames = [Path(frame) for frame in first["frames"]]
    records = _frame_records(fixture, frames, source_id)
    digest = _records_digest(records)
    episode_dir = units / f"episode_{source_id:06d}"
    _encode_episode(episode_dir, fixture, frames, records, source_id, capabilities, cameras, shapes)
    meta = {"episode_index": int(first["episode_index"]), "source_episode_id": source_id,
            "length": len(frames)}
    journal.complete(f"preview:episode:{source_id}", digest,
                     [artifact_record(episode_dir, root=units)], meta=meta)

    manifest = build_preview(fixture, output)
    result = {
        "status": "passed",
        "reused_episodes": manifest["resume"]["reused_episodes"],
        "encoded_episodes": manifest["resume"]["encoded_episodes"],
        "episodes": manifest["total_episodes"],
        "frames": manifest["total_frames"],
        "videos": [str(path.relative_to(output).as_posix())
                   for path in sorted((output / "videos").rglob("*.mp4"))],
        "video_hashes": {path.relative_to(output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in sorted((output / "videos").rglob("*.mp4"))},
    }
    if result["reused_episodes"] != 1 or result["encoded_episodes"] != 1 or result["episodes"] != 2:
        raise SystemExit(f"resume assertion failed: {result}")
    if len(result["videos"]) != 2:
        raise SystemExit(f"expected two videos: {result['videos']}")
    (work / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
