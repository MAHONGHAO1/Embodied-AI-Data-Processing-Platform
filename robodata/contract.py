"""Write the acceptance conclusion that travels with a delivery.

docs/DATA_CONTRACT.md defines the shape. This module is the implementation:
it takes an assembled quality report, keeps the stable subset, and writes
QUALITY_REPORT.json plus a one-page QUALITY_REPORT.md.

The original report's result_digest is a digest of the FULL report and cannot
be recomputed from a subset, so the contract carries its own contract_digest
that anyone can recompute from the file itself.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

CONTRACT_VERSION = "1.0"

TOP_LEVEL_FIELDS = (
    "schema_version", "app_version", "rule_version", "params", "check_video",
    "input_fingerprint", "result_digest", "summary", "limitations", "issues",
)
SOURCE_FIELDS = ("source_kind", "manifest", "integrity_status", "integrity_note")

VERDICT_DELIVERABLE = "可交付"
VERDICT_REJECTED = "不通过"
VERDICT_INCOMPLETE = "结论不完整"


def _stable(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      allow_nan=False, separators=(",", ":"), default=str)


def digest_of(payload: dict) -> str:
    """SHA-256 over a deterministic serialization of a dict."""
    return hashlib.sha256(_stable(payload).encode("utf-8")).hexdigest()


def verdict(summary: dict) -> str:
    """Three answers, in the order defined by docs/DATA_CONTRACT.md."""
    if summary.get("data_issue_count", 0) > 0 or summary.get("load_failure_count", 0) > 0:
        return VERDICT_REJECTED
    if summary.get("not_checked_count", 0) > 0:
        return VERDICT_INCOMPLETE
    return VERDICT_DELIVERABLE


def build_contract(report: dict) -> dict:
    """Keep the stable subset and give it a recomputable digest."""
    contract = {key: report.get(key) for key in TOP_LEVEL_FIELDS}
    source = report.get("source") or {}
    contract["source"] = {key: source.get(key) for key in SOURCE_FIELDS}
    contract["contract_version"] = CONTRACT_VERSION
    contract["verdict"] = verdict(report.get("summary") or {})
    body = {key: value for key, value in contract.items() if key != "contract_digest"}
    contract["contract_digest"] = digest_of(body)
    return contract


def verify_contract(contract: dict) -> bool:
    """Recompute contract_digest from the file's own content."""
    expected = contract.get("contract_digest")
    if not expected:
        return False
    body = {key: value for key, value in contract.items() if key != "contract_digest"}
    return digest_of(body) == expected


def _markdown(contract: dict) -> str:
    summary = contract.get("summary") or {}
    lines = [
        f"# {contract.get('verdict', VERDICT_INCOMPLETE)}",
        "",
        f"判据版本 `{contract.get('rule_version')}` · 工作台 `{contract.get('app_version')}` · "
        f"契约 `{contract.get('contract_version')}`",
        "",
        "| 项目 | 数值 |",
        "| --- | --- |",
        f"| 任务数 | {summary.get('episode_count', 0)} |",
        f"| 样本行数 | {summary.get('row_count', 0)} |",
        f"| 数据异常 | {summary.get('data_issue_count', 0)} |",
        f"| 加载失败 | {summary.get('load_failure_count', 0)} |",
        f"| 未检查 | {summary.get('not_checked_count', 0)} |",
        f"| 视频检查 | {'已执行' if contract.get('check_video') else '未启用'} |",
        "",
    ]
    issues = [item for item in (contract.get("issues") or [])
              if item.get("category") != "not_checked"]
    if issues:
        lines += ["## 拦下的问题", ""]
        for item in issues[:20]:
            location = f"episode {item.get('episode_index')}"
            if item.get("row_index") is not None:
                location += f" 第 {item['row_index']} 行"
            lines.append(f"- `{item.get('rule')}`（{item.get('category')}）· {location} · {item.get('message')}")
        if len(issues) > 20:
            lines.append(f"- 另有 {len(issues) - 20} 条，见 `QUALITY_REPORT.json`")
        lines.append("")

    not_checked = [item for item in (contract.get("issues") or [])
                   if item.get("category") == "not_checked"]
    if not_checked:
        lines += ["## 没有检查到的项", ""]
        for item in not_checked:
            lines.append(f"- `{item.get('rule')}` · {item.get('message')}")
        lines.append("")

    limitations = contract.get("limitations") or []
    if limitations:
        lines += ["## 判据不覆盖的", ""]
        lines.extend(f"- {text}" for text in limitations)
        lines.append("")

    lines += [
        "## 校验",
        "",
        f"- 原始报告摘要：`{contract.get('result_digest')}`",
        f"- 本文件摘要：`{contract.get('contract_digest')}`",
        "- 去掉 `contract_digest` 后按固定顺序序列化取 SHA-256，应等于上面这条。",
        "",
        "结论口径与字段定义见 `docs/DATA_CONTRACT.md`，判据见 `docs/RULES.md`。",
    ]
    return "\n".join(lines) + "\n"


def write_contract(report: dict, dest: Path, filename_json: str = "QUALITY_REPORT.json",
                   filename_md: str = "QUALITY_REPORT.md") -> dict:
    """Write both files into dest. Returns their paths and the verdict."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    contract = build_contract(report)
    json_path = dest / filename_json
    md_path = dest / filename_md
    json_path.write_text(json.dumps(contract, ensure_ascii=False, indent=2,
                                    allow_nan=False, default=str) + "\n", encoding="utf-8")
    md_path.write_text(_markdown(contract), encoding="utf-8")
    return {"json": str(json_path), "md": str(md_path), "verdict": contract["verdict"]}
