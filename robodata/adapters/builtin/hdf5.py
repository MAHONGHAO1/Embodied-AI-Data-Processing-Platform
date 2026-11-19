"""Pinned robomimic HDF5 adapter."""

from __future__ import annotations

from pathlib import Path
import shutil

from ...sources import HDF5_SHA256, HDF5_SIZE, HDF5_SOURCE
from ...source import file_sha256
from ..contract import (INPUT_FILE, AdapterDescriptor, Detection, Preflight, PreparedInput,
                        SourceAdapter, rejected, supported)
from ..support import (HDF5_RULES, PROFILE, file_records, preview_worker, read_json)

ADAPTER_ID = "hdf5"


class Hdf5Adapter(SourceAdapter):
    descriptor = AdapterDescriptor(
        adapter_id=ADAPTER_ID,
        label="Robomimic HDF5（固定公开版本或明确标注的故障副本）",
        source_kind="hdf5",
        input_kind=INPUT_FILE,
        supported_versions=("robomimic-hdf5-pinned", "hdf5-test-copy"),
        pinned=True,
        supports_test_label=True,
        description="单个 .hdf5 文件；默认只接受固定 SHA-256，故障副本必须填写测试标记。",
    )

    def detect(self, path: Path, *, test_label: str | None = None) -> Detection:
        path = Path(path)
        if not path.is_file():
            return rejected(ADAPTER_ID, "输入不是文件；HDF5 适配器只接受单个 .hdf5/.h5 文件",
                            reasons=["输入路径不存在或不是文件"])
        if path.suffix.lower() not in {".hdf5", ".h5"}:
            return rejected(ADAPTER_ID, f"文件扩展名不是 .hdf5/.h5：{path.suffix or '(无)'}",
                            confidence=0.1, reasons=["扩展名不匹配"])
        size = path.stat().st_size
        if test_label:
            return supported(ADAPTER_ID, "hdf5-test-copy", 0.8,
                             [f"文件大小 {size} 字节", f"故障副本测试标记：{test_label}"],
                             {"size": size, "test_label": test_label})
        if size != HDF5_SIZE:
            return rejected(ADAPTER_ID, "HDF5 大小与固定公开版本不一致；若为故障副本，必须填写独立测试标记",
                            confidence=0.5, version="hdf5-local",
                            reasons=[f"实际 {size} 字节，固定版本 {HDF5_SIZE} 字节"], evidence={"size": size})
        actual = file_sha256(path)
        if actual != HDF5_SHA256:
            return rejected(ADAPTER_ID, "HDF5 SHA-256 与固定公开版本不一致；若为故障副本，必须填写独立测试标记",
                            confidence=0.5, version="hdf5-local",
                            reasons=["内容哈希不匹配"], evidence={"size": size, "sha256": actual})
        return supported(ADAPTER_ID, "robomimic-hdf5-pinned", 0.99,
                         ["文件大小与 SHA-256 均匹配固定公开版本"],
                         {"size": size, "sha256": actual})

    def preflight(self, path: Path, *, test_label: str | None = None) -> Preflight:
        detection = self.detect(path, test_label=test_label)
        path = Path(path)
        summary = {"format": "hdf5", "size": path.stat().st_size if path.is_file() else None,
                   "pinned_sha256": detection.detected_version == "robomimic-hdf5-pinned"}
        warnings = [] if detection.supported else [detection.unsupported_reason or ""]
        return Preflight(adapter_id=ADAPTER_ID, supported=detection.supported,
                         detected_version=detection.detected_version, summary=summary,
                         warnings=[item for item in warnings if item],
                         unsupported_reason=detection.unsupported_reason)

    def prepare(self, request: dict, raw: Path, managed: Path) -> PreparedInput:
        source = Path(request["input_path"]).resolve()
        label = request.get("test_label")
        if not source.is_file():
            raise FileNotFoundError("找不到待导入的 HDF5 文件")
        before = file_sha256(source)
        if not label and (before != HDF5_SHA256 or source.stat().st_size != HDF5_SIZE):
            raise ValueError("HDF5 与固定公开版本不一致；若为故障副本，必须填写独立测试标记")
        shutil.copy2(source, raw / "test.hdf5")
        if before != file_sha256(raw / "test.hdf5") or before != file_sha256(source):
            raise ValueError("复制期间 HDF5 输入发生变化，未登记批次")
        interpretation = {**PROFILE, "kind": "hdf5", "test_label": label,
                          "batch_label": str(request.get("label") or "").strip()}
        return PreparedInput(files=file_records(raw, ["test.hdf5"]), interpretation=interpretation)

    def preview(self, request: dict, managed: Path, run_dir: Path) -> dict:
        preview_worker(managed / "source/test.hdf5", managed / "preview", request.get("test_label"))
        files = [path.relative_to(managed / "preview").as_posix()
                 for path in (managed / "preview").rglob("*") if path.is_file()]
        from ..support import write_json
        write_json(managed / "preview_manifest.json", {"files": file_records(managed / "preview", files)})
        return {"preview_root": str(managed / "preview"),
                "metadata_path": str(managed / "preview/metadata.json")}

    def inventory(self, request: dict, managed: Path, run_dir: Path) -> dict:
        metadata = read_json(managed / "preview/metadata.json")
        source = {
            "kind": "hdf5",
            "managed_path": str(managed),
            "input_fingerprint": read_json(managed / "input_manifest.json")["input_fingerprint"],
            "manifest_path": str(managed / "input_manifest.json"),
            "import_run_id": run_dir.name,
            "test_label": request.get("test_label"),
        }
        source.update(**{key: value for key, value in HDF5_SOURCE.items() if key != "format_version"},
                      hdf5_path=str(managed / "source/test.hdf5"),
                      preview_root=str(managed / "preview"),
                      source_statistics=metadata["source_statistics"],
                      warnings=metadata.get("warnings", []),
                      required_rules=list(HDF5_RULES),
                      format_version="HDF5 (source)")
        episodes = [{"episode_index": int(item["episode_index"]), "row_count": int(item["row_count"])}
                    for item in metadata["episodes"]]
        return {"label": request.get("label", "本地导入批次"), "source": source, "episodes": episodes}


adapter = Hdf5Adapter()
