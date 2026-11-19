"""Pinned SO-100 LeRobot v3.0 adapter."""

from __future__ import annotations

from pathlib import Path
import shutil

from ...config import REPO_ID, REVISION
from ...source import DEMO_MANIFEST_NAME, MANIFEST_NAME, SAMPLE_FILES, file_sha256
from ..contract import (INPUT_DIRECTORY, AdapterDescriptor, Detection, Preflight, PreparedInput,
                        SourceAdapter, rejected, supported)
from ..support import PROFILE, file_records, read_json

ADAPTER_ID = "so100"


def _manifest_entries(source: Path):
    """Read the download manifest so a complete episode subset can be imported."""
    manifest_path = source / MANIFEST_NAME
    if not manifest_path.is_file():
        return None, {}
    manifest = read_json(manifest_path)
    if manifest.get("repo_id") != REPO_ID or manifest.get("revision") != REVISION:
        raise ValueError("SO-100 来源清单的仓库或 revision 与固定配置不一致")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise ValueError("SO-100 来源清单没有文件记录")
    expected = {}
    for entry in entries:
        relative = entry.get("path")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("SO-100 来源清单包含不安全路径")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError(f"SO-100 来源清单缺少 SHA-256：{relative}")
        expected[relative] = digest
    return [entry["path"] for entry in entries], expected


class So100Adapter(SourceAdapter):
    descriptor = AdapterDescriptor(
        adapter_id=ADAPTER_ID,
        label="SO-100（固定 LeRobot v3.0 样本）",
        source_kind="so100",
        input_kind=INPUT_DIRECTORY,
        supported_versions=("lerobot-v3.0-so100-pinned", "lerobot-v3.0-so100-test-copy"),
        pinned=True,
        supports_test_label=True,
        description="固定 revision 的 SO-100 LeRobot v3.0 目录；完整 Episode 子集可导入，故障副本必须标注。",
    )

    def detect(self, path: Path, *, test_label: str | None = None) -> Detection:
        path = Path(path)
        if not path.is_dir():
            return rejected(ADAPTER_ID, "输入不是目录", reasons=["SO-100 需要包含 meta/ 与 data/ 的目录"])
        info_path = path / "meta/info.json"
        if not info_path.is_file():
            return rejected(ADAPTER_ID, "缺少 meta/info.json，不是 LeRobot 目录", confidence=0.1)
        try:
            info = read_json(info_path)
        except (ValueError, OSError) as exc:
            return rejected(ADAPTER_ID, f"meta/info.json 无法读取：{exc}", confidence=0.2)
        version = info.get("codebase_version")
        if version in {"v2.0", "v2.1"}:
            return rejected(ADAPTER_ID, f"检测到 LeRobot {version}；当前导入只支持 v3.0，v2.x 计划在 v0.15 提供",
                            confidence=0.8, version=version, reasons=["版本不在支持矩阵内"])
        if version != "v3.0":
            return rejected(ADAPTER_ID, f"未知 LeRobot 版本：{version!r}；当前导入只支持 v3.0",
                            confidence=0.6, version=str(version), reasons=["版本不在支持矩阵内"])
        if not any((path / "data").rglob("*.parquet")):
            return rejected(ADAPTER_ID, "LeRobot 目录没有 data/*.parquet", confidence=0.5,
                            version=version, reasons=["缺少数据表"])
        if not any((path / "videos").rglob("*.mp4")):
            return rejected(ADAPTER_ID, "LeRobot 目录没有 videos/*.mp4", confidence=0.5,
                            version=version, reasons=["缺少视频文件"])
        manifest_path = path / MANIFEST_NAME
        manifest = read_json(manifest_path) if manifest_path.is_file() else {}
        declared = manifest.get("source", {}) if isinstance(manifest, dict) else {}
        pinned = declared.get("repo_id") == REPO_ID and declared.get("revision") == REVISION
        if test_label:
            return supported(ADAPTER_ID, "lerobot-v3.0-so100-test-copy", 0.75,
                             ["LeRobot v3.0 目录", f"故障副本测试标记：{test_label}"],
                             {"codebase_version": version, "has_manifest": manifest_path.is_file()})
        if not manifest_path.is_file():
            return rejected(ADAPTER_ID, "缺少固定来源清单 source_manifest.json，无法证明 SO-100 来源",
                            confidence=0.6, version=version,
                            reasons=["LeRobot v3.0 结构匹配，但未固定来源"])
        if not pinned:
            return rejected(ADAPTER_ID, "来源清单的 repo_id/revision 与固定 SO-100 配置不一致",
                            confidence=0.4, version=version,
                            reasons=[f"清单来源：{declared.get('repo_id')}@{declared.get('revision')}"])
        return supported(ADAPTER_ID, "lerobot-v3.0-so100-pinned", 0.97,
                         ["LeRobot v3.0 目录", "来源清单匹配固定 repo_id/revision"],
                         {"codebase_version": version, "repo_id": REPO_ID, "revision": REVISION})

    def preflight(self, path: Path, *, test_label: str | None = None) -> Preflight:
        detection = self.detect(path, test_label=test_label)
        path = Path(path)
        summary: dict = {}
        warnings: list[str] = []
        if path.is_dir() and (path / "meta/info.json").is_file():
            info = read_json(path / "meta/info.json")
            summary = {
                "codebase_version": info.get("codebase_version"),
                "fps": info.get("fps"),
                "cameras": [key for key, feature in info.get("features", {}).items()
                            if isinstance(feature, dict) and feature.get("dtype") == "video"],
                "data_files": len(list((path / "data").rglob("*.parquet"))),
            }
        if not detection.supported and detection.unsupported_reason:
            warnings.append(detection.unsupported_reason)
        return Preflight(adapter_id=ADAPTER_ID, supported=detection.supported,
                         detected_version=detection.detected_version, summary=summary,
                         warnings=warnings, unsupported_reason=detection.unsupported_reason)

    def prepare(self, request: dict, raw: Path, managed: Path) -> PreparedInput:
        source = Path(request["input_path"]).resolve()
        label = request.get("test_label")
        if not source.is_dir():
            raise NotADirectoryError("SO-100 导入入口需要本地数据目录")
        manifest_names, expected_hashes = _manifest_entries(source)
        names = ([*manifest_names, MANIFEST_NAME] if manifest_names is not None
                 else [*SAMPLE_FILES, MANIFEST_NAME])
        if (source / DEMO_MANIFEST_NAME).is_file():
            names.append(DEMO_MANIFEST_NAME)
            if not label:
                raise ValueError("检测到故障副本清单，请填写测试标记再导入")
        pinned = read_json(Path(__file__).parents[2] / "profiles/so100_hashes.json")
        for name in names:
            file = source / name
            if not file.is_file():
                if label:
                    continue
                raise FileNotFoundError(f"固定 SO-100 样本缺少文件：{name}")
            before = file_sha256(file)
            if not label and name in expected_hashes and before != expected_hashes[name]:
                raise ValueError(f"SO-100 文件与来源清单不一致：{name}")
            if not label and name in pinned["files"] and before != pinned["files"][name]:
                raise ValueError(f"SO-100 文件与固定来源不一致：{name}")
            destination = raw / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, destination)
            if file_sha256(destination) != before or file_sha256(file) != before:
                raise ValueError(f"复制期间输入发生变化：{name}")
        interpretation = {**PROFILE, "kind": "so100", "test_label": label,
                          "batch_label": str(request.get("label") or "").strip()}
        return PreparedInput(files=file_records(raw, names), interpretation=interpretation)

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
            "kind": "so100",
            "managed_path": str(managed),
            "input_fingerprint": manifest["input_fingerprint"],
            "manifest_path": str(managed / "input_manifest.json"),
            "import_run_id": run_dir.name,
            "test_label": request.get("test_label"),
        }
        source.update(metadata["source"], data_root=str(managed / "source"), required_rules=list(RULES))
        episodes = [{"episode_index": index, "row_count": int(metadata["episodes"][index]["length"])}
                    for index in metadata["episode_indices"]]
        return {"label": request.get("label", "本地导入批次"), "source": source, "episodes": episodes}


adapter = So100Adapter()
