"""Presentation-only status semantics shared by batch and runtime views.

Execution completion and data acceptance are distinct: a completed check can
still reject its input. Only current, accepted completion of every supplied
business node makes an aggregate successful.
"""

from __future__ import annotations

from html import escape

import pandas as pd
from pandas.io.formats.style import Styler


_STATUS = {
    "completed": ("success", "已完成"),
    "passed": ("success", "通过"),
    "approved": ("success", "审核通过"),
    "success": ("success", "成功"),
    "running": ("running", "进行中"),
    "failed": ("error", "执行失败"),
    "failure": ("error", "失败"),
    "timed_out": ("error", "执行超时"),
    "interrupted": ("error", "已中断"),
    "completed_with_errors": ("error", "部分失败"),
    "execution_error": ("error", "执行异常"),
    "load_failed": ("error", "读取失败"),
    "rejected": ("warning", "未通过"),
    "issues": ("warning", "数据异常"),
    "incomplete": ("warning", "检查不完整"),
    "returned": ("warning", "已退回"),
    "blocked": ("warning", "暂不能继续"),
    "warning": ("warning", "警告"),
    "uncertain": ("warning", "不确定"),
    "ready": ("muted", "准备就绪"),
    "queued": ("muted", "等待启动"),
    "pending": ("muted", "待处理"),
    "waiting_upstream": ("muted", "等待上游"),
    "waiting_human": ("muted", "等待人工"),
    "not_started": ("muted", "尚未开始"),
    "not_checked": ("muted", "未检查"),
    "skipped": ("muted", "已跳过"),
    "superseded": ("muted", "已过期"),
    "cancelled": ("muted", "已取消"),
    "canceled": ("muted", "已取消"),
    "draft": ("muted", "草稿"),
    "submitted": ("muted", "已提交"),
    "unlabeled": ("muted", "尚未标注"),
    "unknown": ("muted", "未知状态"),
}

_CHINESE_TONES = {label: tone for tone, label in _STATUS.values()}
_CHINESE_TONES.update({
    "运行完成": "success", "执行完成": "success", "检查通过": "success",
    "运行中": "running", "执行中": "running", "处理中": "running",
    "运行失败": "error", "超时": "error", "中断": "error",
    "运行已中断": "error", "执行中断": "error", "运行结束，有节点失败": "error",
    "发现数据问题": "warning", "发现问题": "warning", "退回修改": "warning",
    "尚未检查": "muted", "等待执行": "muted", "未执行": "muted",
    "已就绪": "muted", "尚无结论": "muted", "等待下一步": "muted",
})

_CELL_STYLES = {
    "success": "color: #166534; background-color: #dcfce7; font-weight: 600;",
    "running": "color: #1d4ed8; background-color: #dbeafe; font-weight: 600;",
    "warning": "color: #9a3412; background-color: #ffedd5; font-weight: 600;",
    "error": "color: #991b1b; background-color: #fee2e2; font-weight: 600;",
    "muted": "color: #475569; background-color: #f1f5f9; font-weight: 600;",
}


def status_style(status: str) -> tuple[str, str]:
    """Return one of five fixed tones and a label; unknown states stay neutral."""
    value = str(status) if status is not None else ""
    if value in _STATUS:
        return _STATUS[value]
    if value in _CHINESE_TONES:
        return _CHINESE_TONES[value], value
    return "muted", value or "未知状态"


def status_badge(status: str, label: str | None = None) -> str:
    """Render safe badge markup without accepting a user-controlled CSS class."""
    tone, default_label = status_style(status)
    text = default_label if label is None else str(label)
    return f'<span class="rd-status rd-status-{tone}">{escape(text, quote=True)}</span>'


def aggregate_status(nodes: list[dict]) -> tuple[str, str]:
    """Summarize current node evidence, never infer success from missing states.

    Historical attempts must remain in each node's history, not be appended to
    this list. Execution errors outrank active work; an active run can mention
    unresolved data findings without relabeling those findings as run failures.
    """
    if not nodes:
        return "muted", "尚无流程状态"
    statuses = {node.get("status") for node in nodes}
    outcomes = {node.get("outcome") for node in nodes}
    if statuses & {"failed", "timed_out", "interrupted", "completed_with_errors", "execution_error", "load_failed"} or "execution_error" in outcomes:
        return "error", "存在执行失败"
    has_issues = bool(statuses & {"blocked", "rejected", "issues", "incomplete", "returned", "warning"}
                      or outcomes & {"rejected", "issues"})
    if "running" in statuses:
        return "running", "处理中，存在待处理问题" if has_issues else "处理中"
    if has_issues:
        return "warning", "存在待处理问题"
    if all(node.get("status") == "completed" and node.get("outcome") == "passed"
           for node in nodes):
        return "success", "流程已完成"
    if "superseded" in statuses:
        return "muted", "结果已过期，需重新处理"
    if statuses & {"cancelled", "canceled"}:
        return "muted", "存在已取消步骤"
    if any(node.get("status") == "completed" and node.get("outcome") != "passed" for node in nodes):
        return "muted", "执行已完成，结果待确认"
    if any(status not in _STATUS for status in statuses):
        return "muted", "状态待确认"
    return "muted", "等待下一步"


def styled_status_table(frame: pd.DataFrame) -> Styler:
    """Color exact Chinese status cells while preserving values and columns.

    Free text, identifiers, numbers and nullable cells remain untouched. This
    is a display wrapper; it does not rewrite the supplied dataframe.
    """
    def cell_style(value):
        if not isinstance(value, str):
            return ""
        tone = _CHINESE_TONES.get(value)
        return _CELL_STYLES[tone] if tone else ""

    return frame.style.map(cell_style)
