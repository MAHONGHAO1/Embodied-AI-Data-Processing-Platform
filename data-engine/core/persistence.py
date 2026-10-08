"""
算子结果统一持久化层。
仅依赖标准库，将各领域算子的过程数据以 JSON 文件落盘，供跨库编排与审计复用。
严禁在本模块引入 torch / cv2 / ultralytics 等重型依赖。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# 持久化契约版本，变更 Schema 时递增
PERSISTENCE_SCHEMA_VERSION = "1.0"


class JsonResultPersister:
    """
    统一 JSON 结果持久化器。
    所有领域算子通过本类写出过程数据，保证文件名、元信息与 Schema 版本一致。
    """

    def __init__(self, schema_version: str = PERSISTENCE_SCHEMA_VERSION):
        self.schema_version = schema_version

    def save(
        self,
        operator: str,
        payload: Dict[str, Any],
        output_dir: str,
        filename: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        将算子结果写入 JSON 文件，返回落盘后的绝对路径。

        :param operator: 算子标识，如 ``perception.hand_pose_yolo`` ``clean.quality_lowlight``
        :param payload: 业务载荷（通常含 samples / events / stats 等）
        :param output_dir: 输出目录（绝对或相对路径均可，内部会 resolve）
        :param filename: 文件名（可省略 ``.json`` 后缀）
        :param meta: 可选元信息，将合并进顶层 ``meta`` 字段
        :return: JSON 文件的绝对路径字符串
        """
        out_dir = Path(output_dir).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        stem = filename if filename.endswith(".json") else f"{filename}.json"
        json_path = out_dir / stem

        document = self.build_document(operator=operator, payload=payload, meta=meta)
        with open(json_path, "w", encoding="utf-8") as file:
            json.dump(document, file, indent=2, ensure_ascii=False)

        return str(json_path)

    def build_document(
        self,
        operator: str,
        payload: Dict[str, Any],
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        组装符合统一 Schema 的文档对象（不落盘，便于单测与预览）。
        """
        merged_meta: Dict[str, Any] = {
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if meta:
            merged_meta.update(meta)

        return {
            "schema_version": self.schema_version,
            "operator": operator,
            "meta": merged_meta,
            **payload,
        }
