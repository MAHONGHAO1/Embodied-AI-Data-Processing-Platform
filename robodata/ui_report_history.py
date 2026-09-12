"""Browse saved reports for the selected batch without rerunning its pipeline."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from . import ui_business, ui_runs
from .errors import explain_error


def report_entries(batch_id: str) -> list[dict]:
    entries = []
    for state in ui_runs.available_runs():
        request = state.get("request", {})
        if (state.get("batch_id") or request.get("batch_id")) != batch_id:
            continue
        artifacts = state.get("artifacts", {})
        candidates = [("最终数据质检", artifacts.get("final_quality_report")),
                      ("输出质检" if request.get("operation") in {"conversion", "final_quality"}
                       else "基础清洗检查", state.get("report_path") or artifacts.get("json"))]
        seen = set()
        for label, value in candidates:
            if not value:
                continue
            path = ui_runs.artifact_path(state, value)
            if str(path) in seen:
                continue
            seen.add(str(path))
            entries.append({"key": f"{state['run_id']}:{label}", "label": label,
                            "run_id": state["run_id"], "path": path,
                            "started_at": state.get("started_at", "未记录"),
                            "content_version": request.get("content_version", "未记录")})
    return entries


def render_history(batch: dict):
    st.subheader("历史质检报告")
    try:
        entries = report_entries(batch["batch_id"])
    except Exception as exc:
        st.warning(f"历史报告列表暂时无法读取：{explain_error(exc)}")
        return
    if not entries:
        st.info("本批次尚无已保存的历史报告。")
        return
    lookup = {e["key"]: e for e in entries}
    key = f"batch_report_history_{batch['batch_id']}"
    if st.session_state.get(key) not in lookup:
        st.session_state[key] = entries[0]["key"]
    chosen = st.selectbox("选择历史报告", list(lookup), key=key,
                          format_func=lambda k: f"{lookup[k]['label']} · {lookup[k]['started_at']} · {lookup[k]['run_id']}")
    entry = lookup[chosen]
    st.caption(f"原运行时间 {entry['started_at']} · 内容版本 {entry['content_version']} · {entry['run_id']}")
    st.button("查看该报告的节点与日志", key="batch_report_open_run",
              on_click=ui_business.open_run, args=(entry["run_id"],))
    st.caption("以下为当时保存的检查证据；当前版本是否可交付，以当前节点验证结果为准。")
    try:
        path = Path(entry["path"])
        raw = path.read_bytes()
        report = json.loads(raw)
        if not isinstance(report, dict) or not isinstance(report.get("summary"), dict):
            raise ValueError("报告结构不完整，缺少检查统计")
        summary = report["summary"]
        st.dataframe(pd.DataFrame([{"任务数": summary.get("episode_count"),
            "记录数": summary.get("row_count"), "问题数": summary.get("issue_count"),
            "已检查": summary.get("coverage_performed"), "应检查": summary.get("coverage_total")}]),
            hide_index=True, width="stretch")
        issues = report.get("issues", [])
        if not issues:
            issues = [issue for ep in report.get("episodes", []) for issue in ep.get("issues", [])]
        if issues:
            st.dataframe(pd.DataFrame([{"任务": i.get("episode_index"), "行位置": i.get("row_index"),
                "字段": i.get("field"), "问题": i.get("message"), "规则": i.get("rule")} for i in issues]),
                hide_index=True, width="stretch")
        with st.expander("完整历史报告"):
            st.json(report)
        st.download_button("下载历史报告 JSON", raw, f"{entry['run_id']}-{path.name}",
                           "application/json", key="batch_history_json")
        html = path.with_suffix(".html")
        if html.is_file():
            st.download_button("下载历史报告 HTML", html.read_bytes(), f"{entry['run_id']}-{html.name}",
                               "text/html", key="batch_history_html")
    except Exception as exc:
        st.error(f"历史报告无法读取：{explain_error(exc)}")
