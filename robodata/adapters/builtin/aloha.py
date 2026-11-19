"""Pinned ALOHA Coffee LeRobot v3.0 adapter."""

from __future__ import annotations

import os
from pathlib import Path

from ...aloha import (ALOHA_CAMERAS, ALOHA_EPISODES, ALOHA_FILES, ALOHA_MANIFEST_NAME,
                      ALOHA_REPO_ID, ALOHA_REVISION, fetch_aloha_sample)
from ..contract import (INPUT_DIRECTORY, AdapterDescriptor, Detection, Preflight, PreparedInput,
                        SourceAdapter, rejected, supported)
from ..support import read_json

ADAPTER_ID = "aloha"


class AlohaAdapter(SourceAdapter):
    descriptor = AdapterDescriptor(
        adapter_id=ADAPTER_ID,
        label="ALOHA Coffee（固定 revision 的四相机 v3.0 样本）",
        source_kind="aloha",
        input_kind=INPUT_DIRECTORY,
        supported_versions=("lerobot-v3.0-aloha-coffee-pinned",),
        pinned=True,
        supports_test_label=False,
        description="固定 repo_id/revision/相机/帧率的 ALOHA Coffee 目录；导入时只读下载并校验固定清单。",
    )

    def detect(self, path: Path, *, test_label: str | None = None) -> Detection:
        path = Path(path)
        if not path.is_dir():
            return rejected(ADAPTER_ID, "输入不是目录", reasons=["ALOHA Coffee 需要本地 LeRobot 目录"])
        info_path = path / "meta/info.json"
        if not info_path.is_file():
            return rejected(ADAPTER_ID, "缺少 meta/info.json，不是 LeRobot 目录", confidence=0.1)
        try:
            info = read_json(info_path)
        except (ValueError, OSError) as exc:
            return rejected(ADAPTER_ID, f"meta/info.json 无法读取：{exc}", confidence=0.2)
        version = info.get("codebase_version")
        if version != "v3.0":
            return rejected(ADAPTER_ID, f"ALOHA Coffee 固定来源只支持 LeRobot v3.0，检测到 {version!r}",
                            confidence=0.4, version=str(version), reasons=["版本不在支持矩阵内"])
        if not any((path / "data").rglob("*.parquet")):
            return rejected(ADAPTER_ID, "LeRobot 目录没有 data/*.parquet", confidence=0.5,
                            version=version, reasons=["缺少数据表"])
        if not any((path / "videos").rglob("*.mp4")):
            return rejected(ADAPTER_ID, "LeRobot 目录没有 videos/*.mp4", confidence=0.5,
                            version=version, reasons=["缺少视频文件"])
        manifest_path = path / ALOHA_MANIFEST_NAME
        if not manifest_path.is_file():
            return rejected(ADAPTER_ID, "缺少固定来源清单 source_manifest.json，无法证明 ALOHA Coffee 来源",
                            confidence=0.6, version=version)
        declared = read_json(manifest_path).get("source", {})
        if declared.get("repo_id") != ALOHA_REPO_ID:
            return rejected(ADAPTER_ID, f"来源清单不是 ALOHA Coffee：{declared.get('repo_id')!r}",
                            confidence=0.2, version=version)
        if declared.get("revision") != ALOHA_REVISION:
            return rejected(ADAPTER_ID, "ALOHA Coffee revision 不符合已固定版本",
                            confidence=0.5, version=version,
                            reasons=[f"清单 revision：{declared.get('revision')}"])
        cameras = sorted(key for key, feature in info.get("features", {}).items()
                         if isinstance(feature, dict) and feature.get("dtype") == "video")
        if set(cameras) != set(ALOHA_CAMERAS):
            return rejected(ADAPTER_ID, "ALOHA Coffee 相机特征不符合固定四相机配置",
                            confidence=0.5, version=version, reasons=[f"实际相机：{cameras}"])
        if float(info.get("fps", 0)) != 50.0:
            return rejected(ADAPTER_ID, "ALOHA Coffee 帧率不是固定 50 FPS",
                            confidence=0.5, version=version)
        return supported(ADAPTER_ID, "lerobot-v3.0-aloha-coffee-pinned", 0.98,
                         ["LeRobot v3.0", "repo_id/revision/相机/帧率均匹配固定来源"],
                         {"repo_id": ALOHA_REPO_ID, "revision": ALOHA_REVISION,
                          "cameras": cameras, "fps": info.get("fps")})

    def preflight(self, path: Path, *, test_label: str | None = None) -> Preflight:
        detection = self.detect(path, test_label=test_label)
        summary: dict = {}
        if Path(path, "meta/info.json").is_file():
            info = read_json(Path(path) / "meta/info.json")
            summary = {"codebase_version": info.get("codebase_version"), "fps": info.get("fps"),
                       "cameras": [key for key, feature in info.get("features", {}).items()
                                   if isinstance(feature, dict) and feature.get("dtype") == "video"],
                       "data_files": len(list((Path(path) / "data").rglob("*.parquet")))}
        return Preflight(adapter_id=ADAPTER_ID, supported=detection.supported,
                         detected_version=detection.detected_version, summary=summary,
                         warnings=[detection.unsupported_reason] if detection.unsupported_reason else [],
                         unsupported_reason=detection.unsupported_reason)

    def prepare(self, request: dict, raw: Path, managed: Path) -> PreparedInput:
        if request.get("test_label"):
            raise ValueError("固定 ALOHA Coffee 来源不能使用故障副本标记；故障测试必须使用独立合成夹具")
        source_manifest = fetch_aloha_sample(
            raw, proxy=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))
        names = [*ALOHA_FILES, ALOHA_MANIFEST_NAME]
        interpretation = {
            "profile_version": "0.6.0", "kind": "aloha",
            "repo_id": source_manifest["source"]["repo_id"], "revision": ALOHA_REVISION,
            "episodes": list(ALOHA_EPISODES), "test_label": None,
            "batch_label": str(request.get("label") or "").strip(),
        }
        from ..support import file_records
        return PreparedInput(files=file_records(raw, names), interpretation=interpretation)

    def preview(self, request: dict, managed: Path, run_dir: Path) -> dict:
        from ...dataset import load_metadata
        meta = load_metadata(managed / "source")
        return {"data_root": str(managed / "source"), "episode_indices": meta["episode_indices"]}

    def inventory(self, request: dict, managed: Path, run_dir: Path) -> dict:
        from ...dataset import load_metadata
        from ...quality import RULES
        metadata = load_metadata(managed / "source")
        indices = [index for index in ALOHA_EPISODES if index in metadata["episodes"]]
        if tuple(indices) != ALOHA_EPISODES:
            raise ValueError("固定 ALOHA Coffee Episode 0–4 不完整，拒绝创建批次")
        manifest = read_json(managed / "input_manifest.json")
        source = {
            "kind": "aloha", "managed_path": str(managed),
            "input_fingerprint": manifest["input_fingerprint"],
            "manifest_path": str(managed / "input_manifest.json"),
            "import_run_id": run_dir.name, "test_label": request.get("test_label"),
        }
        source.update(metadata["source"], data_root=str(managed / "source"), required_rules=list(RULES))
        episodes = [{"episode_index": index, "row_count": int(metadata["episodes"][index]["length"])}
                    for index in indices]
        return {"label": request.get("label", "本地导入批次"), "source": source, "episodes": episodes}


adapter = AlohaAdapter()
