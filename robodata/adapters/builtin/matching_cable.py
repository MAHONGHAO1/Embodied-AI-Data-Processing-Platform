"""Matching Cable per-frame HDF5 directory adapter."""

from __future__ import annotations

import os
from pathlib import Path

from ...matching import (SOURCE_KIND, discover_episodes, file_records as matching_file_records)
from ..contract import (INPUT_DIRECTORY, AdapterDescriptor, Detection, Preflight, PreparedInput,
                        SourceAdapter, rejected, supported)
from ..support import PROFILE, digest, matching_preview_worker, read_json, write_json

ADAPTER_ID = "matching_cable"
VERSION = "matching_cable_frames_v1"


def _structural_scan(path: Path) -> tuple[list[Path], int]:
    episodes = []
    frames = 0
    if path.is_dir():
        for candidate in sorted(path.iterdir()):
            if not candidate.is_dir() or not candidate.name.isdigit():
                continue
            count = sum(1 for item in candidate.glob("*.hdf5") if item.is_file())
            if count:
                episodes.append(candidate)
                frames += count
    return episodes, frames


class MatchingCableAdapter(SourceAdapter):
    descriptor = AdapterDescriptor(
        adapter_id=ADAPTER_ID,
        label="Matching Cable（逐帧 HDF5 目录）",
        source_kind=SOURCE_KIND,
        input_kind=INPUT_DIRECTORY,
        supported_versions=(VERSION,),
        pinned=False,
        supports_test_label=False,
        description="父目录下每个数字子目录是一条 Episode，每个 .hdf5 是一帧；可选流按实际存在记录。",
    )

    def detect(self, path: Path, *, test_label: str | None = None) -> Detection:
        path = Path(path)
        if not path.is_dir():
            return rejected(ADAPTER_ID, "输入不是目录", reasons=["Matching Cable 需要包含数字 Episode 目录的本地目录"])
        if (path / "meta/info.json").is_file():
            return rejected(ADAPTER_ID, "该目录看起来是 LeRobot 数据集，不是 Matching Cable 逐帧目录",
                            confidence=0.2)
        episodes, frames = _structural_scan(path)
        if not episodes:
            return rejected(ADAPTER_ID, "没有找到包含 .hdf5 帧的数字 Episode 目录", confidence=0.1)
        return supported(ADAPTER_ID, VERSION, 0.9,
                         [f"找到 {len(episodes)} 个 Episode 目录、{frames} 个 .hdf5 帧",
                          "目录结构匹配；字段与可选流将在预览阶段逐帧校验"],
                         {"episodes": len(episodes), "frames": frames,
                          "episode_ids": [int(item.name) for item in episodes][:50]})

    def preflight(self, path: Path, *, test_label: str | None = None) -> Preflight:
        detection = self.detect(path, test_label=test_label)
        summary: dict = {}
        warnings: list[str] = []
        if detection.supported:
            try:
                episodes = discover_episodes(Path(path), os.environ.get("ROBODATA_MATCHING_EPISODES"))
                summary = {"episodes": len(episodes),
                           "frames": sum(len(item["frames"]) for item in episodes),
                           "episode_ids": [int(item["source_episode_id"]) for item in episodes],
                           "frame_limit": os.environ.get("ROBODATA_MATCHING_MAX_FRAMES_PER_EPISODE") or None}
            except ValueError as exc:
                return Preflight(adapter_id=ADAPTER_ID, supported=False,
                                 detected_version=detection.detected_version, summary={},
                                 warnings=[str(exc)], unsupported_reason=str(exc))
        if detection.unsupported_reason:
            warnings.append(detection.unsupported_reason)
        return Preflight(adapter_id=ADAPTER_ID, supported=detection.supported,
                         detected_version=detection.detected_version, summary=summary,
                         warnings=warnings, unsupported_reason=detection.unsupported_reason)

    def prepare(self, request: dict, raw: Path, managed: Path) -> PreparedInput:
        source = Path(request["input_path"]).resolve()
        if request.get("test_label"):
            raise ValueError("Matching Cable 本机数据不支持固定样本故障副本标记")
        episodes = discover_episodes(source, os.environ.get("ROBODATA_MATCHING_EPISODES"))
        files = matching_file_records(source, episodes, include_hashes=False)
        interpretation = {
            **PROFILE, "kind": SOURCE_KIND, "source_root": str(source),
            "episodes": [{"episode_index": item["episode_index"],
                          "source_episode_id": item["source_episode_id"],
                          "frame_count": len(item["frames"])} for item in episodes],
            "state_field": "puppet/joint_position", "action_field": "master/joint_position",
            "fps": 20, "test_label": None,
            "batch_label": str(request.get("label") or "").strip(),
        }
        return PreparedInput(files=files, interpretation=interpretation)

    def preview(self, request: dict, managed: Path, run_dir: Path) -> dict:
        matching_preview_worker(Path(request["input_path"]), managed / "preview")
        preview_manifest = read_json(managed / "preview/source_manifest.json")
        input_manifest = read_json(managed / "input_manifest.json")
        input_manifest["files"] = preview_manifest["files"]
        input_manifest["interpretation"]["episodes"] = [
            {"episode_index": item["episode_index"], "source_episode_id": item["source_episode_id"],
             "frame_count": item["length"]} for item in preview_manifest["episodes"]]
        for key in ("cameras", "missing_cameras", "depth_source_cameras",
                    "missing_depth_source_cameras", "optional_fields", "tactile"):
            input_manifest["interpretation"][key] = preview_manifest["source"].get(key)
        fingerprint = digest({"files": input_manifest["files"],
                              "interpretation": input_manifest["interpretation"]})
        input_manifest["input_fingerprint"] = fingerprint
        preview_manifest["input_fingerprint"] = fingerprint
        write_json(managed / "input_manifest.json", input_manifest)
        write_json(managed / "preview/source_manifest.json", preview_manifest)
        return {"data_root": str(managed / "preview"), "preview_root": str(managed / "preview"),
                "metadata_path": str(managed / "preview/source_manifest.json"),
                "episode_indices": [item["episode_index"] for item in preview_manifest["episodes"]]}

    def inventory(self, request: dict, managed: Path, run_dir: Path) -> dict:
        from ...dataset import load_metadata
        from ...quality import RULES
        preview = managed / "preview"
        preview_manifest = read_json(preview / "source_manifest.json")
        manifest = read_json(managed / "input_manifest.json")
        source = {
            "kind": SOURCE_KIND, "managed_path": str(managed),
            "input_fingerprint": manifest["input_fingerprint"],
            "manifest_path": str(managed / "input_manifest.json"),
            "import_run_id": run_dir.name, "test_label": request.get("test_label"),
            "data_root": str(preview),
            "raw_data_root": str(Path(request["input_path"]).resolve()),
            "preview_root": str(preview),
            "repo_id": "local/matching-cable-teleoperation", "revision": "local-source",
            "license": "用户本机数据；许可由数据提供方确认", "robot": "双主从臂遥操作平台",
            "format_version": VERSION, "fps": 20.0,
            "camera": preview_manifest["source"]["camera"],
            "cameras": preview_manifest["source"]["cameras"],
            "camera_metadata": preview_manifest["camera_metadata"],
            "missing_cameras": preview_manifest["source"].get("missing_cameras", []),
            "depth_source_cameras": preview_manifest["source"].get("depth_source_cameras", []),
            "missing_depth_source_cameras": preview_manifest["source"].get("missing_depth_source_cameras", []),
            "optional_fields": preview_manifest["source"].get("optional_fields", {}),
            "tactile": preview_manifest["source"].get("tactile"),
            "episode_mapping": preview_manifest["source"]["episode_mapping"],
            "state_field": "puppet/joint_position", "action_field": "master/joint_position",
            "source_statistics": {"source_episodes": len(preview_manifest["episodes"]),
                                  "source_frames": preview_manifest["total_frames"]},
            "warnings": preview_manifest.get("timestamp_warnings", []),
            "required_rules": list(RULES),
            "timestamp_semantics": preview_manifest["source"]["timestamp_semantics"],
        }
        episodes = [{"episode_index": item["episode_index"], "row_count": int(item["length"]),
                     "source_episode_id": item["source_episode_id"]}
                    for item in preview_manifest["episodes"]]
        return {"label": request.get("label", "本地导入批次"), "source": source, "episodes": episodes}


adapter = MatchingCableAdapter()
