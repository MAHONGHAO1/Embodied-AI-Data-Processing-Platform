"""Generate the known-bad fixture sets used to validate the acceptance rules.

Each fixture is a small LeRobot v3.0 dataset with exactly one injected defect.
Run with the project environment:

    python scripts/make_known_bad.py

Rebuilding is idempotent: the output directory is deleted first, so the
fixtures on disk always match this script.
"""

from __future__ import annotations

import json
import shutil
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
import pandas as pd

FPS = 30.0
ROWS = 8
STATE_DIM = 6
IMAGE = 32
CAMERA = "observation.images.laptop"
SECOND_CAMERA = "observation.images.phone"

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "fixtures" / "known-bad"

MOTORS = ["main_shoulder_pan", "main_shoulder_lift", "main_elbow_flex",
          "main_wrist_flex", "main_wrist_roll", "main_gripper"]


def info_json(cameras: list[str]) -> dict:
    features = {
        "index": {"dtype": "int64", "shape": [1], "names": ["index"]},
        "episode_index": {"dtype": "int64", "shape": [1], "names": ["episode_index"]},
        "frame_index": {"dtype": "int64", "shape": [1], "names": ["frame_index"]},
        "timestamp": {"dtype": "float32", "shape": [1], "names": ["timestamp"]},
        "observation.state": {"dtype": "float32", "shape": [STATE_DIM],
                              "names": {"motors": MOTORS}},
        "action": {"dtype": "float32", "shape": [STATE_DIM],
                   "names": {"motors": MOTORS}},
    }
    for camera in cameras:
        features[camera] = {
            "dtype": "video", "shape": [IMAGE, IMAGE, 3],
            "names": ["height", "width", "channels"], "info": {"video.fps": FPS},
        }
    return {
        "codebase_version": "v3.0",
        "robot_type": "robodata-known-bad-fixture",
        "fps": FPS,
        "features": features,
        "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
        "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
    }


def data_table(episode_index: int = 0, nan_joint: bool = False) -> pd.DataFrame:
    frames = np.arange(ROWS, dtype=np.int64)
    timestamps = frames.astype(np.float64) / FPS
    state = np.tile(np.linspace(0.1, 0.6, STATE_DIM), (ROWS, 1))
    action = np.tile(np.linspace(0.2, 0.7, STATE_DIM), (ROWS, 1))
    if nan_joint:
        state[2, 1] = np.nan
    return pd.DataFrame({
        "index": np.arange(ROWS, dtype=np.int64),
        "episode_index": np.full(ROWS, episode_index, dtype=np.int64),
        "frame_index": frames,
        "timestamp": timestamps.astype(np.float32),
        "task_index": np.zeros(ROWS, dtype=np.int64),
        "observation.state": [row.astype(np.float32) for row in state],
        "action": [row.astype(np.float32) for row in action],
    })


def episode_index_row(episode_index: int, *, length: int, cameras: list[str]) -> dict:
    to_timestamp = ROWS / FPS
    row = {
        "episode_index": episode_index,
        "length": length,
        "task_index": 0,
        "tasks": ["known-bad fixture task"],
        "dataset_from_index": 0,
        "dataset_to_index": ROWS,
        "data/chunk_index": 0,
        "data/file_index": 0,
    }
    for camera in cameras:
        row[f"videos/{camera}/chunk_index"] = 0
        row[f"videos/{camera}/file_index"] = 0
        row[f"videos/{camera}/from_timestamp"] = 0.0
        row[f"videos/{camera}/to_timestamp"] = to_timestamp
    return row


def write_video(path: Path, frame_count: int, seed: int = 0) -> None:
    """Encode a tiny clip whose PTS are exactly 0, 1/fps, ..., (n-1)/fps."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    with av.open(str(path), mode="w") as container:
        stream = container.add_stream("mpeg4", rate=int(FPS))
        stream.width = IMAGE
        stream.height = IMAGE
        stream.pix_fmt = "yuv420p"
        stream.time_base = Fraction(1, int(FPS))
        for index in range(frame_count):
            pixels = rng.integers(0, 256, size=(IMAGE, IMAGE, 3), dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
            frame.pts = index
            frame.time_base = Fraction(1, int(FPS))
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def build_base(dest: Path, cameras: list[str], video_frames: int,
               length: int = ROWS, nan_joint: bool = False, seed: int = 0) -> None:
    (dest / "meta" / "episodes" / "chunk-000").mkdir(parents=True, exist_ok=True)
    (dest / "data" / "chunk-000").mkdir(parents=True, exist_ok=True)

    (dest / "meta" / "info.json").write_text(
        json.dumps(info_json(cameras), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")

    pd.DataFrame([episode_index_row(0, length=length, cameras=cameras)]).to_parquet(
        dest / "meta" / "episodes" / "chunk-000" / "file-000.parquet", index=False)
    pd.DataFrame({"task_index": [0]},
                 index=pd.Index(["known-bad fixture task"], name="task")).to_parquet(
        dest / "meta" / "tasks.parquet")
    data_table(nan_joint=nan_joint).to_parquet(
        dest / "data" / "chunk-000" / "file-000.parquet", index=False)

    for offset, camera in enumerate(cameras):
        write_video(dest / "videos" / camera / "chunk-000" / "file-000.mp4",
                    video_frames, seed=seed + offset)

    (dest / "source_manifest.json").write_text(json.dumps({
        "source": {"repo_id": "MAHONGHAO1/robodata-known-bad",
                   "revision": "fixtures", "license": "MIT",
                   "robot": "robodata-known-bad-fixture"},
        "note": "人工构造的故障样本，仅供判据验证，不是真实采集数据。",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


FIXTURES = [
    {
        "name": "missing-camera",
        "title": "缺一路相机视频",
        "inject": "声明两路相机，只保留 laptop 的视频文件，phone 的文件删除。",
        "expect": ["video_decode"],
        "build": lambda dest: build_base(dest, [CAMERA, SECOND_CAMERA], ROWS, seed=1),
        "after": lambda dest: (dest / "videos" / SECOND_CAMERA / "chunk-000" / "file-000.mp4").unlink(),
    },
    {
        "name": "frame-gap",
        "title": "帧索引跳号",
        "inject": "第 3 行的 frame_index 从 3 改成 5，中间空出一帧。",
        "expect": ["frame_index"],
        "build": lambda dest: build_base(dest, [CAMERA], ROWS, seed=2),
        "after": lambda dest: _patch_data(dest, {"frame_index": {3: 5}}),
    },
    {
        "name": "nan-joint",
        "title": "关节值出现 NaN",
        "inject": "第 2 行 observation.state 的第 2 个关节改为 NaN。",
        "expect": ["numeric_finite"],
        "build": lambda dest: build_base(dest, [CAMERA], ROWS, nan_joint=True, seed=3),
        "after": None,
    },
    {
        "name": "length-mismatch",
        "title": "表格行数与元数据不符",
        "inject": "元数据把 length 写成 12，实际只有 8 行。",
        "expect": ["row_count"],
        "build": lambda dest: build_base(dest, [CAMERA], ROWS, length=ROWS + 4, seed=4),
        "after": None,
    },
    {
        "name": "timestamp-jump",
        "title": "时间戳跳了一段",
        "inject": "第 5 行之后整体平移 10 秒，采样还在继续但中间空了一段。",
        "expect": ["timestamp_interval", "video_timestamp_mapping"],
        "build": lambda dest: build_base(dest, [CAMERA], ROWS, seed=5),
        "after": lambda dest: _patch_data(dest, {"timestamp": {
            row: float(row / FPS) + 10.0 for row in range(5, ROWS)}}),
    },
    {
        "name": "video-length",
        "title": "视频帧数与行数不符",
        "inject": "视频只编码 5 帧，表格有 8 行。",
        "expect": ["video_frame_count"],
        "build": lambda dest: build_base(dest, [CAMERA], ROWS - 3, seed=6),
        "after": None,
    },
]


def _patch_data(dest: Path, changes: dict[str, dict[int, object]]) -> None:
    path = dest / "data" / "chunk-000" / "file-000.parquet"
    table = pd.read_parquet(path)
    for column, edits in changes.items():
        for row, value in edits.items():
            table.at[row, column] = value
    table.to_parquet(path, index=False)


def observed_rules(dest: Path) -> tuple[list[str], str]:
    """Run the real checker and return the rules that actually fired."""
    from robodata.quality import run_batch
    report = run_batch(dest, episode_indices=[0], check_video=True)
    fired = sorted({item["rule"] for item in report["issues"]
                    if item["category"] != "not_checked"})
    return fired, report["episodes"][0]["status"]


def expected_markdown(item: dict, fired: list[str], status: str) -> str:
    expected = "\n".join(f"- `{rule}`" for rule in item["expect"])
    actual = "\n".join(f"- `{rule}`" for rule in fired) or "- 无"
    return (
        f"# {item['title']}\n\n"
        f"{item['inject']}\n\n"
        "应当命中的判据：\n\n"
        f"{expected}\n\n"
        f"实测（`python scripts/make_known_bad.py` 自动记录，任务状态 `{status}`）：\n\n"
        f"{actual}\n\n"
        "这个目录是故意做坏的，不是采集缺陷样本。判据来源见 `docs/RULES.md`。\n"
    )


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    failures = []
    for item in FIXTURES:
        dest = OUT / item["name"]
        item["build"](dest)
        if item["after"]:
            item["after"](dest)
        fired, status = observed_rules(dest)
        missing = [rule for rule in item["expect"] if rule not in fired]
        if missing:
            failures.append(f"{item['name']}: 判据 {missing} 未命中，实测命中 {fired}")
        (dest / "EXPECTED.md").write_text(
            expected_markdown(item, fired, status), encoding="utf-8")
        print(f"{item['name']}: 期望 {item['expect']} -> 实测 {fired} ({status})")
    (OUT / "README.md").write_text(
        "# known-bad 故障样本\n\n"
        "每个目录是一个故意做坏的小数据集，附 `EXPECTED.md` 写明应当命中的判据，"
        "以及生成时实测命中的判据。\n\n"
        "它们用来验证判据本身有没有在工作，也能拿去测你自己的检查器。\n\n"
        "生成方式：`python scripts/make_known_bad.py`（会重建整个目录，并在实测不符合预期时失败）。\n\n"
        "判据定义见 `docs/RULES.md`。\n",
        encoding="utf-8")
    if failures:
        for line in failures:
            print(f"FAIL {line}")
        return 1
    print(f"\nwritten to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
