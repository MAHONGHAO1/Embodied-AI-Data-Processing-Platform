"""Generic LeRobot v2.1 directory adapter.

Unlike the pinned SO-100/ALOHA adapters, this accepts any structurally valid
LeRobot v2.1 dataset. The official LeRobot converter is used for the
v2.1 -> v3.0 step, so the input must carry ``meta/episodes_stats.jsonl``.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil

from ..contract import (INPUT_DIRECTORY, AdapterDescriptor, Detection, Preflight, PreparedInput,
                        SourceAdapter, rejected, supported)
from ..support import file_records, read_json, write_json

ADAPTER_ID = "lerobot_v21"
VERSION = "lerobot-v2.1-generic"
MARKER = "robodata_source.json"


def _cameras(info: dict) -> list[str]:
    return [key for key, feature in (info.get("features") or {}).items()
            if isinstance(feature, dict) and feature.get("dtype") == "video"]


def _episode_count(root: Path) -> int:
    jsonl = root / "meta/episodes.jsonl"
    if jsonl.is_file():
        return sum(1 for line in jsonl.read_text(encoding="utf-8").splitlines() if line.strip())
    files = sorted((root / "meta/episodes").glob("chunk-*/file-*.parquet"))
    if not files:
        raise FileNotFoundError("缺少 meta/episodes.jsonl 或 meta/episodes/")
    import pandas as pd
    return len(pd.concat([pd.read_parquet(path) for path in files], ignore_index=True))


def _frame_count(root: Path) -> int:
    jsonl = root / "meta/episodes.jsonl"
    if jsonl.is_file():
        total = 0
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            if line.strip():
                total += int(json.loads(line).get("length", 0))
        return total
    import pandas as pd
    files = sorted((root / "meta/episodes").glob("chunk-*/file-*.parquet"))
    table = pd.concat([pd.read_parquet(path) for path in files], ignore_index=True)
    return int(table["length"].sum())


class LeRobotV21Adapter(SourceAdapter):
    descriptor = AdapterDescriptor(
        adapter_id=ADAPTER_ID,
        label="LeRobot v2.1（通用输入）",
        source_kind=ADAPTER_ID,
        input_kind=INPUT_DIRECTORY,
        supported_versions=(VERSION,),
        pinned=False,
        supports_test_label=False,
        description="任意结构合法的 LeRobot v2.1 目录；官方 v2.1→v3.0 转换要求 meta/episodes_stats.jsonl。",
    )

    def detect(self, path: Path, *, test_label: str | None = None) -> Detection:
        path = Path(path)
        if not path.is_dir():
            return rejected(ADAPTER_ID, "输入不是目录", reasons=["LeRobot v2.1 需要目录"])
        info_path = path / "meta/info.json"
        if not info_path.is_file():
            return rejected(ADAPTER_ID, "缺少 meta/info.json", confidence=0.1)
        try:
            info = read_json(info_path)
        except (OSError, ValueError) as exc:
            return rejected(ADAPTER_ID, f"meta/info.json 无法读取：{exc}", confidence=0.2)
        version = info.get("codebase_version")
        if version == "v2.0":
            return rejected(ADAPTER_ID, "检测到 LeRobot v2.0；先升级到 v2.1（含 episodes_stats.jsonl）再导入",
                            confidence=0.6, version="v2.0", reasons=["v2.0 不在支持矩阵"])
        if version != "v2.1":
            return rejected(ADAPTER_ID, f"不是 LeRobot v2.1：{version!r}",
                            confidence=0.4, version=str(version), reasons=["版本不在支持矩阵"])
        if not any((path / "data").rglob("*.parquet")):
            return rejected(ADAPTER_ID, "LeRobot 目录没有 data/*.parquet", confidence=0.5, version=version)
        if not any((path / "videos").rglob("*.mp4")):
            return rejected(ADAPTER_ID, "LeRobot 目录没有 videos/*.mp4", confidence=0.5, version=version)
        if not (path / "meta/episodes_stats.jsonl").is_file():
            return rejected(ADAPTER_ID,
                            "v2.1 缺少 meta/episodes_stats.jsonl；官方 v2.1→v3.0 转换需要逐集统计",
                            confidence=0.6, version=version)
        cameras = _cameras(info)
        if not cameras:
            return rejected(ADAPTER_ID, "v2.1 元数据没有声明 video 相机", confidence=0.4, version=version)
        return supported(ADAPTER_ID, VERSION, 0.9,
                         ["LeRobot v2.1 结构", f"声明 {len(cameras)} 路相机",
                          "包含 meta/episodes_stats.jsonl"],
                         {"codebase_version": version, "cameras": cameras, "fps": info.get("fps")})

    def preflight(self, path: Path, *, test_label: str | None = None) -> Preflight:
        detection = self.detect(path, test_label=test_label)
        summary = {}
        warnings = []
        if detection.supported:
            try:
                info = read_json(Path(path) / "meta/info.json")
                summary = {"codebase_version": info.get("codebase_version"),
                           "episodes": _episode_count(Path(path)),
                           "frames": _frame_count(Path(path)),
                           "cameras": _cameras(info), "fps": info.get("fps")}
            except (OSError, ValueError, FileNotFoundError) as exc:
                warnings.append(str(exc))
        if detection.unsupported_reason:
            warnings.append(detection.unsupported_reason)
        return Preflight(adapter_id=ADAPTER_ID, supported=detection.supported,
                         detected_version=detection.detected_version, summary=summary,
                         warnings=warnings, unsupported_reason=detection.unsupported_reason)

    def prepare(self, request: dict, raw: Path, managed: Path) -> PreparedInput:
        source = Path(request["input_path"]).resolve()
        if request.get("test_label"):
            raise ValueError("通用 LeRobot v2.1 导入不支持故障副本标记")
        if not source.is_dir():
            raise NotADirectoryError("LeRobot v2.1 导入需要本地目录")
        paths = []
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise ValueError("LeRobot v2.1 输入包含符号链接，拒绝复制")
            relative = path.relative_to(source)
            target = raw / relative
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif path.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                paths.append(relative.as_posix())
        marker = {"schema_version": 1, "kind": "lerobot_v21", "source_format": "v2.1",
                  "original_root": str(source), "batch_label": str(request.get("label") or "").strip()}
        write_json(raw / MARKER, marker)
        interpretation = {
            "profile_version": "0.7.0", "kind": ADAPTER_ID, "format_version": "v2.1",
            "source_root": str(source), "episodes": _episode_count(raw),
            "batch_label": str(request.get("label") or "").strip(),
        }
        return PreparedInput(files=file_records(raw, paths), interpretation=interpretation)

    def preview(self, request: dict, managed: Path, run_dir: Path) -> dict:
        from ...dataset import load_metadata
        meta = load_metadata(managed / "source")
        return {"data_root": str(managed / "source"), "episode_indices": meta["episode_indices"]}

    def inventory(self, request: dict, managed: Path, run_dir: Path) -> dict:
        from ...dataset import load_metadata
        from ...quality import RULES
        metadata = load_metadata(managed / "source")
        manifest = read_json(managed / "input_manifest.json")
        source = {
            "kind": ADAPTER_ID, "managed_path": str(managed),
            "input_fingerprint": manifest["input_fingerprint"],
            "manifest_path": str(managed / "input_manifest.json"),
            "import_run_id": run_dir.name, "test_label": None,
        }
        source.update(metadata["source"], data_root=str(managed / "source"),
                      required_rules=list(RULES))
        episodes = [{"episode_index": index,
                     "row_count": int(metadata["episodes"][index]["length"])}
                    for index in metadata["episode_indices"]]
        return {"label": request.get("label", "本地导入批次"), "source": source, "episodes": episodes}


adapter = LeRobotV21Adapter()
