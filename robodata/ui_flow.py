"""Eight distinct business nodes over the authoritative batch and run records."""

from __future__ import annotations

import hashlib
import json
from html import escape
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
import streamlit as st

from . import ui_business as ui, ui_runs
from .errors import explain_error
from . import ui_navigation as nav
from .ui_status import aggregate_status, status_badge, styled_status_table

STATUS = {"pending": "等待上游", "waiting_upstream": "等待上游", "blocked": "暂不能继续",
          "ready": "准备就绪", "waiting_human": "等待人工", "running": "执行中",
          "completed": "已完成", "failed": "执行失败", "timed_out": "执行超时",
          "interrupted": "已中断", "superseded": "已过期", "not_started": "尚未开始"}
OUTCOME = {"passed": "通过", "rejected": "未通过", "execution_error": "执行异常", "unknown": "尚无结论", None: "尚无结论"}
STATUS_STYLE = {
    "completed": ("success", "已完成"), "passed": ("success", "通过"), "approved": ("success", "审核通过"),
    "running": ("running", "进行中"), "ready": ("running", "准备就绪"),
    "failed": ("error", "失败"), "timed_out": ("error", "超时"),
    "rejected": ("warning", "未通过"), "blocked": ("warning", "暂不能继续"),
    "interrupted": ("interrupted", "已中断"), "superseded": ("muted", "已过期"),
    "pending": ("muted", "待处理"), "waiting_upstream": ("muted", "等待上游"),
    "waiting_human": ("muted", "等待人工"), "not_started": ("muted", "尚未开始"),
}


def navigate(node_id: str):
    nav.navigate(node_id)


def empty_flow():
    return {"nodes": [{"node_id": key, "label": label,
        "status": "ready" if key == "import" else "waiting_upstream",
        "outcome": None, "reason": "请选择或上传输入文件。" if key == "import" else "请先导入一个批次，后续节点会沿用它的输入与版本。",
        "inputs": [], "intermediates": [], "outputs": [], "history": []}
        for key, label in ui.NODE_LABELS.items()]}


def flow_for(batch: dict | None):
    if batch is None:
        return empty_flow()
    from .workflow import get_flow
    return get_flow(batch, runs_root=ui_runs.runs_root(), business_root=ui.service().get_store().root)


def _overall_status(flow: dict) -> tuple[str, str]:
    return aggregate_status(flow.get("nodes", []))


def next_node(flow: dict) -> str:
    nodes = flow.get("nodes", [])
    for node in nodes:
        if node.get("status") == "running":
            return node["node_id"]
    for node in nodes:
        if node.get("status") in {"failed", "timed_out", "interrupted"}:
            return node["node_id"]
        if node.get("outcome") == "rejected":
            return {"clean": "select", "review": "annotate"}.get(node["node_id"], node["node_id"])
        if node.get("status") in {"ready", "waiting_human", "superseded"}:
            return node["node_id"]
    if nodes and all(n.get("status") == "completed" and n.get("outcome") == "passed" for n in nodes):
        return "deliver"
    return next((n["node_id"] for n in nodes if n.get("status") != "completed"), "import")


def context_header(batch: dict | None, flow: dict, page: str):
    kind, status = _overall_status(flow)
    stage = nav.ROUTE_LABELS.get(page, nav.ROUTE_LABELS.get(nav.main_for(page) or "", "批次总览"))
    label = escape(str(batch.get("label", "未命名批次"))) if batch else "尚未导入"
    st.markdown(
        f'<div class="rd-context"><strong>当前批次</strong><span>{label}</span>'
        f'<span class="sep">›</span><strong>当前阶段</strong><span>{stage}</span>'
        f'<span class="rd-status rd-status-{kind}">{escape(status if batch else "等待输入")}</span></div>',
        unsafe_allow_html=True)


PROGRESS_ARROW = (
    '<svg viewBox="0 0 16 16" fill="none">'
    '<path d="M2 8h10.5M8.8 4.3 12.5 8l-3.7 3.7" stroke="currentColor" '
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>')


def _quarantined_count(batch: dict | None) -> int:
    """被隔离的任务数。

    节点执行成功不等于数据干净：清洗与筛选节点的 outcome 都是 passed，
    被隔离的异常任务数只体现在 reason 文本里（如「2 条隔离」），
    导致故障批次在进度条上整体显示为绿色。

    只统计 quarantine：它代表数据本身有问题。exclude 是人工决定不保留，
    正常批次也会出现（真实闭环验收批次即有 1 条 exclude），不能当作异常信号。
    """
    episodes = (batch or {}).get("episodes") or []
    return sum(1 for ep in episodes if ep.get("disposition") == "quarantine")


def progress_strip(flow: dict, selected: str, batch: dict | None = None):
    by_id = {node["node_id"]: node for node in flow.get("nodes", [])}
    active = nav.main_for(selected)
    quarantined = _quarantined_count(batch)
    cells = []
    for index, node_id in enumerate(nav.NODE_ORDER, 1):
        tone, text = aggregate_status([by_id.get(node_id, {"status": "not_started"})])
        # 执行成功但存在被隔离任务时改用告警色，避免故障批次整体显示为绿色。
        if tone == "success" and quarantined and node_id in {"clean", "select"}:
            tone, text = "warning", f"已完成；{quarantined} 条任务被隔离"
        cls = {"success": "done", "running": "current", "warning": "alert", "error": "failed"}.get(tone, "")
        viewing = node_id == active
        cls += " selected" if viewing else ""
        current = ' aria-current="step"' if viewing else ""
        suffix = " · 当前页面" if viewing else ""
        params = {"page": node_id}
        if st.query_params.get("batch_id"):
            params["batch_id"] = st.query_params["batch_id"]
        href = escape("?" + urlencode(params), quote=True)
        cells.append(
            f'<a class="rd-progress-step {cls}" href="{href}" target="_self"{current}>'
            f'<span class="rd-progress-num">{index:02d}</span>'
            f'<span class="rd-progress-name">{escape(nav.ROUTE_LABELS[node_id])}</span>'
            f'<span class="rd-progress-state">{escape(text)}{escape(suffix)}</span>'
            f'</a>')
        if index < len(nav.NODE_ORDER):
            cells.append(f'<span class="rd-progress-arrow" aria-hidden="true">{PROGRESS_ARROW}</span>')
    st.markdown(
        '<div class="rd-progress" aria-label="批次阶段进度">'
        '<div class="rd-progress-row">' + "".join(cells) + "</div></div>",
        unsafe_allow_html=True)


def render_node_status(node: dict):
    execution = status_badge(node.get("status", "not_started"))
    result = status_badge(node.get("outcome") or "unknown", OUTCOME.get(node.get("outcome"), "尚无结论"))
    st.markdown(f'<div class="rd-context">节点执行 {execution}<span class="sep">·</span>数据／业务结果 {result}</div>',
                unsafe_allow_html=True)
    reason = node.get("reason")
    if reason and (node.get("status") in {"failed", "timed_out", "interrupted", "blocked", "superseded"}
                   or node.get("outcome") in {"rejected", "execution_error"}):
        (st.error if node.get("status") in {"failed", "timed_out", "interrupted"}
         or node.get("outcome") == "execution_error" else st.warning)(reason)
        run_id = node.get("attempt_id")
        if run_id and (ui_runs.runs_root() / run_id / "state.json").is_file():
            st.button("查看此节点日志", on_click=ui.open_run, args=(run_id,))


def decision_export(batch: dict, kind: str, node: dict):
    from .workflow import download_artifact
    labels = {"clean": "导出清洗处理记录 JSON", "select": "导出筛选清单 JSON",
              "annotate": "导出标注文件 JSON", "review": "导出审核记录 JSON"}
    candidates = [ref for ref in node.get("intermediates", []) + node.get("outputs", [])
                  if Path(ref.get("path", "")).name == f"{kind}.json"]
    if not candidates:
        st.caption("尚未保存本节点业务记录。")
        return
    ref = candidates[0]
    try:
        data = download_artifact(batch["batch_id"], ref, business_root=ui.service().get_store().root,
                                 runs_root=ui_runs.runs_root())
        st.download_button(labels[kind], data, f"{batch['batch_id']}-v{batch['content_version']}-{kind}.json",
                           "application/json", key=f"decision_export_{kind}")
        st.caption(f"内容版本 {batch['content_version']} · SHA-256：{hashlib.sha256(data).hexdigest()}")
    except Exception as exc:
        st.error(f"本节点业务记录下载前核验未通过：{explain_error(exc)}")


def artifact_list(batch: dict, references: list[dict], title: str, key: str):
    st.markdown(f"**{title}**")
    if not references:
        st.caption("尚未保存。")
        return
    st.dataframe(pd.DataFrame([{"文件": str(ref.get("path", "")), "类型": ref.get("role", ""),
        "字节": ref.get("size", ref.get("size_bytes")), "SHA-256": ref.get("sha256", ""),
        "数据版本": ref.get("content_version"), "有效性": "当前有效" if ref.get("valid") and ref.get("current") else "历史或待核验"}
        for ref in references]), hide_index=True, width="stretch")
    selected = st.selectbox("选择查看／下载的文件", range(len(references)), key=f"artifact_{key}",
        format_func=lambda i: f"{references[i].get('role', '文件')} · {Path(references[i].get('path', '')).name}")
    ref = references[selected]
    if not ref.get("current"):
        st.caption("该文件属于历史版本；当前节点不会将它继续传递为有效输入。")
        return
    if not ref.get("valid"):
        st.warning(ref.get("validity") or ref.get("message") or ref.get("error") or "文件缺失、尚未校验或内容发生变化，已停止提供有效下载。")
        return
    try:
        from .workflow import download_artifact
        data = download_artifact(batch["batch_id"], ref, business_root=ui.service().get_store().root, runs_root=ui_runs.runs_root())
        suffix = Path(ref["path"]).suffix.lower()
        if suffix in {".json", ".jsonl", ".md", ".txt"} and len(data) < 150_000:
            with st.expander("查看文件内容"):
                st.code(data.decode("utf-8"), language="json" if suffix == ".json" else None)
        mime = {".json": "application/json", ".html": "text/html", ".jsonl": "application/x-ndjson",
                ".zip": "application/zip", ".mp4": "video/mp4"}.get(suffix, "application/octet-stream")
        st.download_button("下载所选实际文件", data, Path(ref["path"]).name, mime, key=f"download_artifact_{key}")
    except Exception as exc:
        st.error(f"文件下载前核验未通过：{explain_error(exc)}")


def render_logs(batch: dict, node: dict):
    from .workflow import read_flow_events
    node_id = node["node_id"]
    with st.expander("当前节点日志与尝试记录"):
        try:
            events = read_flow_events(batch, node_id=node_id, runs_root=ui_runs.runs_root())
            if not events:
                st.caption("此节点尚无已保存日志。人工节点的决定保存在业务审计历史。")
                return
            episode_options = [None] + sorted({e["episode_index"] for e in events if isinstance(e.get("episode_index"), int)})
            attempts = [None] + sorted({str(e.get("attempt_id") or e.get("run_id")) for e in events if e.get("attempt_id") or e.get("run_id")})
            columns = st.columns(3)
            episode = columns[0].selectbox("任务筛选", episode_options, format_func=lambda v: "全部任务" if v is None else f"任务 {v}", key=f"flow_log_ep_{node_id}")
            attempt = columns[1].selectbox("尝试筛选", attempts, format_func=lambda v: v or "全部尝试", key=f"flow_log_attempt_{node_id}")
            level = columns[2].selectbox("级别筛选", [None, "INFO", "WARNING", "ERROR"], format_func=lambda v: {None: "全部级别", "INFO": "信息", "WARNING": "警告", "ERROR": "错误"}[v], key=f"flow_log_level_{node_id}")
            filtered = [e for e in events if (episode is None or e.get("episode_index") == episode)
                and (attempt is None or str(e.get("attempt_id") or e.get("run_id")) == attempt)
                and (level is None or str(e.get("level", "INFO")).upper() == level)]
            st.dataframe(pd.DataFrame([{"时间": e.get("timestamp", e.get("at_utc", e.get("time_utc"))),
                "尝试": e.get("attempt_id", e.get("run_id")), "任务": e.get("episode_index"),
                "节点": e.get("node_id", node_id), "级别": e.get("level"),
                "说明": e.get("message", e.get("message_zh", ""))} for e in filtered]), hide_index=True, width="stretch")
            st.download_button("下载当前筛选日志 JSONL", ui_runs.events_jsonl(filtered), f"{batch['batch_id']}-{node_id}-events.jsonl", "application/x-ndjson", key=f"flow_log_download_{node_id}")
            with st.expander("原始日志与错误证据"):
                st.json(filtered)
        except Exception as exc:
            ui.failure("节点日志读取失败", exc)


def node_details(batch: dict, node: dict):
    st.write(f"**{STATUS.get(node['status'], node['status'])} · {OUTCOME.get(node.get('outcome'), node.get('outcome') or '尚无结论')}**")
    st.info(node.get("reason") or "请查看当前节点证据与运行记录。")
    st.caption(f"内容版本 {node.get('content_version', batch['content_version'])} · 本次尝试 {node.get('attempt_id') or '尚未开始'} · 上游尝试 {node.get('upstream_attempt') or '尚无'}")
    progress = node.get("progress", {})
    if progress.get("total") is not None:
        st.caption(f"已保存阶段进度：{progress.get('completed', 0)} / {progress['total']}")
    if node.get("diagnostics"):
        with st.expander("节点失败诊断"):
            st.json(node["diagnostics"])
    for key, title in (("inputs", "当前输入"), ("intermediates", "已保存中间产物"), ("outputs", "节点输出")):
        with st.expander(title, expanded=False):
            artifact_list(batch, node.get(key, []), title, f"{node['node_id']}_{key}")
    if node.get("history"):
        with st.expander("历史尝试（可能已过期）"):
            st.json(node["history"])
    render_logs(batch, node)


def render_cleaning(batch: dict, *, node: dict, active=False):
    done = node.get("status") == "completed"
    if done:
        st.button("前往数据筛选", type="primary", on_click=navigate, args=("select",))
    st.button("重新执行基础清洗检查", type="secondary" if done or active else "primary",
              disabled=active, on_click=ui.start_operation,
              args=(ui.service().start_batch_quality, batch["batch_id"], batch["revision"]))
    if active:
        st.info("当前批次正在处理，完成后可重新检查。")
    st.caption("重新检查将要求重新确认候选集及审核；旧下游产物保留为历史。")
    st.dataframe(styled_status_table(ui.inventory(batch)), hide_index=True, width="stretch")
    ep = ui.choose_episode(batch)
    if ep:
        ui.quality_evidence(batch, ep)
        ui.render_preview(batch, ep)


def final_quality_allowed(batch: dict):
    kept = [e for e in batch["episodes"] if e["disposition"] == "keep"]
    return bool(batch["cleaning_confirmed"] and kept and all(e["review"]["status"] == "approved" for e in kept))


def render_quality(batch: dict, *, node: dict, active=False, conversion_passed=False):
    allowed = final_quality_allowed(batch)
    done = node.get("status") == "completed" and node.get("outcome") == "passed"
    if done and not active:
        st.button("前往打包交付" if conversion_passed else "前往格式转换", type="primary",
                  on_click=navigate, args=("deliver" if conversion_passed else "convert",))
    st.button("执行最终质检并自动继续", type="secondary" if done or active else "primary",
              disabled=not allowed or active, on_click=ui.start_operation,
              args=(ui.service().start_final_quality, batch["batch_id"], batch["revision"]))
    kept = [e for e in batch["episodes"] if e["disposition"] == "keep"]
    remaining = sum(e["review"]["status"] != "approved" for e in kept)
    if active:
        st.info("当前批次正在处理；无需重复启动，可在运行记录查看进度。")
    elif not batch["cleaning_confirmed"]:
        st.warning("请先确认候选集，再继续最终质检。")
    elif not kept:
        st.warning("候选集为空，请至少保留 1 条通过检查的任务。")
    elif remaining:
        st.warning(f"还有 {remaining} 条保留任务未审核；所有保留任务审核通过后才能继续。")
    st.caption("HDF5 通过后自动转换并验证交付包；规则未通过或执行失败会保留报告并停止下游。")
    st.dataframe(styled_status_table(ui.inventory(batch)), hide_index=True, width="stretch")


def report_downloads(batch: dict, node: dict):
    from .workflow import download_artifact
    files = node.get("outputs", []) + node.get("intermediates", [])
    reports = [ref for ref in files if ("report" in Path(ref.get("path", "")).name.lower()
               or "quality" in ref.get("role", "").lower() or "质检" in ref.get("role", "") or "核验" in ref.get("role", ""))
               and Path(ref.get("path", "")).suffix.lower() in {".json", ".html"}]
    for extension, label in ((".html", "下载本节点质检报告 HTML"), (".json", "下载本节点质检报告 JSON")):
        available = [ref for ref in reports if Path(ref["path"]).suffix.lower() == extension and ref.get("current") and ref.get("valid")]
        if not available:
            continue
        ref = available[-1]
        try:
            data = download_artifact(batch["batch_id"], ref, business_root=ui.service().get_store().root, runs_root=ui_runs.runs_root())
            st.download_button(label, data, Path(ref["path"]).name,
                "text/html" if extension == ".html" else "application/json", key=f"quality_report_{node['node_id']}_{extension}")
        except Exception as exc:
            st.error(f"报告下载前核验未通过：{explain_error(exc)}")


def render_batch_report(batch: dict, flow: dict):
    by_id = {node["node_id"]: node for node in flow["nodes"]}
    st.dataframe(styled_status_table(ui.inventory(batch)), hide_index=True, width="stretch")
    for key, label in (("clean", "基础清洗检查"), ("quality", "最终数据质检")):
        st.subheader(label)
        render_node_status(by_id[key])
        report_downloads(batch, by_id[key])
    ep = ui.choose_episode(batch)
    if ep:
        ui.quality_evidence(batch, ep)
        with st.expander("查看任务数据"):
            ui.render_preview(batch, ep)
    from .ui_report_history import render_history
    render_history(batch)


def render_batch_source(batch: dict):
    source = batch["source"]
    st.dataframe(pd.DataFrame([
        {"项目": label, "值": source.get(key, "未记录")}
        for key, label in (("repo_id", "来源数据集"), ("revision", "固定版本"),
                           ("format_version", "来源格式"), ("robot", "机器人／场景"),
                           ("camera", "主相机"), ("license", "来源许可"))
    ]), hide_index=True, width="stretch")
    st.write("**时间语义：**" + str(
        source.get("timestamp_semantics")
        or "来源元数据未声明时间语义；本平台按原值读取，不推断采样频率，"
           "也不声明硬件时间同步。"))
    try:
        from .importing import preview_batch
        preview = preview_batch(batch, batch["episodes"][0]["episode_index"], 0)
        fields = [{"字段": field, "维度": len(preview.get(key, [])),
                   "分量顺序": "、".join(preview.get(key, []))}
                  for field, key in (("observation.state", "state_names"), ("action", "action_names"))]
        st.dataframe(pd.DataFrame(fields), hide_index=True, width="stretch")
    except Exception as exc:
        st.warning(f"字段说明暂时无法读取：{explain_error(exc)}")
    if source.get("source_statistics"):
        with st.expander("来源统计"):
            st.json(source["source_statistics"])
    with st.expander("完整来源与输入清单"):
        st.json(source)


def render(page: str):
    st.title(nav.ROUTE_LABELS.get(page, page))
    if message := st.session_state.pop("business_notice", None):
        (st.error if message["error"] else st.success)(message["message"])
    if details := st.session_state.pop("business_diagnostic", None):
        with st.expander("本次操作原始诊断"):
            st.json(details)
    try:
        ui.sync_last_run()
        listing = ui.service().safe_list_batches()
    except Exception as exc:
        ui.failure("业务记录暂时无法读取", exc)
        return
    for error in listing.get("errors", []):
        st.warning(f"一个批次记录无法读取：{error.get('message', '请查看原始诊断')}。其余批次仍可使用。")
        with st.expander(f"损坏批次诊断 · {error.get('path', '')}"):
            st.json(error)
    batches = listing.get("batches", [])
    batch = None
    if batches:
        lookup = {b["batch_id"]: b for b in batches}
        selected = st.session_state.get("selected_batch_id") or st.query_params.get("batch_id")
        if selected not in lookup:
            selected = batches[0]["batch_id"]
        if st.session_state.get("batch_picker") not in lookup:
            st.session_state["batch_picker"] = selected
        with st.sidebar:
            st.selectbox("当前批次", list(lookup),
                         format_func=lambda key: f"{lookup[key]['label']} · {key[-8:]}",
                         key="batch_picker", on_change=ui.select_batch)
            st.button("刷新批次状态", key="refresh_business", width="stretch")
        st.session_state["selected_batch_id"] = st.session_state["batch_picker"]
        st.query_params["batch_id"] = st.session_state["selected_batch_id"]
        batch = ui.service().get_store().get_batch(st.session_state["selected_batch_id"])
    try:
        flow = flow_for(batch)
    except Exception as exc:
        ui.failure("流程证据暂时无法读取", exc)
        flow = empty_flow()
    context_header(batch, flow, page)
    progress_strip(flow, page, batch)
    nav.subnavigation(page)
    if page == "import":
        ui.render_import()
    if batch is None:
        st.info("尚无可用批次，请先上传或导入本机数据。")
        if page != "import":
            st.button("前往上传／导入", type="primary", on_click=navigate, args=("import",))
        return
    by_id = {n["node_id"]: n for n in flow["nodes"]}
    active = any(n.get("status") in {"running", "queued"} for n in flow["nodes"])
    passed = lambda key: by_id[key].get("status") == "completed" and by_id[key].get("outcome") == "passed"
    ui.render_summary(batch, compact=page != "flow_overview")
    if page == "flow_overview":
        target = next_node(flow)
        label = "查看已验证交付包" if passed("deliver") else f"继续：{nav.ROUTE_LABELS[target]}"
        if passed("deliver"):
            st.caption("八个节点已全部完成并通过，可以下载经独立校验的交付包。")
        else:
            done = sum(1 for n in flow["nodes"] if n.get("status") == "completed")
            st.caption(f"链路进度 {done}/{len(flow['nodes'])} 个节点已完成，"
                       f"下一个待处理节点：{nav.ROUTE_LABELS[target]}。"
                       f"上游节点未完成时会显示“等待上游”，不会跳过。")
        st.button(label, type="primary", on_click=navigate, args=(target,))
        st.dataframe(styled_status_table(pd.DataFrame([
            {"节点": n["label"], "执行状态": STATUS.get(n["status"], n["status"]),
             "数据／业务结果": OUTCOME.get(n.get("outcome"), n.get("outcome")),
             "当前说明": n.get("reason", "")} for n in flow["nodes"]])), hide_index=True, width="stretch")
        with st.expander("批次任务清单"):
            st.dataframe(styled_status_table(ui.inventory(batch)), hide_index=True, width="stretch")
        st.button("浏览本批次任务", on_click=navigate, args=("batch_tasks",))
    elif page == "batch_tasks":
        st.dataframe(styled_status_table(ui.inventory(batch)), hide_index=True, width="stretch")
        ep = ui.choose_episode(batch)
        if ep:
            ui.quality_evidence(batch, ep)
            ui.render_preview(batch, ep)
    elif page == "batch_report":
        render_batch_report(batch, flow)
    elif page == "batch_source":
        render_batch_source(batch)
    else:
        node = by_id[page]
        render_node_status(node)
        if page == "import":
            with st.expander("本批次来源与导入说明"):
                st.json(batch["source"])
        elif page == "clean":
            render_cleaning(batch, node=node, active=active)
        elif page == "select":
            ui.render_selection(batch)
        elif page == "annotate":
            ui.render_annotations(batch)
        elif page == "review":
            ui.render_reviews(batch)
        elif page == "quality":
            render_quality(batch, node=node, active=active, conversion_passed=passed("convert"))
        elif page == "convert":
            ui.render_conversion(batch, final_quality_passed=passed("quality"),
                                 current_output_valid=passed("convert"), active=active)
        elif page == "deliver":
            ui.render_delivery(batch, conversion_passed=passed("convert"),
                               current_delivery_valid=passed("deliver"), active=active)
        if page in {"quality", "clean"}:
            report_downloads(batch, node)
        if page in {"clean", "select", "annotate", "review"}:
            with st.expander("导出本节点记录"):
                decision_export(batch, page, node)
        # 默认展开：这是每个节点最核心的证据，收起来容易被人忽略。
        with st.expander("节点输入、进度与输出", expanded=True):
            node_details(batch, node)
    ui.render_associated_runs(batch["batch_id"])
    ui.render_audit(batch)
