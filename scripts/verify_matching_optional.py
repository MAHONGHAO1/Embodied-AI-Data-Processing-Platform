"""Verify Matching Cable optional-stream handling without real 9 GB data.

Creates two synthetic episodes with one RGB camera and no depth, tactile or
optional numeric fields, then runs the real preview and conversion workers in
the isolated conversion environment. This is the regression fixture for the
"data is incomplete but still usable" path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from robodata.conversion import CONVERSION_PYTHON  # noqa: E402


FIXTURE_CODE = r'''
import io
import sys
from pathlib import Path
import h5py
import numpy as np
from PIL import Image

root = Path(sys.argv[1])
vlen = h5py.vlen_dtype(np.dtype("uint8"))
for episode_id in (68, 70):
    episode = root / str(episode_id)
    episode.mkdir(parents=True)
    for frame_index in range(2):
        image = io.BytesIO()
        Image.new("RGB", (32, 24), (40 + frame_index * 20, 90, 140)).save(image, format="JPEG")
        with h5py.File(episode / f"{frame_index}.hdf5", "w") as handle:
            handle.create_dataset("master/joint_position", data=np.arange(16, dtype=np.float32))
            handle.create_dataset("puppet/joint_position", data=np.arange(16, dtype=np.float32) + 1)
            handle.create_dataset("timestamps", data=np.array([frame_index * 50], dtype=np.int32))
            dataset = handle.create_dataset("observations/rgb_images/camera_0", (1,), dtype=vlen)
            dataset[0] = np.frombuffer(image.getvalue(), dtype=np.uint8)
'''


def run(command: list[str], env: dict[str, str]) -> None:
    subprocess.run(command, cwd=PROJECT_ROOT, env=env, check=True)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "work/matching-optional")
    args = parser.parse_args()
    if not CONVERSION_PYTHON.is_file():
        print(f"缺少独立转换环境：{CONVERSION_PYTHON}", file=sys.stderr)
        return 1

    work = args.output.resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    fixture = work / "fixture"
    preview = work / "preview"
    dataset = work / "dataset"
    artifacts = work / "artifacts"
    env = os.environ.copy()
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", HF_HUB_OFFLINE="1", HF_DATASETS_OFFLINE="1")
    python = str(CONVERSION_PYTHON)

    run([python, "-c", FIXTURE_CODE, str(fixture)], env)
    run([python, str(PROJECT_ROOT / "tools/conversion/matching_preview.py"),
         "--input", str(fixture), "--output", str(preview)], env)
    manifest = json.loads((preview / "source_manifest.json").read_text(encoding="utf-8"))
    source = manifest["source"]
    expected_camera = "observation.images.camera_0"
    assert source["cameras"] == [expected_camera], source["cameras"]
    assert len(source["missing_cameras"]) == 3, source["missing_cameras"]
    assert source["depth_source_cameras"] == [], source["depth_source_cameras"]
    assert len(source["missing_depth_source_cameras"]) == 2, source["missing_depth_source_cameras"]
    assert all(value == "not_provided" for value in source["optional_fields"].values()), source["optional_fields"]
    assert source["tactile"]["status"] == "not_provided", source["tactile"]
    reuse = manifest["reuse"]
    assert reuse["schema_version"] == 1 and reuse["layout"] == "one_video_per_episode", reuse
    assert len(reuse["episodes"]) == 2, reuse
    preview_videos = sorted((preview / "videos").rglob("*.mp4"))
    assert len(preview_videos) == 2, preview_videos
    for item in reuse["episodes"]:
        video = item["videos"][expected_camera]
        assert video["from_timestamp"] == 0.0 and video["frame_count"] == item["length"], video
        assert (preview / video["path"]).is_file(), video["path"]

    spec = {
        "snapshot_id": "synthetic-matching-optional",
        "episodes": [
            {"source_episode_index": index, "output_episode_index": index, "row_count": 2,
             "annotation": {"task": "synthetic matching pick"}}
            for index in range(2)
        ],
    }
    spec_path = work / "spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

    stages = []

    def run_conversion(preview_root: Path, dataset_root: Path, artifact_root: Path, spec_file: Path) -> None:
        for stage in ("file_validation", "matching_validation", "field_mapping", "format_write", "official_verify"):
            run([python, str(PROJECT_ROOT / "tools/conversion/matching_worker.py"),
                 "--stage", stage, "--input", str(preview_root), "--output", str(dataset_root),
                 "--artifacts", str(artifact_root), "--spec", str(spec_file)], env)
            stages.append(stage)
        run([python, str(PROJECT_ROOT / "tools/conversion/matching_worker.py"),
             "--stage", "archive_verify", "--output", str(dataset_root), "--artifacts", str(artifact_root)], env)
        stages.append("archive_verify")

    run_conversion(preview, dataset, artifacts, spec_path)

    format_write = json.loads((artifacts / "steps/format_write.json").read_text(encoding="utf-8"))["details"]
    assert format_write["strategy"] == "preview_video_reuse", format_write
    assert format_write["source_numeric_consistency_rechecked"] is True, format_write
    assert format_write["preview_video_hashes_verified"] is True, format_write
    assert format_write["preview_data_hashes_verified"] is True, format_write
    assert format_write["video_files_reused"] == 2 and format_write["fallback_reason"] is None, format_write
    assert len(list((dataset / "data").rglob("*.parquet"))) == 2, "one parquet per episode"
    output_videos = sorted((dataset / "videos").rglob("*.mp4"))
    assert len(output_videos) == 2, output_videos
    for item in reuse["episodes"]:
        output_video = dataset / item["videos"][expected_camera]["path"]
        assert hashlib.sha256(output_video.read_bytes()).hexdigest() == item["videos"][expected_camera]["sha256"]

    official = json.loads((artifacts / "steps/official_verify.json").read_text(encoding="utf-8"))["details"]
    assert official["output_loader_consistency_checked"] is True, official
    assert official["source_numeric_consistency_rechecked"] is False, official
    assert official["cameras"] == [expected_camera], official
    assert official["sample_count"] == 6, official
    assert official["conversion_strategy"]["name"] == "preview_video_reuse", official
    archive = json.loads((artifacts / "steps/archive_verify.json").read_text(encoding="utf-8"))["details"]
    assert archive["output_loader_consistency_checked"] is True, archive
    assert archive["source_numeric_consistency_rechecked"] is False, archive

    subset_spec = {
        "snapshot_id": "synthetic-matching-subset",
        "episodes": [{"source_episode_index": 1, "output_episode_index": 0, "row_count": 2,
                      "annotation": {"task": "synthetic matching subset"}}],
    }
    subset_spec_path = work / "spec-subset.json"
    subset_spec_path.write_text(json.dumps(subset_spec, ensure_ascii=False), encoding="utf-8")
    subset_dataset, subset_artifacts = work / "dataset-subset", work / "artifacts-subset"
    run_conversion(preview, subset_dataset, subset_artifacts, subset_spec_path)
    import pandas as pd
    subset = pd.concat([pd.read_parquet(path) for path in sorted((subset_dataset / "data").rglob("*.parquet"))],
                       ignore_index=True)
    assert subset["episode_index"].tolist() == [0, 0], subset["episode_index"].tolist()
    assert subset["index"].tolist() == [0, 1], subset["index"].tolist()
    assert subset["frame_index"].tolist() == [0, 1], subset["frame_index"].tolist()
    assert subset["task_index"].tolist() == [0, 0], subset["task_index"].tolist()
    assert len(list((subset_dataset / "videos").rglob("*.mp4"))) == 1
    subset_details = json.loads((subset_artifacts / "steps/format_write.json").read_text(encoding="utf-8"))["details"]
    assert subset_details["video_files_reused"] == 1, subset_details

    tampered = work / "preview-tampered"
    shutil.copytree(preview, tampered)
    tampered_video = next((tampered / "videos").rglob("*.mp4"))
    with tampered_video.open("ab") as stream:
        stream.write(b"tampered")
    fallback_dataset, fallback_artifacts = work / "dataset-fallback", work / "artifacts-fallback"
    run_conversion(tampered, fallback_dataset, fallback_artifacts, spec_path)
    fallback_details = json.loads((fallback_artifacts / "steps/format_write.json").read_text(encoding="utf-8"))["details"]
    assert fallback_details["strategy"] == "legacy_reencode", fallback_details
    assert fallback_details["fallback_reason"], fallback_details
    assert fallback_details["video_files_reused"] == 0, fallback_details
    fallback_official = json.loads((fallback_artifacts / "steps/official_verify.json").read_text(encoding="utf-8"))["details"]
    assert fallback_official["output_loader_consistency_checked"] is True, fallback_official

    report = {
        "passed": True,
        "fixture": "2 episodes x 2 frames; one RGB camera; no depth/tactile/optional numeric fields",
        "cameras": source["cameras"],
        "missing_cameras": source["missing_cameras"],
        "missing_depth_source_cameras": source["missing_depth_source_cameras"],
        "optional_fields": source["optional_fields"],
        "tactile": source["tactile"],
        "stages": stages,
        "format_write": format_write,
        "subset_format_write": subset_details,
        "fallback_format_write": fallback_details,
        "official_verify": official,
        "archive_verify": archive,
    }
    report_path = work / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": True, "stages": len(stages), "cameras": source["cameras"],
                      "missing_cameras": len(source["missing_cameras"]),
                      "missing_depth": len(source["missing_depth_source_cameras"])}, ensure_ascii=False, indent=2))
    print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
