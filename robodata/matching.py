"""Discovery and integrity helpers for Matching Cable teleoperation HDF5 frames."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

SOURCE_KIND = "matching_cable"
FPS = 20
CAMERAS = (
    "observation.images.camera_0",
    "observation.images.camera_1",
    "observation.images.camera_2",
    "observation.images.camera_3",
)
CAMERA_SOURCE_FIELDS = {
    camera: f"observations/rgb_images/camera_{index}" for index, camera in enumerate(CAMERAS)
}
DEPTH_CAMERAS = ("observations/depth_images/camera_1", "observations/depth_images/camera_2")
STATE_FIELD = "puppet/joint_position"
ACTION_FIELD = "master/joint_position"
OPTIONAL_NUMERIC_FIELDS = {
    "puppet/eef": 28,
    "master/eef": 28,
    "puppet/joint_effort": 16,
    "puppet/6f": 12,
}
OPTIONAL_FEATURE_KEYS = {
    "puppet/eef": "observation.eef",
    "master/eef": "master.eef",
    "puppet/joint_effort": "observation.joint_effort",
    "puppet/6f": "observation.force_torque",
}
REQUIRED_FIELDS = (
    ACTION_FIELD,
    STATE_FIELD,
    "timestamps",
)


def _episode_number(path: Path) -> int:
    if not path.name.isdigit():
        raise ValueError(f"Episode 目录必须使用数字名称：{path.name}")
    return int(path.name)


def _frame_number(path: Path) -> int:
    if not path.stem.isdigit():
        raise ValueError(f"帧文件名必须使用数字序号：{path.name}")
    return int(path.stem)


def capabilities_from_fields(present_fields: set[str]) -> dict:
    """Pure capability classification shared by HDF5 inspection and tests."""
    missing_required = [field for field in REQUIRED_FIELDS if field not in present_fields]
    cameras = [camera for camera in CAMERAS if CAMERA_SOURCE_FIELDS[camera] in present_fields]
    missing_cameras = [camera for camera in CAMERAS if camera not in cameras]
    depth_present = [field for field in DEPTH_CAMERAS if field in present_fields]
    depth_missing = [field for field in DEPTH_CAMERAS if field not in present_fields]
    optional_present = [field for field in OPTIONAL_NUMERIC_FIELDS if field in present_fields]
    optional_missing = [field for field in OPTIONAL_NUMERIC_FIELDS if field not in present_fields]
    if missing_required:
        raise ValueError("Matching Cable 帧缺少必填字段：" + "、".join(f"/{field}" for field in missing_required))
    if not cameras:
        raise ValueError("Matching Cable 帧没有可用的 RGB 相机；当前适配器至少需要一路 RGB")
    return {
        "cameras": cameras,
        "missing_cameras": missing_cameras,
        "depth_present": depth_present,
        "depth_missing": depth_missing,
        "optional_present": optional_present,
        "optional_missing": optional_missing,
        "tactile": {"status": "not_provided", "note": "适配器不读取触觉字段；缺失不影响导入"},
    }


def inspect_frame_capabilities(path: Path) -> dict:
    """Describe which optional streams a Matching Cable frame actually provides.

    Missing depth, optional numeric fields or extra RGB cameras are capabilities,
    not corruption: the preview records what is present and continues. Only the
    state, action and timestamp fields plus at least one RGB camera are required.
    """
    import h5py

    candidates = [*REQUIRED_FIELDS, *CAMERA_SOURCE_FIELDS.values(), *DEPTH_CAMERAS, *OPTIONAL_NUMERIC_FIELDS]
    with h5py.File(path, "r") as handle:
        present_fields = {field for field in candidates if field in handle}
    return capabilities_from_fields(present_fields)


def raw_frames(source_root: Path, source_id: int, expected_length: int) -> list[Path]:
    """Return exactly the previewed frames; extra or missing frames both fail."""
    directory = Path(source_root) / str(source_id)
    if not directory.is_dir():
        raise FileNotFoundError(f"源 Episode 目录不存在：{directory}")
    frames = sorted(directory.glob("*.hdf5"), key=_frame_number)
    if len(frames) != expected_length:
        raise ValueError(f"源 Episode {source_id} 帧数变化：期望 {expected_length}，实际 {len(frames)}")
    numbers = [_frame_number(path) for path in frames]
    if numbers != list(range(numbers[0], numbers[0] + expected_length)):
        raise ValueError(f"源 Episode {source_id} 帧序号不连续")
    return frames


def parse_episode_selection(raw: str | None) -> list[int] | None:
    text = (raw or "").strip()
    if not text:
        return None
    values: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo_text, _, hi_text = part.partition("-")
            try:
                lo, hi = int(lo_text), int(hi_text)
            except ValueError as exc:
                raise ValueError(f"数据范围片段无法解析：{part}") from exc
            if hi < lo:
                raise ValueError(f"数据范围区间上界小于下界：{part}")
            values.extend(range(lo, hi + 1))
        else:
            try:
                values.append(int(part))
            except ValueError as exc:
                raise ValueError(f"数据范围片段无法解析：{part}") from exc
    if not values or any(value < 0 for value in values):
        raise ValueError(f"数据范围无效：{raw!r}")
    return sorted(set(values))


def discover_episodes(root: Path, episode_range: str | None = None) -> list[dict]:
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Matching Cable 导入需要帧目录：{root}")
    if root.name.isdigit() and any(root.glob("*.hdf5")):
        candidates = [root]
    else:
        candidates = [path for path in root.iterdir()
                      if path.is_dir() and path.name.isdigit() and any(path.glob("*.hdf5"))]
    candidates.sort(key=_episode_number)
    selected = parse_episode_selection(episode_range)
    if selected is not None:
        by_id = {_episode_number(path): path for path in candidates}
        missing = [value for value in selected if value not in by_id]
        if missing:
            raise ValueError(f"数据范围包含不存在的 Episode：{missing}")
        candidates = [by_id[value] for value in selected]
    if not candidates:
        raise ValueError("目录下没有找到数字 Episode 目录和 HDF5 帧文件")
    max_frames_text = (os.environ.get("ROBODATA_MATCHING_MAX_FRAMES_PER_EPISODE") or "").strip()
    try:
        max_frames = int(max_frames_text) if max_frames_text else None
    except ValueError as exc:
        raise ValueError("ROBODATA_MATCHING_MAX_FRAMES_PER_EPISODE 必须为正整数") from exc
    if max_frames is not None and max_frames < 1:
        raise ValueError("ROBODATA_MATCHING_MAX_FRAMES_PER_EPISODE 必须为正整数")
    result = []
    for output_index, path in enumerate(candidates):
        frames = sorted(path.glob("*.hdf5"), key=_frame_number)
        if not frames:
            raise ValueError(f"Episode {path.name} 没有 HDF5 帧文件")
        if max_frames is not None:
            frames = frames[:max_frames]
        numbers = [_frame_number(frame) for frame in frames]
        if numbers != list(range(numbers[0], numbers[0] + len(numbers))):
            raise ValueError(f"Episode {path.name} 的 HDF5 文件序号不连续")
        result.append({"episode_index": output_index, "source_episode_id": _episode_number(path),
                       "path": path, "frames": frames})
    return result


def file_records(root: Path, episodes: list[dict], include_hashes: bool = False) -> list[dict]:
    root = Path(root).resolve()
    records = []
    for episode in episodes:
        for frame in episode["frames"]:
            path = Path(frame)
            record = {"path": path.relative_to(root).as_posix(), "size": path.stat().st_size,
                      "mtime_ns": path.stat().st_mtime_ns,
                      "episode_id": int(episode["source_episode_id"])}
            if include_hashes:
                record["sha256"] = _sha256(path)
            records.append(record)
    return records


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_digest(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def assert_source_unchanged(source: dict) -> str:
    root = Path(source.get("raw_data_root") or source["data_root"]).resolve()
    manifest = json.loads(Path(source["manifest_path"]).read_text(encoding="utf-8"))
    expected = manifest.get("files")
    if not isinstance(expected, list) or not expected:
        raise ValueError("Matching Cable 输入清单缺少文件记录")
    actual = []
    for item in expected:
        path = (root / item["path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError(f"原始帧文件缺失或路径越界：{item.get('path')}")
        current = {"path": item["path"], "size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns,
                   "episode_id": int(item["episode_id"])}
        if item.get("sha256"):
            current["sha256"] = _sha256(path)
        actual.append(current)
    if actual != expected:
        raise ValueError("Matching Cable 原始帧文件已变化，请重新导入；当前检查和产物不可继续使用")
    return source["input_fingerprint"]
