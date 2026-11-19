"""ROS 2 MCAP inspection adapter (inspection only).

There is no real MCAP sample or agreed topic mapping in the repository yet.
This adapter therefore reports topics/types and validates an optional RoboData
sidecar profile, but never guesses state/action/camera semantics and never
imports. Conversion is intentionally blocked until a real sample and a frozen
profile exist.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..contract import (INPUT_FILE, AdapterDescriptor, Detection, Preflight, PreparedInput,
                        SourceAdapter, rejected, supported)

ADAPTER_ID = "mcap"
VERSION = "mcap-inspection-v0"
PROFILE_NAME = "robodata_mcap.json"


def _sidecar(path: Path) -> dict | None:
    candidates = [path.with_suffix(".robodata.json"), path.parent / PROFILE_NAME]
    for candidate in candidates:
        if candidate.is_file():
            value = json.loads(candidate.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("schema_version") != 1:
                raise ValueError(f"{candidate} 不是 schema_version=1 的 MCAP 映射配置")
            return value
    return None


def _topics(path: Path) -> list[dict]:
    from mcap.reader import make_reader
    with path.open("rb") as stream:
        reader = make_reader(stream)
        summary = reader.get_summary()
        if summary is None:
            counts: dict[str, int] = {}
            schemas: dict[str, str | None] = {}
            for schema, channel, _message in reader.iter_messages():
                counts[channel.topic] = counts.get(channel.topic, 0) + 1
                schemas[channel.topic] = schema.name if schema else None
            return [{"topic": topic, "schema": schemas.get(topic), "messages": counts[topic]}
                    for topic in sorted(counts)]
        statistics = summary.statistics
        counts = statistics.channel_message_counts if statistics else {}
        topics = []
        for channel in summary.channels.values():
            schema = summary.schemas.get(channel.schema_id)
            topics.append({"topic": channel.topic, "schema": schema.name if schema else None,
                           "messages": int(counts.get(channel.id, 0))})
        return sorted(topics, key=lambda item: item["topic"])


class McapAdapter(SourceAdapter):
    descriptor = AdapterDescriptor(
        adapter_id=ADAPTER_ID,
        label="ROS 2 MCAP（检查）",
        source_kind=ADAPTER_ID,
        input_kind=INPUT_FILE,
        supported_versions=(VERSION,),
        pinned=False,
        supports_test_label=False,
        description="只读取 MCAP 的 topic/type/message 计数并校验可选 robodata_mcap.json；"
                    "不推断 state/action/相机语义，不支持导入或转换。",
        inspection_only=True,
    )

    def detect(self, path: Path, *, test_label: str | None = None) -> Detection:
        path = Path(path)
        if not path.is_file():
            return rejected(ADAPTER_ID, "输入不是文件", reasons=["MCAP 检查器只接受 .mcap 文件"])
        if path.suffix.lower() != ".mcap":
            return rejected(ADAPTER_ID, f"文件扩展名不是 .mcap：{path.suffix or '(无)'}",
                            confidence=0.1)
        try:
            topics = _topics(path)
        except ModuleNotFoundError:
            return rejected(ADAPTER_ID,
                            "识别到 .mcap，但未安装可选依赖 mcap/mcap-ros2-support；"
                            "执行 uv pip install -r examples/ros2_mcap/requirements.txt",
                            confidence=0.6, version=VERSION)
        except Exception as exc:
            return rejected(ADAPTER_ID, f"MCAP 无法读取：{exc}", confidence=0.2)
        if not topics:
            return rejected(ADAPTER_ID, "MCAP 没有可读 topic", confidence=0.4)
        try:
            profile = _sidecar(path)
        except (OSError, ValueError) as exc:
            return rejected(ADAPTER_ID, str(exc), confidence=0.5, version=VERSION)
        reasons = [f"识别到 {len(topics)} 个 topic"]
        if profile:
            reasons.append(f"发现映射配置：{profile.get('profile') or PROFILE_NAME}")
        else:
            reasons.append("未发现 robodata_mcap.json；不会推断机器人语义")
        return supported(ADAPTER_ID, VERSION, 0.95, reasons,
                         {"topics": topics, "profile_present": bool(profile)})

    def preflight(self, path: Path, *, test_label: str | None = None) -> Preflight:
        detection = self.detect(path, test_label=test_label)
        summary = dict(detection.evidence)
        warnings = []
        try:
            profile = _sidecar(Path(path))
        except (OSError, ValueError) as exc:
            profile = None
            warnings.append(str(exc))
        if profile:
            topics = {item["topic"] for item in summary.get("topics", [])}
            required = []
            for key in ("state", "action"):
                value = profile.get(key)
                if isinstance(value, dict) and value.get("topic"):
                    required.append((key, value["topic"]))
            for camera in profile.get("cameras") or []:
                if isinstance(camera, dict) and camera.get("topic"):
                    required.append((camera.get("name", "camera"), camera["topic"]))
            missing = [{"role": role, "topic": topic} for role, topic in required
                       if topic not in topics]
            summary["profile_missing_topics"] = missing
            if missing:
                warnings.append("映射配置引用了不存在的 topic")
        else:
            warnings.append("没有 topic 映射：检查通过也不能进入导入/转换")
        if not detection.supported:
            warnings.append(detection.unsupported_reason or "MCAP 检查不可用")
        return Preflight(adapter_id=ADAPTER_ID, supported=detection.supported,
                         detected_version=detection.detected_version, summary=summary,
                         warnings=warnings, unsupported_reason=detection.unsupported_reason)

    def prepare(self, request: dict, raw: Path, managed: Path) -> PreparedInput:
        raise ValueError("MCAP 当前只支持检查；导入/转换需要先冻结 topic 映射并使用真实样本")

    def preview(self, request: dict, managed: Path, run_dir: Path) -> dict:
        raise ValueError("MCAP 当前只支持检查，不生成预览")

    def inventory(self, request: dict, managed: Path, run_dir: Path) -> dict:
        raise ValueError("MCAP 当前只支持检查，不生成批次清单")


adapter = McapAdapter()
