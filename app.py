"""Local data inspection and persistent runs for two fixed robot-data profiles."""

from __future__ import annotations

import html
import json
import os
from pathlib import Path
from contextlib import contextmanager

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from robodata.config import (
    APP_VERSION, DEFAULT_DATA_ROOT, DEFAULT_RULE_PARAMS, EPISODES, PROJECT_ROOT, REPO_ID, REVISION, RULE_VERSION,
)
from robodata.dataset import dataset_fingerprint, load_episode, load_metadata
from robodata.demo import create_failure_demo, read_demo_manifest
from robodata.errors import error_details, explain_error
from robodata.quality import RULE_LABELS
from robodata.reports import evidence_for_display, report_html, report_json
from robodata import ui_runs, ui_conversion, ui_business, ui_flow, ui_navigation
from robodata.ui_status import status_badge, styled_status_table
from robodata.sources import HDF5_SOURCE, MAPPING
from robodata.video import read_video_frame
from robodata.ui_charts import apply_signal_layout


st.set_page_config(page_title="RoboData Workbench v0.4.0 · 具身智能数据处理平台",
                   page_icon="◈", layout="wide")

PAGES = (*ui_navigation.BUSINESS_ROUTES, *ui_navigation.TECHNICAL_ROUTES, "运行记录")
COLORS = ("#0d9488", "#2563eb", "#8b5cf6", "#f59e0b", "#e76f51", "#64748b", "#db2777", "#65a30d", "#0891b2")
STATUS = {"passed": "检查通过", "issues": "发现数据问题", "incomplete": "检查不完整", "load_failed": "读取失败"}
CATEGORY = {"data_issue": "数据异常", "load_failure": "读取失败", "not_checked": "未检查"}
DATA_MODES = ("原始公开样本", "异常演示副本", "视频缺失演示副本", "已验证转换样本")

st.markdown("""
<style>
  .stApp { background: #f7f9fc; }
  .block-container { padding-top: 3.6rem; padding-bottom: 2rem; max-width: 1440px; }
  [data-testid="stSidebar"] { background: #ffffff; border-right: 1px solid #e2e8f0; }
  [data-testid="stMetric"] { background: #ffffff; padding: 16px 18px; border: 1px solid #e2e8f0; border-radius: 10px; }
  [data-testid="stMetricValue"] { color: #0f172a; font-size: 1.8rem; }
  [data-testid="stMetricValue"] > div { white-space: normal; overflow-wrap: anywhere; }
  h1 { font-size: 2rem !important; letter-spacing: -0.035em; color: #0f172a; }
  h2 { font-size: 1.25rem !important; color: #0f172a; }
  h3 { font-size: 1.05rem !important; color: #0f172a; }
  .brand { color: #0f172a; font-size: 1.45rem; font-weight: 750; letter-spacing: -.035em; }
  .brand span { color: #0d9488; }
  .stMain [data-testid="stVerticalBlockBorderWrapper"] {
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    box-shadow: 0 1px 2px rgba(15,23,42,.04);
  }
  .rd-context { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin: -4px 0 18px; color:#475569; font-size:.92rem; }
  .rd-context strong { color:#0f172a; }
  .rd-context .sep { color:#94a3b8; }
  .rd-status { display:inline-flex; align-items:center; gap:5px; border-radius:999px; padding:3px 10px; font-size:.82rem; font-weight:650; white-space:nowrap; }
  .rd-status-success { color:#166534; background:#dcfce7; }
  .rd-status-running { color:#1d4ed8; background:#dbeafe; }
  .rd-status-warning { color:#9a3412; background:#ffedd5; }
  .rd-status-error { color:#b91c1c; background:#fee2e2; }
  .rd-status-muted { color:#475569; background:#e2e8f0; }
  .rd-status-success::before { content:'✓'; }
  .rd-status-running::before { content:'●'; }
  .rd-status-warning::before { content:'!'; }
  .rd-status-error::before { content:'×'; }
  .rd-status-muted::before { content:'○'; }
  .rd-progress { margin:0 0 20px; padding:14px 16px; background:#fff; border:1px solid #e2e8f0; border-radius:12px; }
  .rd-progress-row { display:flex; align-items:stretch; }
  .rd-progress-step { flex:1 1 0; min-width:0; display:flex; flex-direction:column; gap:3px;
    padding:10px 12px; border:1px solid #e2e8f0; border-radius:10px; background:#f8fafc;
    color:#64748b; font-size:.82rem; text-decoration:none; transition:border-color .15s, background .15s, box-shadow .15s; }
  .rd-progress-step, .rd-progress-step * { text-decoration:none !important; }
  .rd-progress-step:hover { border-color:#94a3b8; background:#fff; text-decoration:none !important; }
  .rd-progress-num { font-size:.68rem; font-weight:700; letter-spacing:.06em; color:#94a3b8; line-height:1; }
  .rd-progress-name { font-weight:650; color:#475569; line-height:1.3; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .rd-progress-state { display:flex; align-items:center; gap:5px; font-size:.72rem; color:#94a3b8; line-height:1.3; }
  .rd-progress-state::before { content:''; width:7px; height:7px; border-radius:50%; background:#cbd5e1; flex:0 0 auto; }
  .rd-progress-arrow { flex:0 0 26px; display:flex; align-items:center; justify-content:center; color:#cbd5e1; }
  .rd-progress-arrow svg { width:16px; height:16px; display:block; }
  .rd-progress-step.selected { border-color:#0d9488; background:#f0fdfa; box-shadow:0 0 0 3px #ccfbf1; }
  .rd-progress-step.done { border-color:#86efac; background:#f0fdf4; }
  .rd-progress-step.done .rd-progress-num { color:#22c55e; }
  .rd-progress-step.done .rd-progress-name { color:#166534; }
  .rd-progress-step.done .rd-progress-state { color:#15803d; }
  .rd-progress-step.done .rd-progress-state::before { background:#16a34a; }
  .rd-progress-step.current { border-color:#93c5fd; background:#eff6ff; }
  .rd-progress-step.current .rd-progress-num { color:#2563eb; }
  .rd-progress-step.current .rd-progress-name { color:#1d4ed8; }
  .rd-progress-step.current .rd-progress-state { color:#1d4ed8; }
  .rd-progress-step.current .rd-progress-state::before { background:#2563eb; box-shadow:0 0 0 3px #dbeafe; }
  .rd-progress-step.alert { border-color:#fed7aa; background:#fff7ed; }
  .rd-progress-step.alert .rd-progress-name { color:#9a3412; }
  .rd-progress-step.alert .rd-progress-state::before { background:#f97316; }
  .rd-progress-step.failed { border-color:#fecaca; background:#fef2f2; }
  .rd-progress-step.failed .rd-progress-name { color:#b91c1c; }
  .rd-progress-step.failed .rd-progress-state::before { background:#dc2626; }
  @media (max-width:1180px) { .rd-progress-arrow { flex:0 0 18px; } .rd-progress-name { font-size:.78rem; } .rd-progress-state { font-size:.68rem; } }
  @media (max-width:860px) {
    .rd-progress-row { flex-wrap:wrap; gap:8px; }
    .rd-progress-arrow { display:none; }
    .rd-progress-step { flex:1 1 calc(50% - 4px); }
  }
  [data-testid="stSidebar"] .stRadio > label { font-weight:650; color:#334155; }
  [data-testid="stSidebar"] [role="radiogroup"] { gap:3px; }
  /* ===== 边界强化：st.expander 展开态可见（左色条 + 浅底 + 加粗） ===== */
  .streamlit-expander {
    border: 1px solid #e2e8f0 !important;
    border-radius: 10px;
    background: #ffffff;
    overflow: hidden;
    transition: border-color .15s, background .15s;
  }
  .streamlit-expander summary {
    padding: 10px 14px;
    font-weight: 600;
    color: #475569;
    border-left: 3px solid transparent;
    transition: background .15s, border-color .15s, color .15s;
  }
  .streamlit-expander summary:hover {
    background: #f8fafc;
    border-left-color: #cbd5e1;
  }
  .streamlit-expander[open] {
    background: #fbfdfe;
    border: 1px solid #99f6e4 !important;
  }
  .streamlit-expander[open] summary {
    background: #f0fdfa;
    border-left: 3px solid #0d9488;
    color: #0f766e;
    font-weight: 700;
  }
  .streamlit-expander .streamlit-expanderContent {
    padding: 2px 14px 14px;
  }
  /* ===== 边界强化：展开块按状态着色（左条 + 浅底随节点/运行状态） ===== */
  .rd-exp-flag { display: none; }
  .streamlit-expander:has(.rd-exp-flag-done) { border-color: #86efac !important; background: #f0fdf4; }
  .streamlit-expander:has(.rd-exp-flag-done)[open] summary { background: #f0fdf4; border-left: 3px solid #16a34a; color: #166534; font-weight: 700; }
  .streamlit-expander:has(.rd-exp-flag-running) { border-color: #93c5fd !important; background: #eff6ff; }
  .streamlit-expander:has(.rd-exp-flag-running)[open] summary { background: #eff6ff; border-left: 3px solid #2563eb; color: #1d4ed8; font-weight: 700; }
  .streamlit-expander:has(.rd-exp-flag-alert) { border-color: #fed7aa !important; background: #fff7ed; }
  .streamlit-expander:has(.rd-exp-flag-alert)[open] summary { background: #fff7ed; border-left: 3px solid #f97316; color: #9a3412; font-weight: 700; }
  .streamlit-expander:has(.rd-exp-flag-failed) { border-color: #fecaca !important; background: #fef2f2; }
  .streamlit-expander:has(.rd-exp-flag-failed)[open] summary { background: #fee2e2; border-left: 3px solid #dc2626; color: #b91c1c; font-weight: 700; }
  .streamlit-expander:has(.rd-exp-flag-muted) { border-color: #cbd5e1 !important; background: #f1f5f9; }
  .streamlit-expander:has(.rd-exp-flag-muted)[open] summary { background: #f1f5f9; border-left: 3px solid #94a3b8; color: #475569; font-weight: 700; }
  /* ===== 内部执行管线：把折叠的自动子步骤铺开成迷你管线 ===== */
  .rd-pipe { margin: 2px 0 18px; padding: 12px 14px; background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; }
  .rd-pipe-head { display: flex; align-items: baseline; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
  .rd-pipe-title { font-weight: 650; color: #0f172a; font-size: .95rem; }
  .rd-pipe-meta { font-size: .82rem; color: #64748b; }
  .rd-pipe-track { display: flex; flex-wrap: wrap; align-items: stretch; gap: 6px; }
  .rd-pipe-node { display: flex; flex-direction: column; gap: 2px; min-width: 92px;
    padding: 7px 10px 7px 12px; border: 1px solid #e2e8f0; border-left: 3px solid #cbd5e1;
    border-radius: 8px; background: #f8fafc; }
  .rd-pipe-idx { font-size: .68rem; font-weight: 700; letter-spacing: .06em; color: #94a3b8; line-height: 1; }
  .rd-pipe-name { font-size: .8rem; font-weight: 600; color: #475569; white-space: nowrap; }
  .rd-pipe-time { font-size: .7rem; color: #94a3b8; font-variant-numeric: tabular-nums; }
  .rd-pipe-node.done { border-left-color: #16a34a; background: #f0fdf4; }
  .rd-pipe-node.done .rd-pipe-name { color: #166534; }
  .rd-pipe-node.running { border-left-color: #2563eb; background: #eff6ff; }
  .rd-pipe-node.running .rd-pipe-name { color: #1d4ed8; }
  .rd-pipe-node.alert { border-left-color: #f97316; background: #fff7ed; }
  .rd-pipe-node.alert .rd-pipe-name { color: #9a3412; }
  .rd-pipe-node.failed { border-left-color: #dc2626; background: #fef2f2; }
  .rd-pipe-node.failed .rd-pipe-name { color: #b91c1c; }
  .rd-pipe-node.muted { border-left-color: #94a3b8; background: #f1f5f9; }
  .rd-pipe-link { flex: 0 0 12px; align-self: center; height: 2px; background: #cbd5e1; border-radius: 2px; }
</style>
""", unsafe_allow_html=True)


@st.cache_data(show_spinner=False, max_entries=12)
def cached_metadata(root: str, fingerprint: str):
    return load_metadata(Path(root))


@st.cache_data(show_spinner=False, max_entries=20)
def cached_episode(root: str, episode_index: int, fingerprint: str):
    return load_episode(Path(root), episode_index)


@st.cache_data(show_spinner=False, max_entries=48)
def cached_frame(path: str, timestamp: float, fingerprint: str):
    return read_video_frame(Path(path), timestamp)


def jump_to_issue(episode_index: int, row_index: int):
    st.session_state["page"] = "任务浏览"
    st.session_state["navigation_pending"] = True
    st.session_state["selected_episode"] = int(episode_index)
    st.session_state["selected_row"] = int(row_index)


def move_row(row_index: int):
    st.session_state["selected_row"] = int(row_index)


def reset_row():
    st.session_state["selected_row"] = 0


def reset_data_mode():
    st.session_state["selected_episode"] = EPISODES[0]
    reset_row()
    st.session_state.pop("quality_report", None)
    st.session_state.pop("selected_run_id", None)
    st.session_state.pop("history_data_root", None)
    st.session_state.pop("active_episode_indices", None)
    st.query_params.pop("run_id", None)
    for key in ("issue_browser", "issue_report"):
        st.session_state.pop(key, None)


def show_error(context: str, exc: Exception, *, warning: bool = False):
    message = f"{context}：{explain_error(exc)}"
    (st.warning if warning else st.error)(message)
    with status_expander("原始错误详情（排查用）", status="failed"):
        details = error_details(exc)
        st.code(json.dumps(details, ensure_ascii=False, indent=2), language=None)


@contextmanager
def status_expander(label: str, status: str | None = None, expanded: bool = False):
    """st.expander 包装：展开态（及收起态）按节点/运行状态着色。
    status ∈ {done, running, alert, failed, muted, None}；None 走默认主色（Step 2）。
    通过注入隐藏标记 + CSS :has() 实现，无需自定义组件。"""
    with st.expander(label, expanded=expanded) as box:
        if status:
            st.markdown(
                f'<span class="rd-exp-flag rd-exp-flag-{status}" aria-hidden="true"></span>',
                unsafe_allow_html=True,
            )
        yield box


INTERNAL_STAGE_SHORT = {
    "copy": "复制校验", "preview": "原始预览", "inventory": "清单核对",
    "import_registration": "批次登记", "quality": "基础质检", "registration": "结果回写",
    "final_quality": "最终质检", "final_registration": "质检登记",
    "conversion_registration": "转换登记", "source_verify": "源完整性",
    "file_validation": "文件校验", "hdf5_validation": "HDF5 检查",
    "field_mapping": "字段映射", "format_write": "格式写入",
    "official_verify": "官方验证", "output_quality": "输出质检",
    "package": "生成 ZIP", "verify": "解包校验", "archive_verify": "交付包验证",
}


def _pipeline_tone(status: str, data_issues) -> str:
    """节点执行状态 → 管线色阶（与进度条/展开块同一套四色体系）。"""
    if status in {"failed", "timed_out", "interrupted"}:
        return "failed"
    if status == "running":
        return "running"
    if status in {"completed", "passed", "registered", "done", "success"}:
        return "alert" if int(data_issues or 0) > 0 else "done"
    return "muted"


def _internal_stage_list(request: dict) -> list[str]:
    """取一次运行实际会经过的有序内部子步骤（业务/转换运行才有）。"""
    kind = request.get("kind")
    try:
        if kind == "business":
            from robodata.business import BUSINESS_STAGES, business_stages
            operation = request.get("operation")
            if operation not in BUSINESS_STAGES:
                return []
            return list(business_stages(operation, request))
        if kind == "conversion":
            from robodata.conversion import CONVERSION_STAGES
            return list(CONVERSION_STAGES)
    except Exception:
        return []
    return []


def _pipeline_html(state: dict) -> str | None:
    """构造迷你管线 HTML；纯函数，便于单测。无内部子步骤时返回 None。"""
    request = state.get("request", {})
    stages = _internal_stage_list(request)
    if not stages:
        return None
    by_stage = {n["node_id"]: n for n in state.get("nodes", []) if n.get("episode_index") is None}
    cells, done, running, bad = [], 0, 0, 0
    for index, stage in enumerate(stages, 1):
        node = by_stage.get(stage, {})
        tone = _pipeline_tone(node.get("status", "pending"), node.get("data_issue_count", 0))
        done += tone == "done"
        running += tone == "running"
        bad += tone in {"failed", "alert"}
        name = INTERNAL_STAGE_SHORT.get(stage) or node.get("label") or stage
        try:
            elapsed = float(node.get("elapsed_seconds") or 0)
        except (TypeError, ValueError):
            elapsed = 0.0
        clock = f'<span class="rd-pipe-time">{elapsed:.2f}s</span>' if elapsed > 0 else ""
        cells.append(
            f'<div class="rd-pipe-node {tone}"><span class="rd-pipe-idx">{index:02d}</span>'
            f'<span class="rd-pipe-name">{html.escape(str(name))}</span>{clock}</div>')
        if index < len(stages):
            cells.append('<span class="rd-pipe-link"></span>')
    meta = f"共 {len(stages)} 步 · 完成 {done}"
    if running:
        meta += f" · 运行中 {running}"
    if bad:
        meta += f" · 失败/异常 {bad}"
    return (
        '<div class="rd-pipe"><div class="rd-pipe-head">'
        '<span class="rd-pipe-title">内部执行管线</span>'
        f'<span class="rd-pipe-meta">{meta}</span></div>'
        '<div class="rd-pipe-track">' + "".join(cells) + '</div></div>')


def render_internal_pipeline(state: dict) -> None:
    """把一次运行内部折叠的自动子步骤铺开成一条迷你管线，让自动化深度可见。"""
    markup = _pipeline_html(state)
    if markup:
        st.markdown(markup, unsafe_allow_html=True)


def quality_provenance(report: dict):
    st.caption(
        f"结果生成于 {report.get('started_at_utc', '未记录')} · "
        f"运行 {float(report.get('runtime_seconds', 0)):.2f} 秒 · "
        f"规则 {report.get('rule_version', RULE_VERSION)}。正在查看该次运行保存的报告，未重新执行。"
    )


def issue_table(issues: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "任务": f"Episode {i.get('episode_index', '—')}",
            "行位置": i.get("row_index"), "帧索引": i.get("frame_index"),
            "类别": CATEGORY.get(i.get("category"), i.get("category", "—")),
            "规则": RULE_LABELS.get(i.get("rule"), i.get("rule", "—")), "说明": i.get("message", "—"),
        }
        for i in issues
    ])


def draw_signal(table: pd.DataFrame, field: str, names: list[str], row: int, title: str):
    if field not in table or "timestamp" not in table:
        st.warning(f"{title}所需字段缺失，无法绘图。")
        return
    try:
        values = np.stack(table[field].to_numpy()).astype(float)
        times = np.asarray(table["timestamp"], dtype=float)
        if times.ndim != 1:
            raise ValueError("记录时间必须为一维标量序列，请查看数值有效性质检结果")
        if values.ndim != 2 or values.shape[1] != len(names):
            raise ValueError("数组维度与字段说明不一致")
    except (ValueError, TypeError) as exc:
        show_error(f"{title}无法绘制", exc, warning=True)
        return
    fig = go.Figure()
    for i, name in enumerate(names):
        fig.add_trace(go.Scatter(
            x=times, y=values[:, i], name=name, mode="lines",
            line={"width": 1.6, "color": COLORS[i % len(COLORS)]},
            hovertemplate="%{x:.4f} s<br>%{y:.4f}<extra>%{fullData.name}</extra>",
        ))
    if np.isfinite(times[row]):
        fig.add_vline(x=float(times[row]), line_color="#0f172a", line_dash="dot", line_width=1.5)
    apply_signal_layout(fig, x_title="记录时间 / s")
    st.subheader(title)
    st.plotly_chart(fig, width="stretch", key=f"plot_{field}")


def show_overview(root: Path, fingerprint: str, metadata: dict, report: dict | None):
    st.title("数据概览")
    source = metadata.get("source", {})
    indices = metadata.get("episode_indices", list(EPISODES))
    info = metadata.get("info", {})
    loaded, failures = [], []
    for ep_id in indices:
        try:
            loaded.append(cached_episode(str(root), ep_id, fingerprint))
        except Exception as exc:
            failures.append((ep_id, exc))
    columns = st.columns(4)
    columns[0].metric("可读取任务", f"{len(loaded)} / {len(indices)}")
    columns[1].metric("实际记录帧数", f"{sum(len(ep.table) for ep in loaded):,}")
    columns[2].metric("主相机可用任务", f"{sum(ep.video_path.is_file() for ep in loaded)} / {len(indices)}")
    columns[3].metric("来源采样频率", f"{metadata.get('info', {}).get('fps', '—')} Hz")
    st.write("")
    left, right = st.columns([1.7, 1])
    with left, st.container(border=True):
        st.subheader("任务清单")
        statuses = {ep["episode_index"]: STATUS.get(ep.get("status"), ep.get("status", "—")) for ep in (report or {}).get("episodes", [])}
        rows = []
        for ep in loaded:
            details = ep.metadata.get("episode", {})
            task_names = details.get("tasks", [])
            if isinstance(task_names, np.ndarray):
                task_names = task_names.tolist()
            rows.append({
                "任务": f"Episode {ep.episode_index}", "帧数": len(ep.table),
                "来源预期帧数": ep.expected_length,
                "任务描述": " / ".join(task_names) if isinstance(task_names, list) else str(task_names),
                "质检状态": statuses.get(ep.episode_index, "尚未检查"),
            })
        if rows:
            st.dataframe(styled_status_table(pd.DataFrame(rows)), width="stretch", hide_index=True)
        else:
            st.info("尚无可读取的数据表。请先获取固定样本。")
        for ep_id, reason in failures:
            show_error(f"任务 {ep_id} 读取失败", reason, warning=True)
    with right, st.container(border=True):
        st.subheader("这份样本包含什么")
        state_shape = info.get("features", {}).get("observation.state", {}).get("shape", ["—"])
        action_shape = info.get("features", {}).get("action", {}).get("shape", ["—"])
        camera = source.get("camera", "observation.images.laptop").split(".")[-1]
        st.markdown(f"**主相机图像** · {camera}\n\n**状态** · {state_shape[0]} 维\n\n**动作** · {action_shape[0]} 维\n\n**任务索引与时间** · 按 episode 组织")
        st.caption(source.get("timestamp_semantics", "来源未完整说明单位及控制含义，保留原值；时间检查不代表硬件同步验证。"))
        if source.get("format_version") == "v3.0":
            st.caption("状态由指定来源字段顺序拼接；动作沿用源数组，未进行坐标变换或重新定义控制语义。")
        st.link_button("查看公开数据来源 ↗", source.get("url", f"https://huggingface.co/datasets/{REPO_ID}/tree/{REVISION}"), width="stretch")
    with st.container(border=True):
        st.subheader("检查覆盖")
        if report is None:
            st.info(f"尚未生成质检结果。点击左侧「运行基础质检」，检查这 {len(indices)} 条任务。")
        else:
            summary = report["summary"]
            st.write(f"已执行 {summary.get('coverage_performed', 0)} / {summary.get('coverage_total', 0)} 项检查；"
                     f"发现 {summary.get('data_issue_count', 0)} 条数据异常、"
                     f"{summary.get('load_failure_count', 0)} 条读取失败、"
                     f"{summary.get('not_checked_count', 0)} 条未检查说明。")
            quality_provenance(report)
    with st.expander("来源与字段说明"):
        st.write(f"数据集：{source.get('repo_id', REPO_ID)}")
        st.code(source.get("revision", REVISION), language=None)
        st.caption(f"实际输入指纹：{fingerprint}")
        st.dataframe(pd.DataFrame([
            {"字段": "observation.state", "含义": f"{info.get('features', {}).get('observation.state', {}).get('shape')}；名称与维度来自元数据"},
            {"字段": "action", "含义": f"{info.get('features', {}).get('action', {}).get('shape')}；独立于状态维度，保留来源顺序"},
            {"字段": "timestamp", "含义": "任务内记录时间（秒）；用于连续性和视频 PTS 对应检查"},
            {"字段": "frame_index / episode_index", "含义": "任务内帧索引 / 任务编号"},
        ]), hide_index=True, width="stretch")


def show_browser(root: Path, fingerprint: str, report: dict | None):
    st.title("任务浏览")
    indices = st.session_state.get("active_episode_indices", list(EPISODES))
    if st.session_state.get("selected_episode") not in indices:
        st.session_state["selected_episode"] = indices[0]
        reset_row()
    st.selectbox("选择任务", indices, key="selected_episode", format_func=lambda x: f"Episode {x:03d}", on_change=reset_row)
    ep_id = st.session_state["selected_episode"]
    try:
        ep = cached_episode(str(root), ep_id, fingerprint)
    except Exception as exc:
        show_error(f"任务 {ep_id} 无法读取", exc)
        st.info("可选择其他任务继续浏览；质检报告会保留读取失败记录。")
        return
    count = len(ep.table)
    if count == 0:
        st.warning("此任务没有记录，无法选择帧。")
        return
    st.session_state["selected_row"] = min(max(int(st.session_state.get("selected_row", 0)), 0), count - 1)
    a, b, c, spacer = st.columns([1, 1, 1, 5])
    a.button("首帧", on_click=move_row, args=(0,), width="stretch")
    b.button("中间帧", on_click=move_row, args=(count // 2,), width="stretch")
    c.button("末帧", on_click=move_row, args=(count - 1,), width="stretch")
    st.slider("记录位置（从 0 开始）", min_value=0, max_value=max(1, count - 1), key="selected_row", disabled=count < 2)
    row = int(st.session_state["selected_row"])
    record = ep.table.iloc[row]
    timestamp = record.get("timestamp")
    left, right = st.columns([1.75, 1])
    with left, st.container(border=True):
        camera_name = ep.source.get("camera", "observation.images.laptop").split(".")[-1]
        st.subheader(f"主相机 · {camera_name}")
        try:
            timestamp = float(timestamp)
            if not np.isfinite(timestamp) or timestamp < 0:
                raise ValueError("当前记录的时间无效")
            video_start = float(getattr(ep, "video_start_time", 0.0))
            frame = cached_frame(str(ep.video_path), timestamp + video_start, fingerprint)
            st.image(frame["image"], width="stretch")
            error = float(frame["timestamp_error"])
            st.caption(f"任务时间 {timestamp:.6f} s · 文件偏移 {video_start:.6f} s · 视频 PTS {float(frame['pts_time']):.6f} s · 差值 {error * 1000:+.3f} ms")
            if abs(error) > 0.5 / ep.fps + 0.00001:
                st.warning("视频帧与目标时间的差值超过半个采样周期，请结合质检结果判断。")
        except Exception as exc:
            show_error("本帧图像无法读取", exc)
            st.caption("当前显示为空；未使用替代图片。状态与动作仍可单独检查。")
    with right, st.container(border=True):
        st.subheader("当前位置")
        st.metric("任务内记录", f"{row + 1} / {count}")
        st.write(f"**帧索引**　{record.get('frame_index', '缺失')}")
        st.write(f"**记录时间**　{timestamp} s")
        try:
            state_names = getattr(ep, "state_names", ep.field_names)
            action_names = getattr(ep, "action_names", ep.field_names)
            st.dataframe(pd.DataFrame({"状态字段": state_names, "原始状态": record["observation.state"]}), width="stretch", hide_index=True)
            st.dataframe(pd.DataFrame({"动作字段": action_names, "原始动作": record["action"]}), width="stretch", hide_index=True)
        except (KeyError, ValueError, TypeError):
            st.warning("当前状态或动作字段不完整，无法逐维展示。")
    with st.container(border=True):
        draw_signal(ep.table, "observation.state", getattr(ep, "state_names", ep.field_names), row, "原始状态值")
        draw_signal(ep.table, "action", getattr(ep, "action_names", ep.field_names), row, "原始动作值")
        st.caption("保留来源数值；不推断关节单位、坐标系或动作是绝对量还是增量。")
    with st.container(border=True):
        st.subheader("此任务的检查结果")
        if report is None:
            st.info("尚未运行质检。左侧运行后，可在这里查看问题及定位信息。")
        else:
            issues = [i for i in report.get("issues", []) if i.get("episode_index") == ep_id]
            if issues:
                st.dataframe(issue_table(issues), width="stretch", hide_index=True)
                issue_jump_control(issues, "browser")
            else:
                st.success("已执行的规则未发现此任务的问题。检查范围见质检报告。")


def issue_jump_control(issues: list[dict], key: str):
    indices = st.session_state.get("active_episode_indices", list(EPISODES))
    locatable = [i for i in issues if i.get("episode_index") in indices and i.get("row_index") is not None]
    other = [i for i in issues if i.get("episode_index") not in indices or i.get("row_index") is None]
    choices = locatable + other
    if not choices:
        return
    selected = st.selectbox(
        "选择需要定位的问题", range(len(choices)), key=f"issue_{key}",
        format_func=lambda x: f"任务 {choices[x]['episode_index']} · "
        + (f"行 {choices[x]['row_index']}" if choices[x].get('row_index') is not None else "文件或任务级问题")
        + f" · {choices[x].get('message', '')}",
    )
    issue = choices[selected]
    if issue.get("row_index") is not None and issue.get("episode_index") in indices:
        allowed = st.session_state.get("report_matches_input", False)
        st.button("跳转定位", key=f"jump_{key}", on_click=jump_to_issue, args=(issue["episode_index"], issue["row_index"]), disabled=not allowed)
        if not allowed:
            st.caption("当前文件与本报告记录的输入不一致或无法读取；为避免错位，不跳转到当前数据。")
    else:
        st.caption("此问题对应文件或整条任务，不能跳转到具体行；可查看下方证据，或到任务浏览中选择该任务。")
    with st.expander("所选问题的证据"):
        st.json(evidence_for_display(issue.get("evidence", {})))
    if issue.get("evidence", {}).get("diagnostics"):
        with st.expander("原始错误详情（排查用）"):
            st.json(issue["evidence"]["diagnostics"])


def show_report(report: dict | None):
    st.title("质检报告")
    if report is None:
        st.info("尚未生成报告。点击左侧「运行基础质检」开始。")
        return
    summary = report["summary"]
    columns = st.columns(4)
    columns[0].metric("数据异常", summary.get("data_issue_count", 0))
    columns[1].metric("读取失败", summary.get("load_failure_count", 0))
    columns[2].metric("未检查项", summary.get("not_checked_count", 0))
    columns[3].metric("已执行检查", f"{summary.get('coverage_performed', 0)} / {summary.get('coverage_total', 0)}")
    quality_provenance(report)
    with st.container(border=True):
        st.subheader("任务状态与检查覆盖")
        entries = []
        for ep in report.get("episodes", []):
            coverage = ep.get("coverage", [])
            entries.append({
                "任务": f"Episode {ep['episode_index']}", "状态": STATUS.get(ep.get("status"), ep.get("status", "—")),
                "读取帧数": ep.get("row_count", 0),
                "已执行检查": sum(c.get("status") == "performed" for c in coverage),
                "检查总项数": len(coverage), "问题条数": len(ep.get("issues", [])),
            })
        st.dataframe(styled_status_table(pd.DataFrame(entries)), hide_index=True, width="stretch")
        with st.expander("逐项检查范围"):
            records = [{"任务": ep["episode_index"], "规则": RULE_LABELS.get(c.get("rule"), c.get("rule")),
                        "状态": "已执行" if c.get("status") == "performed" else "未检查", "说明": c.get("detail", "")}
                       for ep in report.get("episodes", []) for c in ep.get("coverage", [])]
            st.dataframe(pd.DataFrame(records), hide_index=True, width="stretch")
    with st.container(border=True):
        st.subheader("问题明细")
        issues = report.get("issues", [])
        left, right = st.columns(2)
        chosen_ep = left.selectbox("按任务筛选", ["全部任务"] + [ep["episode_index"] for ep in report.get("episodes", [])], format_func=lambda x: x if isinstance(x, str) else f"Episode {x}")
        rules = sorted({i.get("rule", "unknown") for i in issues})
        chosen_rule = right.selectbox("按规则筛选", ["全部规则"] + rules, format_func=lambda x: RULE_LABELS.get(x, x))
        selected = [i for i in issues if (chosen_ep == "全部任务" or i.get("episode_index") == chosen_ep)
                    and (chosen_rule == "全部规则" or i.get("rule") == chosen_rule)]
        if selected:
            st.dataframe(issue_table(selected), width="stretch", hide_index=True)
            issue_jump_control(selected, "report")
        elif issues:
            st.info("当前筛选条件下没有问题。")
        else:
            st.success("已执行规则未发现问题。此结果仅覆盖报告列出的检查项。")
    with st.container(border=True):
        st.subheader("导出与追溯")
        a, b, _ = st.columns([1, 1, 2])
        a.download_button("下载 HTML 报告", report_html(report), "robodata-quality-report.html", "text/html", width="stretch")
        b.download_button("下载 JSON 报告", report_json(report), "robodata-quality-report.json", "application/json", width="stretch")
        with st.expander("查看来源、参数与结果指纹"):
            st.json({key: report.get(key) for key in ("source", "input_fingerprint", "rule_version", "params", "result_digest")})


def selected_run() -> dict | None:
    run_id = st.session_state.get("selected_run_id")
    return ui_runs.load_run(run_id) if run_id else None


def registered_output_for_run(state: dict) -> dict | None:
    recorded = state.get("output_path") or state.get("request", {}).get("output_path") or state.get("request", {}).get("export_path")
    for item in ui_conversion.registered_exports():
        if item.get("run_id") == state["run_id"]:
            return item
        if recorded and ui_conversion.export_path(item) == Path(recorded).resolve():
            return item
    return None


def choose_history():
    run_id = st.session_state.get("history_picker")
    if not run_id:
        return
    state = ui_runs.load_run(run_id)
    request = state.get("request", {})
    st.session_state["selected_run_id"] = run_id
    st.query_params["run_id"] = run_id
    if request.get("kind") == "business":
        st.session_state["business_run_id"] = run_id
        batch_id = state.get("batch_id") or request.get("batch_id")
        if batch_id:
            st.session_state["selected_batch_id"] = batch_id
        st.session_state.pop("history_data_root", None)
        return
    st.session_state["history_data_root"] = request.get("data_root")
    # CLI requests may retain their default kind while the quality runner detects
    # the actual source. Prefer its persisted evidence, even after input damage.
    try:
        saved_report = ui_runs.load_report(state) or {}
        saved_source = saved_report.get("source", {})
    except Exception:
        saved_source = {}
    source_kind = saved_source.get("source_kind") or request.get("source_kind")
    if request.get("source_kind") == "injected_test":
        source_kind = "injected_test"
    demo = saved_source.get("demo_manifest") or {}
    if not demo and not saved_source.get("source_kind") and request.get("data_root"):
        try:
            demo = read_demo_manifest(Path(request["data_root"])) or {}
        except Exception:
            demo = {}
    if demo.get("source_kind") == "injected_test":
        source_kind = "injected_test"
    mode = DATA_MODES[0]
    if request.get("kind") == "conversion":
        mode = DATA_MODES[3]
        registered = registered_output_for_run(state)
        st.session_state["history_data_root"] = str(ui_conversion.export_path(registered)) if registered else None
    elif source_kind == "injected_test":
        missing_video = demo.get("scenario") == "missing_video" or "missing_video" in str(request.get("data_root", ""))
        mode = DATA_MODES[2] if missing_video else DATA_MODES[1]
    elif source_kind == "converted_public" or saved_source.get("format_version") == "v3.0":
        mode = DATA_MODES[3]
    st.session_state["data_mode"] = mode
    st.session_state["interval_tolerance"] = float(request.get("params", {}).get("interval_relative_tolerance", 0.1)) * 100
    st.session_state["default_timeout"] = float(request.get("timeouts", {}).get("default", 60))
    st.session_state["video_timeout"] = float(request.get("timeouts", {}).get("video_decode", 120))
    st.session_state["execution_scenario"] = "运行超时演示" if request.get("execution_demo") == "video_timeout" else "正常运行"
    reset_row()


def start_requested_run(root: str, params: dict, mode: str, scenario: str, timeouts: dict):
    try:
        run_id = ui_runs.start_quality(Path(root), params,
            source_kind="injected_test" if mode in DATA_MODES[1:3] else "converted_public" if mode == DATA_MODES[3] else "original_public",
            execution_demo="video_timeout" if scenario == "运行超时演示" else "none", timeouts=timeouts)
        st.session_state["selected_run_id"] = run_id
        st.session_state["history_picker"] = run_id
        st.session_state["history_data_root"] = root
        st.session_state["page"] = "运行记录"
        st.session_state.pop("quality_report", None)
        st.query_params["run_id"] = run_id
    except Exception as exc:
        st.session_state["start_error"] = exc


def report_source_notice(report: dict):
    source = report.get("source", {})
    if source.get("source_kind") == "injected_test":
        st.warning("这是明确标注的故障注入测试副本，结果不代表原始公开样本。")
    integrity = source.get("integrity_status")
    if integrity != "matches_manifest":
        notice = "本次运行的输入文件与下载清单不一致。" if integrity == "differs_from_manifest" else "本次运行的来源一致性尚未核验。"
        st.warning(notice + " " + source.get("integrity_note", ""))


def set_page(page: str):
    st.session_state["page"] = page
    st.session_state["navigation_pending"] = True


def start_conversion_run():
    try:
        run_id = ui_conversion.start_conversion()
        st.session_state["selected_run_id"] = run_id
        st.session_state["history_picker"] = run_id
        st.session_state["data_mode"] = DATA_MODES[3]
        st.session_state["execution_scenario"] = "正常运行"
        st.session_state["page"] = "运行记录"
        st.session_state.pop("history_data_root", None)
        st.session_state.pop("quality_report", None)
        st.query_params["run_id"] = run_id
    except Exception as exc:
        st.session_state["start_error"] = exc


def switch_to_export(export: dict):
    st.session_state["data_mode"] = DATA_MODES[3]
    st.session_state["history_data_root"] = str(ui_conversion.export_path(export))
    st.session_state["selected_run_id"] = export["run_id"]
    st.session_state["history_picker"] = export["run_id"]
    st.session_state["selected_episode"] = 0
    st.session_state["selected_row"] = 0
    st.session_state["page"] = "数据概览"
    st.query_params["run_id"] = export["run_id"]


def open_run_report(run_id: str):
    st.session_state["history_picker"] = run_id
    choose_history()
    st.session_state["page"] = "质检报告"
    st.session_state["navigation_pending"] = True


def show_conversion_page():
    st.title("公开样本转换")
    left, right = st.columns([1.6, 1])
    with left, st.container(border=True):
        st.subheader("来源与字段映射")
        st.write(f"**{HDF5_SOURCE['repo_id']}** · {HDF5_SOURCE.get('robot', '')}")
        st.caption(f"固定来源版本 {HDF5_SOURCE['revision']} · {HDF5_SOURCE['license']}")
        st.dataframe(pd.DataFrame(MAPPING), hide_index=True, width="stretch")
        st.link_button("查看 HDF5 公开来源 ↗", HDF5_SOURCE["url"])
    with right, st.container(border=True):
        st.subheader("本次转换配置")
        st.write("**任务**　demo_0、demo_1、demo_2")
        st.write("**状态 / 动作**　9 维 / 7 维")
        st.write("**相机**　agentview · 84 × 84 RGB")
        st.write("**目标数据格式**　LeRobot v3.0")
    st.warning("源文件的全局统计与逐任务实际统计不一致。运行会同时记录二者，以选中任务的实际内容构建输出，不修改源文件。")
    st.info("时间按 control_freq=20 派生：timestamp = frame_index / 20。它不是传感器硬件时间戳；状态与动作使用来源定义，不推断新的控制含义。视频编码有损，映射验证将单独记录图像误差。")
    sample_ready = ui_conversion.input_path().is_file()
    env_ready = ui_conversion.environment_python().is_file()
    with st.container(border=True):
        st.subheader("开始一个独立转换运行")
        st.write(f"HDF5：{'文件已存在，运行时核对 SHA-256' if sample_ready else '尚未获取'}")
        st.caption(str(ui_conversion.input_path()))
        if not sample_ready:
            st.code("uv run robodata fetch-hdf5", language="powershell")
        st.write(f"转换环境：{'独立解释器已存在，运行时验证依赖' if env_ready else '尚未准备'}")
        if not env_ready:
            st.code("uv sync --project tools/conversion --frozen", language="powershell")
        st.button("开始格式转换", type="primary", disabled=not (sample_ready and env_ready), on_click=start_conversion_run)
    with st.container(border=True):
        st.subheader("已验证输出")
        try:
            exports = ui_conversion.registered_exports()
        except Exception as exc:
            show_error("已验证输出列表无法读取", exc)
            return
        if not exports:
            st.info("尚无通过验证且文件保持一致的输出。未完成或失败的转换可到「运行记录」查看。")
        else:
            chosen = st.selectbox("选择转换输出", range(len(exports)), key="export_picker",
                format_func=lambda index: f"{exports[index].get('run_id', '—')} · LeRobot v3.0")
            export = exports[chosen]
            st.caption(str(ui_conversion.export_path(export)))
            st.button("浏览这份转换样本", on_click=switch_to_export, args=(export,))
            with st.expander("输出登记与指纹"):
                st.json(export)


def show_official_verification(state: dict):
    request = state.get("request", {})
    operation = request.get("operation")
    is_business_conversion = request.get("kind") == "business" and operation in {"conversion", "final_quality", "delivery", "verify_delivery"}
    if request.get("kind") != "conversion" and not is_business_conversion:
        return
    stages = ("official_verify", "archive_verify") if operation == "final_quality" else (
        "archive_verify" if operation in {"delivery", "verify_delivery"} else "official_verify",)
    artifacts = state.get("artifacts", {})
    for stage in stages:
        # Automatic final-quality runs retain two distinct pieces of evidence:
        # conversion compares against HDF5; archive verification reads the ZIP.
        value = artifacts.get(stage) if is_business_conversion else state.get("verification_path")
        if not value and operation != "final_quality":
            value = state.get("verification_path")
        show_official_verification_artifact(state, stage, value)


def show_official_verification_artifact(state: dict, stage: str, value):
    is_archive = stage == "archive_verify"
    title = "交付包官方离线加载验证" if is_archive else "官方加载与批读取验证"
    if not value:
        st.caption(f"{title}结果尚未产生。")
        return
    path = ui_runs.artifact_path(state, value)
    if not path.is_file() and state.get("status") in ui_runs.ACTIVE_STATUSES:
        st.caption("官方离线加载验证尚未完成，完成后显示实际结果。")
        return
    with st.container(border=True):
        st.subheader(title)
        try:
            raw = path.read_text(encoding="utf-8")
            verification = json.loads(raw)
            details = verification.get("details", verification)
            columns = st.columns(3)
            columns[0].metric("验证状态", "通过" if verification.get("status") == "passed" else "查看详情")
            columns[1].metric("输出数值检查帧数" if is_archive else "数值比对帧数",
                              details.get("output_numeric_frames_checked" if is_archive else "checked_numeric_frames", "—"))
            columns[2].metric("批读取大小", details.get("batch_size", "—"))
            if details.get("validation_scope"):
                st.caption(details["validation_scope"])
            with st.expander("完整官方验证证据"):
                st.json(verification)
            label = "下载交付包官方验证结果 JSON" if is_archive else "下载官方验证结果 JSON"
            st.download_button(label, raw, f"{state['run_id']}-{stage}.json", "application/json", key=f"official_{state['run_id']}_{stage}")
        except (OSError, ValueError) as exc:
            show_error("官方验证产物无法读取", exc)


def show_run_logs(state: dict):
    try:
        events = ui_runs.load_events(state["run_id"])
    except Exception as exc:
        show_error("运行日志无法读取", exc)
        return
    st.subheader("运行日志")
    columns = st.columns(3)
    episodes = sorted({item["episode_index"] for item in events if item.get("episode_index") is not None})
    nodes = sorted({item["node_id"] for item in events if item.get("node_id")})
    levels = sorted({str(item.get("level", "INFO")).upper() for item in events})
    episode = columns[0].selectbox("日志任务", [None, *episodes], format_func=lambda x: "全部任务" if x is None else f"任务 {x}", key=f"log_episode_{state['run_id']}")
    node = columns[1].selectbox("日志节点", [None, *nodes], format_func=lambda x: "全部节点" if x is None else x, key=f"log_node_{state['run_id']}")
    level = columns[2].selectbox("日志级别", [None, *levels], format_func=lambda x: "全部级别" if x is None else x, key=f"log_level_{state['run_id']}")
    filtered = ui_runs.filter_events(events, episode, node, level)
    st.caption(f"显示 {len(filtered)} / {len(events)} 条事件。节点执行状态和检测到的数据异常分别记录。")
    if filtered:
        st.dataframe(pd.DataFrame([{
            "时间": item.get("timestamp", item.get("at", "")), "级别": item.get("level", "INFO"),
            "任务": item.get("episode_index"), "节点": item.get("node_id", ""),
            "事件": item.get("message", ""),
        } for item in filtered]), hide_index=True, width="stretch")
        with st.expander("所选日志的完整事件与诊断"):
            choice = st.selectbox("选择日志事件", range(len(filtered)),
                                  format_func=lambda x: f"{x + 1}. {filtered[x].get('message', '')}", key=f"event_{state['run_id']}")
            st.json(filtered[choice])
    else:
        st.info("当前条件下没有事件；运行中的日志会自动刷新。")
    a, b = st.columns(2)
    a.download_button("下载筛选日志 JSONL", ui_runs.events_jsonl(filtered), f"{state['run_id']}-filtered.jsonl", "application/x-ndjson", key=f"logs_filtered_{state['run_id']}", width="stretch")
    b.download_button("下载完整日志 JSONL", ui_runs.events_jsonl(events), f"{state['run_id']}-events.jsonl", "application/x-ndjson", key=f"logs_all_{state['run_id']}", width="stretch")


@st.fragment(run_every="1s")
def _render_duration_chart(nodes: list[dict]):
    """把节点耗时画成横向条形，一眼看出瓶颈在哪几个节点。"""
    items = []
    for node in nodes:
        try:
            seconds = float(node.get("elapsed_seconds") or 0)
        except (TypeError, ValueError):
            seconds = 0.0
        if seconds > 0:
            items.append((node.get("label") or node.get("id") or "", seconds))
    if not items:
        return
    total = sum(value for _, value in items)
    peak = max(value for _, value in items)
    top = {name for name, _ in sorted(items, key=lambda pair: -pair[1])[:3]}
    rows = []
    for name, seconds in items:
        share = seconds / total * 100 if total else 0
        width = seconds / peak * 100 if peak else 0
        hot = " rd-dur-hot" if name in top else ""
        rows.append(
            f'<div class="rd-dur-row"><span class="rd-dur-name">{html.escape(name)}</span>'
            f'<span class="rd-dur-track"><i class="rd-dur-fill{hot}" style="width:{width:.1f}%"></i></span>'
            f'<span class="rd-dur-val">{seconds:.2f}s · {share:.1f}%</span></div>')
    top_seconds = sum(value for name, value in items if name in top)
    st.markdown(
        '<style>'
        '.rd-dur-row{display:flex;align-items:center;gap:10px;margin:3px 0;font-size:13px}'
        '.rd-dur-name{width:210px;flex:none;color:#334155}'
        '.rd-dur-track{flex:1;background:#eef2f7;border-radius:4px;height:14px;overflow:hidden}'
        '.rd-dur-fill{display:block;height:100%;background:#94a3b8;border-radius:4px}'
        '.rd-dur-fill.rd-dur-hot{background:#e07a5f}'
        '.rd-dur-val{width:118px;flex:none;text-align:right;color:#64748b;font-variant-numeric:tabular-nums}'
        '</style>' + "".join(rows), unsafe_allow_html=True)
    st.caption(f"橙色为耗时前三，合计 {top_seconds:.2f}s，占本次运行 {top_seconds / total * 100:.0f}%。"
               f"瓶颈集中在官方格式写入与官方加载验证，而非自有处理逻辑。")


def show_run_details(run_id: str):
    if st.session_state.get("page") != "运行记录":
        if st.session_state.pop("navigation_pending", False):
            st.rerun(scope="app")
        return
    try:
        state = ui_runs.load_run(run_id)
    except Exception as exc:
        show_error("运行记录无法读取", exc)
        return
    status = state.get("status", "queued")
    is_active = status in ui_runs.ACTIVE_STATUSES
    ui_business.render_run_context(state)
    if is_active:
        st.info(f"{ui_runs.RUN_STATUS.get(status, status)} · 运行在独立进程中执行，此区域每秒刷新。关闭或刷新页面不会重新启动任务。")
    else:
        st.caption("正在查看已保存的运行记录。刷新页面只读取记录，不重新执行。")
    if state.get("request", {}).get("execution_demo") == "video_timeout":
        st.warning("运行超时演示：只对任务 0 的执行过程施加延迟和短预算，未改动源数据；超时属于运行问题。")
    nodes = state.get("nodes", [])
    if state.get("request", {}).get("kind") != "quality":
        total = len(nodes)
        completed = sum(node.get("status") not in {"pending", "running"} for node in nodes)
        progress_label = "已结束节点"
    else:
        total = int(state.get("episode_total", len(EPISODES)))
        completed = int(state.get("episode_completed", 0))
        progress_label = "已结束任务"
    st.progress(min(1.0, completed / max(1, total)), text=f"{progress_label} {completed} / {total}")
    failed = sum(item.get("status") in {"failed", "timed_out", "interrupted"} for item in nodes)
    columns = st.columns(3)
    columns[0].markdown("**运行状态**")
    columns[0].markdown(status_badge(status, ui_runs.RUN_STATUS.get(status, status)), unsafe_allow_html=True)
    columns[1].metric("失败或超时节点", failed)
    columns[2].metric("节点报告的数据异常", sum(int(item.get("data_issue_count", 0)) for item in nodes))
    st.caption(f"开始 {state.get('started_at', '等待启动')} · 结束 {state.get('completed_at') or '尚未结束'}")
    render_internal_pipeline(state)
    with st.expander("运行编号（可复制）"):
        st.code(run_id, language=None)
    st.subheader("节点执行情况")
    if nodes:
        _render_duration_chart(nodes)
        st.dataframe(styled_status_table(pd.DataFrame([{
            "任务": item.get("episode_index"), "节点": item.get("label", item.get("id", "")),
            "执行状态": ui_runs.NODE_STATUS.get(item.get("status"), item.get("status", "")),
            "数据异常数": item.get("data_issue_count", 0), "耗时 / 秒": item.get("elapsed_seconds"),
            "执行说明": item.get("message", ""),
        } for item in nodes])), hide_index=True, width="stretch")
        for item in nodes:
            if item.get("status") in {"failed", "timed_out", "interrupted"}:
                task = f"任务 {item['episode_index']} · " if item.get("episode_index") is not None else ""
                st.error(f"{task}{item.get('label', '处理节点')} · {ui_runs.NODE_STATUS[item['status']]}：{item.get('message') or '请查看节点诊断与日志。'}")
        _node_idx = int(st.session_state.get(f"node_detail_{run_id}", 0) or 0)
        _node = nodes[_node_idx] if 0 <= _node_idx < len(nodes) else None
        _node_status = _node.get("status") if _node else None
        _node_issues = int((_node or {}).get("data_issue_count", 0))
        if _node_status in {"failed", "timed_out", "interrupted"}:
            _diag_status = "failed"
        elif _node_status == "running":
            _diag_status = "running"
        elif _node_issues > 0:
            _diag_status = "alert"
        elif _node_status in {"done", "success", "passed"}:
            _diag_status = "done"
        elif _node_status == "pending":
            _diag_status = "muted"
        else:
            _diag_status = None
        with status_expander("节点诊断与文件证据", status=_diag_status):
            picked = st.selectbox("选择节点", range(len(nodes)), key=f"node_detail_{run_id}",
                format_func=lambda x: f"任务 {nodes[x].get('episode_index', '—')} · {nodes[x].get('label', nodes[x].get('id', ''))} · {ui_runs.NODE_STATUS.get(nodes[x].get('status'), nodes[x].get('status'))}")
            st.json(nodes[picked])
    else:
        st.caption("等待运行器写入节点记录。")
    if state.get("report_path"):
        st.button("查看本次质检报告", key=f"open_report_{run_id}", on_click=open_run_report, args=(run_id,))
    show_official_verification(state)
    with st.expander("运行参数、产物和完整诊断"):
        st.json({key: state.get(key) for key in ("request", "artifacts", "report_path", "verification_path", "diagnostics", "current_node", "started_at", "completed_at")})
        st.download_button("下载运行记录 JSON", json.dumps(state, ensure_ascii=False, indent=2, default=str), f"{run_id}-state.json", "application/json", key=f"state_{run_id}")
        logs = ui_runs.process_logs(state)
        if logs:
            chosen_log = st.selectbox("进程原始日志", logs, format_func=lambda path: path.name, key=f"process_log_{run_id}")
            try:
                content = chosen_log.read_bytes()
                preview = content.decode("utf-8", errors="replace")
                st.caption("完整日志可下载；长日志只在此预览最后 24,000 个字符。")
                st.code(preview[-24000:] or "（此日志目前为空）", language=None)
                st.download_button("下载所选进程完整日志", content, chosen_log.name, "text/plain", key=f"process_log_download_{run_id}")
            except OSError as exc:
                show_error("进程日志暂时无法读取", exc)
    with status_expander("查看运行日志",
                          status=("failed" if failed else ("running" if is_active else None)),
                          expanded=is_active or bool(failed)):
        show_run_logs(state)


def show_runs_page():
    st.title("运行记录")
    try:
        history = ui_runs.available_runs()
    except Exception as exc:
        show_error("历史运行无法读取", exc)
        return
    if not history:
        st.info("尚无运行记录。点击左侧「运行基础质检」，启动第一次检查。")
        return
    lookup = {item["run_id"]: item for item in history}
    ids = list(lookup)
    selected = st.session_state.get("selected_run_id")
    if selected not in lookup:
        selected = ids[0]
        st.session_state["pending_history_id"] = selected
        st.rerun()
    if st.session_state.get("history_picker") not in lookup:
        st.session_state["history_picker"] = selected
    st.selectbox("选择历史运行", ids, key="history_picker", on_change=choose_history,
        format_func=lambda x: f"{x[:16]}…{x[-6:]} · {ui_business.run_label(lookup[x])} · {ui_runs.RUN_STATUS.get(lookup[x].get('status'), '待确认')}")
    st.query_params["run_id"] = st.session_state["selected_run_id"]
    show_run_details(st.session_state["selected_run_id"])


@st.fragment(run_every="1s")
def show_saved_report(run_id: str):
    if st.session_state.get("page") != "质检报告":
        if st.session_state.pop("navigation_pending", False):
            st.rerun(scope="app")
        return
    try:
        state = ui_runs.load_run(run_id)
        report = ui_runs.load_report(state)
    except Exception as exc:
        show_error("已保存报告无法读取", exc)
        return
    st.caption(f"当前查看运行：{run_id} · {ui_runs.RUN_STATUS.get(state.get('status'), state.get('status', ''))}")
    if report is None:
        st.title("质检报告")
        st.info("此运行尚未生成报告。可在运行记录中查看当前节点、日志和诊断。")
        st.button("查看运行记录", on_click=set_page, args=("运行记录",))
        return
    st.session_state["quality_report"] = report
    st.session_state["active_episode_indices"] = [ep["episode_index"] for ep in report.get("episodes", [])]
    if state.get("request", {}).get("kind") == "business":
        st.session_state["report_matches_input"] = False
        batch_id = state.get("batch_id") or state["request"].get("batch_id")
        st.info("当前为批次运行保存的报告，可进入该批次的任务浏览定位问题。")
        if batch_id:
            st.button("进入批次查看问题位置", on_click=ui_business.open_batch, args=(batch_id, "batch_tasks"))
        show_report(report)
        return
    try:
        if state.get("request", {}).get("kind") == "conversion":
            export = registered_output_for_run(state)
            if export is None:
                raise ValueError("此转换尚无已验证输出")
            input_root = ui_conversion.export_path(export)
        else:
            input_root = Path(state.get("request", {})["data_root"])
        st.session_state["report_matches_input"] = dataset_fingerprint(input_root) == report.get("input_fingerprint")
    except Exception:
        st.session_state["report_matches_input"] = False
    if not st.session_state["report_matches_input"]:
        st.warning("当前输入已变化或无法读取。以下为当时保存的报告，行定位已停用；原始证据和诊断仍可查看。")
    report_source_notice(report)
    show_report(report)


def main():
    original_root = Path(os.environ.get("ROBODATA_DATA_ROOT", str(DEFAULT_DATA_ROOT))).expanduser().resolve()
    requested_page = st.query_params.get("page")
    st.session_state.setdefault("page", requested_page if requested_page in PAGES else PAGES[0])
    if st.session_state["page"] in ui_business.LEGACY_ROUTES:
        st.session_state["page"] = ui_business.LEGACY_ROUTES[st.session_state["page"]]
    elif st.session_state["page"] == "格式转换":
        # Existing sample-tool sessions remain separate from the batch node.
        st.session_state["page"] = "公开样本转换"
    st.session_state.setdefault("selected_episode", EPISODES[0])
    st.session_state.setdefault("selected_row", 0)
    # A full app rerun already honors the selected page. Old fragment timers must
    # not force another page, and URL history restoration happens only once.
    st.session_state.pop("navigation_pending", None)
    if st.session_state.get("pending_history_id"):
        st.session_state["history_picker"] = st.session_state.pop("pending_history_id")
        try:
            choose_history()
        except Exception as exc:
            st.session_state["start_error"] = exc
    restore_query = not st.session_state.get("query_restore_done", False)
    st.session_state["query_restore_done"] = True
    if restore_query and "selected_run_id" not in st.session_state and st.query_params.get("run_id"):
        st.session_state["history_picker"] = st.query_params["run_id"]
        try:
            choose_history()
            if requested_page not in PAGES:
                st.session_state["page"] = "运行记录"
        except Exception as exc:
            st.session_state["start_error"] = exc
            st.query_params.pop("run_id", None)
    st.session_state.setdefault("interval_tolerance", 10.0)
    st.session_state.setdefault("default_timeout", 60.0)
    st.session_state.setdefault("video_timeout", 120.0)
    st.session_state.setdefault("execution_scenario", "正常运行")
    with st.sidebar:
        st.markdown('<div class="brand"><span>◈</span> RoboData</div>', unsafe_allow_html=True)
        st.caption(f"具身数据质检工作台 · v{APP_VERSION}")
        st.divider()
        ui_navigation.sidebar(st.session_state["page"])
        st.divider()
    st.query_params["page"] = st.session_state["page"]
    if st.session_state["page"] in ui_navigation.BUSINESS_ROUTES:
        st.query_params.pop("run_id", None)
        ui_flow.render(st.session_state["page"])
        return
    # A batch run carries its own frozen input. Do not display the unrelated
    # legacy sample selector as if it controlled this run's data source.
    selected_business = None
    if st.session_state["page"] in {"运行记录", "质检报告"} and st.session_state.get("selected_run_id"):
        try:
            candidate = ui_runs.load_run(st.session_state["selected_run_id"])
            if candidate.get("request", {}).get("kind") == "business":
                selected_business = candidate
        except Exception:
            pass  # The run page below displays its full read diagnostic.
    if selected_business:
        with st.sidebar:
            request = selected_business.get("request", {})
            st.markdown(f"**{ui_business.run_label(selected_business)}**")
            st.caption("输入与参数取自所选运行的已保存请求。")
            source = request.get("source", {})
            st.caption(source.get("robot") or request.get("source_kind", "交付文件"))
            st.caption(str(request.get("input_path") or source.get("managed_path") or request.get("archive_path", "")))
            st.caption(f"运行目录 {ui_runs.runs_root()}")
        if st.session_state["page"] == "运行记录":
            show_runs_page()
        else:
            show_saved_report(selected_business["run_id"])
        return
    with st.sidebar:
        mode = st.selectbox("数据模式", DATA_MODES, key="data_mode", on_change=reset_data_mode)
        if mode in DATA_MODES[1:3]:
            st.caption("数据故障仅注入独立副本；原始样本保持不变。")
        scenario = st.selectbox("运行情景", ("正常运行", "运行超时演示"), key="execution_scenario")
        if scenario == "运行超时演示":
            st.caption("演示任务 0 执行超时与后续任务继续；不修改数据，也不将超时称为坏数据。")
        with st.expander("检查参数"):
            percent = st.number_input("时间间隔相对容差 / %", min_value=0.0, max_value=100.0, step=1.0, key="interval_tolerance")
            st.caption("默认允许 10% 采样间隔偏差。每次运行记录参数快照；修改不会改写历史。")
            ordinary_timeout = st.number_input("普通节点超时 / 秒", min_value=1.0, step=1.0, key="default_timeout")
            video_timeout = st.number_input("视频节点超时 / 秒", min_value=1.0, step=1.0, key="video_timeout")
            st.caption("超出预算会终止对应节点。超时演示对任务 0 使用单独的短预算，其他任务沿用这里的配置。")
        st.markdown("**配置的数据源**")
        if mode == DATA_MODES[3]:
            st.caption(HDF5_SOURCE["repo_id"])
            st.caption(f"配置的来源版本 {HDF5_SOURCE['revision'][:12]} · demo_0–2")
            st.caption("目标数据格式 LeRobot v3.0")
        else:
            st.caption(REPO_ID)
            st.caption(f"配置的来源版本 {REVISION[:12]} · Episode 0–4")
    root = original_root
    if mode in DATA_MODES[1:3]:
        root = PROJECT_ROOT / "work" / ("failure_demo" if mode == DATA_MODES[1] else "missing_video_demo")
    export = None
    export_error = None
    if mode == DATA_MODES[3]:
        try:
            candidates = ui_conversion.registered_exports()
            requested_root = st.session_state.get("history_data_root")
            export = next((item for item in candidates if str(ui_conversion.export_path(item)) == requested_root), None) if requested_root else ui_conversion.latest_export()
            if export:
                root = ui_conversion.export_path(export)
        except Exception as exc:
            export_error = exc
    if st.session_state.get("history_data_root"):
        root = Path(st.session_state["history_data_root"])
    params = dict(DEFAULT_RULE_PARAMS)
    params.update(interval_relative_tolerance=percent / 100, video_timestamp_tolerance_frames=0.5)
    with st.sidebar:
        st.button("运行基础质检", type="primary" if st.session_state["page"] in {"数据概览", "任务浏览", "质检报告"} else "secondary", width="stretch", on_click=start_requested_run,
                  args=(str(root), params, mode, scenario, {"default": ordinary_timeout, "video_decode": video_timeout}), disabled=mode == DATA_MODES[3] and not export)
        if st.session_state.get("selected_run_id"):
            st.caption(f"选中运行 {st.session_state['selected_run_id']}")
        with st.expander("本地信息"):
            st.caption(str(root))
            st.caption(f"规则版本 {RULE_VERSION}")
            st.caption(f"运行目录 {ui_runs.runs_root()}")
    if st.session_state.get("start_error"):
        show_error("运行操作失败", st.session_state.pop("start_error"))
    # History and saved reports must remain reachable even when source metadata fails.
    if st.session_state["page"] == "公开样本转换":
        show_conversion_page()
        return
    if st.session_state["page"] == "运行记录":
        show_runs_page()
        return
    if st.session_state["page"] == "质检报告":
        if st.session_state.get("selected_run_id"):
            show_saved_report(st.session_state["selected_run_id"])
        else:
            show_report(None)
        return
    if mode == DATA_MODES[3] and not export:
        st.title("已验证转换样本")
        st.info("尚无可用的已验证输出，或所选输出在验证后发生了变化。请在「公开样本转换」启动转换；历史诊断仍可在「运行记录」查看。")
        if export_error:
            show_error("转换输出无法读取", export_error)
        st.button("前往公开样本转换", on_click=set_page, args=("公开样本转换",))
        return
    if mode in DATA_MODES[1:3] and not (root / "demo_manifest.json").is_file() and not st.session_state.get("history_data_root"):
        st.title("准备演示副本")
        st.info("将已下载的 5 条任务复制到独立目录，只对任务 0 注入故障。其余 4 条用于验证检查能继续完成。")
        st.write("注入内容：动作包含无效数值、时间倒退、异常时间间隔。" if mode == DATA_MODES[1] else "副本不包含任务 0 的主相机视频，保留其数据表。")
        if st.button("准备演示副本", type="primary"):
            try:
                with st.spinner("正在准备独立演示副本…"):
                    create_failure_demo(original_root, root, scenario="anomalies" if mode == DATA_MODES[1] else "missing_video")
                st.rerun()
            except Exception as exc:
                show_error("准备演示副本失败", exc)
        return
    try:
        demo = read_demo_manifest(root)
        fingerprint = dataset_fingerprint(root)
        metadata = cached_metadata(str(root), fingerprint)
        st.session_state["active_episode_indices"] = metadata.get("episode_indices", list(EPISODES))
    except Exception as exc:
        st.title("准备公开样本")
        st.info("需要先获取指定版本的 5 条任务数据，再开始浏览和检查；运行记录和历史报告仍可使用。")
        st.code("uv run robodata fetch-sample", language="powershell")
        show_error("样本读取失败", exc)
        st.caption(f"本地数据目录：{root}")
        return
    if demo:
        st.warning("这是明确标注的故障注入测试副本，结果不代表原始公开样本。")
        with st.expander("演示副本说明与注入位置"):
            st.dataframe(pd.DataFrame([{"任务": item.get("episode_index"), "行位置": item.get("row_index"),
                "字段": item.get("field"), "注入内容": item.get("description")} for item in demo.get("injections", [])]), hide_index=True, width="stretch")
    report = None
    try:
        state = selected_run()
        candidate = ui_runs.load_report(state) if state else None
        if candidate and candidate.get("input_fingerprint") == fingerprint:
            report = candidate
        elif candidate:
            st.info("当前数据与所选历史运行的输入不同。历史报告仍在「质检报告」中保留，本页不套用其结果。")
    except Exception as exc:
        show_error("所选运行报告无法读取", exc, warning=True)
    st.session_state["report_matches_input"] = report is not None
    if report:
        report_source_notice(report)
    if st.session_state["page"] == "数据概览":
        show_overview(root, fingerprint, metadata, report)
    else:
        show_browser(root, fingerprint, report)


main()
