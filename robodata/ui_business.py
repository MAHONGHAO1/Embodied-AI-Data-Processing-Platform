"""Local batch workflow UI. Long operations use the shared persistent runtime."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from . import ui_runs
from .errors import error_details, explain_error
from .ui_charts import apply_signal_layout
from .ui_status import status_badge, styled_status_table

NODE_LABELS = {"import": "上传／导入", "clean": "数据清洗", "select": "数据筛选",
               "annotate": "数据标注", "review": "标注审核", "quality": "数据质检",
               "convert": "格式转换", "deliver": "打包交付"}
PAGE_LABELS = {"flow_overview": "批次总览", **NODE_LABELS}
BUSINESS_PAGES = tuple(PAGE_LABELS)
LEGACY_ROUTES = {"批次管理": "flow_overview", "清洗筛选": "select", "标注审核": "review",
                 "转换验证": "convert", "交付中心": "deliver"}
RUN_KINDS = {"quality": "数据质检", "conversion": "格式转换"}
BUSINESS_OPERATIONS = {"import": "批次导入与基础清洗", "quality": "基础清洗检查", "final_quality": "最终质检与自动交付", "conversion": "候选集转换",
                       "delivery": "交付打包", "verify_delivery": "交付包重新验证"}
DISPOSITIONS = {"keep": "保留", "quarantine": "隔离", "exclude": "排除"}
QUALITY = {"not_checked": "尚未检查", "passed": "检查通过", "issues": "发现数据问题", "incomplete": "检查不完整", "load_failed": "读取失败"}
REVIEW = {"draft": "草稿", "submitted": "已提交", "returned": "已退回", "approved": "审核通过"}
OUTCOMES = {"unlabeled": "尚未标注", "success": "成功", "failure": "失败", "uncertain": "不确定"}
COLORS = ("#0d9488", "#2563eb", "#8b5cf6", "#f59e0b", "#e76f51", "#64748b", "#db2777", "#65a30d", "#0891b2")


def service():
    from . import business
    return business


def run_label(state: dict) -> str:
    request = state.get("request", {})
    if request.get("kind") == "business":
        return BUSINESS_OPERATIONS.get(request.get("operation"), "业务处理")
    return RUN_KINDS.get(request.get("kind"), "数据处理")


def notice(text: str, *, error=False):
    st.session_state["business_notice"] = {"message": text, "error": error}


def failure(context: str, exc: Exception):
    st.error(f"{context}：{explain_error(exc)}")
    with st.expander("原始诊断"):
        st.json(error_details(exc, include_traceback=True))


def perform(action, success: str):
    try:
        action()
        notice(success)
    except Exception as exc:
        notice(explain_error(exc), error=True)
        st.session_state["business_diagnostic"] = error_details(exc, include_traceback=True)


def open_run(run_id: str):
    st.session_state["selected_run_id"] = run_id
    st.session_state["history_picker"] = run_id
    st.session_state["business_run_id"] = run_id
    st.session_state["page"] = "运行记录"
    st.session_state["navigation_pending"] = True
    st.query_params["run_id"] = run_id


def open_batch(batch_id: str, page="flow_overview"):
    st.session_state["selected_batch_id"] = batch_id
    st.session_state["batch_picker"] = batch_id
    st.session_state["page"] = page
    st.session_state["navigation_pending"] = True
    st.query_params["batch_id"] = batch_id
    st.query_params.pop("run_id", None)


def goto_node(page: str):
    st.session_state["page"] = page
    st.session_state["navigation_pending"] = True
    st.query_params.pop("run_id", None)


def short_id(value: str) -> str:
    value = str(value)
    return value if len(value) <= 20 else f"{value[:12]}…{value[-6:]}"


def select_batch():
    st.session_state["selected_batch_id"] = st.session_state["batch_picker"]
    st.session_state.pop("business_episode", None)
    st.query_params["batch_id"] = st.session_state["batch_picker"]


def start_operation(operation, *args):
    try:
        run_id = operation(*args)
        open_run(run_id)
    except Exception as exc:
        notice(explain_error(exc), error=True)
        st.session_state["business_diagnostic"] = error_details(exc, include_traceback=True)


def clean_input_path(raw: str | None) -> str:
    """规范化用户粘贴的路径。

    从资源管理器「复制文件地址」得到的是带引号的 ``"C:\\...\\file.hdf5"``，
    直接粘贴会把引号也当成路径内容；有时还会连带前面的目录一起粘进来。
    这里优先提取被引号包裹的片段，其次只去掉首尾引号。
    """
    text = (raw or "").strip()
    quoted = re.search(r'"([^"]+)"', text)
    if quoted:
        return quoted.group(1).strip()
    return text.strip('"').strip("'").strip()


def start_import():
    from .importing import save_upload

    try:
        method = st.session_state["import_method"]
        if method == "上传 HDF5 文件":
            upload = st.session_state.get("import_upload")
            if upload is None:
                raise ValueError("请先选择需要导入的 HDF5 文件")
            path = save_upload(upload.getvalue(), upload.name)
            kind = "hdf5"
        else:
            path = clean_input_path(st.session_state.get("import_path", ""))
            if not path:
                raise ValueError("请填写本机文件或目录路径")
            kind = "hdf5" if method == "本机 HDF5 路径" else "SO-100 数据目录"
            kind = "so100" if kind == "SO-100 数据目录" else kind
        label = st.session_state.get("import_label", "").strip()
        if not label:
            raise ValueError("请填写批次名称")
        test_label = st.session_state.get("import_test_label", "").strip() or None
        run_id = service().start_import(path, kind, label, test_label=test_label)
        open_run(run_id)
    except Exception as exc:
        notice(explain_error(exc), error=True)
        st.session_state["business_diagnostic"] = error_details(exc, include_traceback=True)


def render_import():
    with st.expander("导入一个本地批次", expanded=True):
        method = st.selectbox("导入方式", ("本机 HDF5 路径", "上传 HDF5 文件", "SO-100 数据目录"), key="import_method")
        st.text_input("批次名称", key="import_label", placeholder="例如：抓取演示 · 第一批")
        if method == "上传 HDF5 文件":
            st.file_uploader("选择 HDF5 文件", type=["hdf5", "h5"], key="import_upload")
        else:
            st.text_input("本机输入路径", key="import_path", placeholder="填写完整文件或数据目录路径")
        with st.expander("故障副本标记（仅测试输入填写）"):
            st.text_input("测试副本说明", key="import_test_label", placeholder="例如：用于验证 NaN 隔离的独立测试副本")
            st.caption("填写后记录为测试输入；此标签不替代结构检查，也不会让异常自动通过。")
        st.button("开始导入", type="primary", on_click=start_import)


def sync_last_run():
    run_id = st.session_state.get("business_run_id")
    if not run_id:
        return
    try:
        state = ui_runs.load_run(run_id)
        if state.get("status") not in ui_runs.ACTIVE_STATUSES:
            service().reconcile_run(run_id)
    except Exception as exc:
        st.warning(f"最近运行的业务登记尚未完成：{explain_error(exc)}")
        with st.expander("业务回写诊断"):
            st.json(error_details(exc, include_traceback=True))


def recover_registration(run_id: str):
    try:
        registration = service().reconcile_run(run_id)
        notice(registration.get("message", "已核对运行产物与批次版本。"), error=registration.get("status") in {"failed", "stale"})
    except Exception as exc:
        notice(explain_error(exc), error=True)
        st.session_state["business_diagnostic"] = error_details(exc, include_traceback=True)


def render_run_context(state: dict):
    request = state.get("request", {})
    if request.get("kind") != "business":
        return
    registration = state.get("business_registration") or {}
    batch_id = state.get("batch_id") or request.get("batch_id") or registration.get("batch_id")
    st.write(f"**业务操作：{run_label(state)}**")
    snapshot = request.get("snapshot") or request.get("business_snapshot") or {}
    content_version = request.get("content_version") or snapshot.get("content_version")
    if content_version:
        st.caption(f"本次处理绑定内容版本 {content_version}")
    if batch_id:
        st.caption(f"关联批次 {batch_id}")
        st.button("返回关联批次", key=f"return_batch_{state['run_id']}", on_click=open_batch, args=(batch_id,))
    status = registration.get("status")
    messages = {"registered": "处理结果已登记到批次。", "pending": "处理产物等待登记；业务完成状态以登记结果为准。",
                "stale": "批次内容已变化，此运行产物作为历史保留，不能作为当前输出。", "failed": "处理产物登记失败，请查看诊断并核对版本。"}
    if status:
        renderer = st.success if status == "registered" else st.warning
        renderer(registration.get("message") or messages.get(status, "请查看业务登记记录。"))
    if state.get("status") not in ui_runs.ACTIVE_STATUSES and status in {"pending", "failed"}:
        st.button("核对并恢复业务登记", key=f"recover_business_{state['run_id']}", on_click=recover_registration, args=(state["run_id"],))
    if message := st.session_state.pop("business_notice", None):
        (st.error if message["error"] else st.success)(message["message"])
    if details := st.session_state.pop("business_diagnostic", None):
        with st.expander("业务登记原始诊断"):
            st.json(details)


@st.fragment(run_every="1s")
def render_associated_runs(batch_id: str):
    if st.session_state.get("page") not in (*BUSINESS_PAGES, "batch_tasks", "batch_report", "batch_source"):
        if st.session_state.pop("navigation_pending", False):
            st.rerun(scope="app")
        return
    try:
        matches = [state for state in ui_runs.available_runs()
                   if (state.get("batch_id") or state.get("request", {}).get("batch_id")) == batch_id]
    except Exception as exc:
        st.warning(f"相关运行暂时无法读取：{explain_error(exc)}")
        return
    if not matches:
        return
    refresh_key = f"associated_run_signature_{batch_id}"
    signature = [(state["run_id"], state.get("status"), state.get("message"),
                  state.get("business_registration", {}).get("status")) for state in matches[:5]]
    previous = st.session_state.get(refresh_key)
    st.session_state[refresh_key] = signature
    if previous is not None and previous != signature:
        st.rerun(scope="app")
    with st.expander("相关程序运行与日志", expanded=any(item.get("status") in ui_runs.ACTIVE_STATUSES for item in matches)):
        for state in matches[:5]:
            active = next((node for node in state.get("nodes", []) if node.get("status") == "running"), None)
            st.write(f"**{run_label(state)}**")
            st.markdown(status_badge(state.get("status"), ui_runs.RUN_STATUS.get(state.get("status"))), unsafe_allow_html=True)
            st.caption(f"{short_id(state['run_id'])} · 当前节点：{active.get('label') if active else state.get('message', '查看节点记录')}")
            st.button("查看节点与日志", key=f"batch_run_{state['run_id']}", on_click=open_run, args=(state["run_id"],))
        st.caption("后台状态变化后会自动刷新；关闭浏览器不终止处理。")


def batch_stage(batch: dict) -> str:
    if any(item.get("current") for item in batch.get("deliveries", [])):
        return "已登记交付"
    if any(item.get("current") for item in batch.get("outputs", [])):
        return "转换已验证"
    kept = [ep for ep in batch["episodes"] if ep["disposition"] == "keep"]
    if batch["cleaning_confirmed"] and kept and all(ep["review"]["status"] == "approved" for ep in kept):
        return "待转换"
    if batch["cleaning_confirmed"]:
        return "标注与审核"
    if any(ep["quality"]["status"] != "not_checked" for ep in batch["episodes"]):
        return "待确认筛选"
    return "待质检"


def inventory(batch: dict):
    return pd.DataFrame([{
        "源任务": ep["episode_index"], "记录数": ep["row_count"],
        "质检": QUALITY.get(ep["quality"]["status"], ep["quality"]["status"]),
        "数据问题数": ep["quality"].get("issue_count", 0),
        "筛选": DISPOSITIONS[ep["disposition"]], "原因": ep["selection_reason"],
        "标注审核": REVIEW[ep["review"]["status"]],
        "机器人执行结果": OUTCOMES.get(ep["annotation"]["outcome"], ep["annotation"]["outcome"]),
    } for ep in batch["episodes"]])


def render_summary(batch: dict, *, compact=False):
    if not compact:
        columns = st.columns(3)
        columns[0].metric("导入任务 / 记录", f"{batch['total_episodes']} / {batch['total_rows']:,}")
        columns[1].metric("候选任务 / 记录", f"{batch['counts']['keep']['episodes']} / {batch['counts']['keep']['rows']:,}")
        columns[2].metric("待审核保留任务", sum(ep["disposition"] == "keep" and ep["review"]["status"] != "approved" for ep in batch["episodes"]))
    source = batch.get("source", {})
    if source.get("test_label"):
        st.warning(f"明确标注的测试副本：{source['test_label']}。其指纹独立于公开原始样本。")
    with st.expander("批次详情与来源"):
        st.caption(f"内容版本 {batch['content_version']} · 保存修订 {batch['revision']}")
        st.write("批次编号")
        st.code(batch["batch_id"], language=None)
        if source.get("input_fingerprint"):
            st.write("来源指纹")
            st.code(source["input_fingerprint"], language=None)
        for warning in source.get("warnings", []):
            st.info(f"来源说明：{warning.get('message', str(warning)) if isinstance(warning, dict) else warning}")
        st.json(source)


def choose_episode(batch: dict, *, only_kept=False):
    episodes = [ep for ep in batch["episodes"] if not only_kept or ep["disposition"] == "keep"]
    if not episodes:
        st.info("当前没有保留任务，请先完成数据清洗与数据筛选。")
        return None
    indices = [ep["episode_index"] for ep in episodes]
    if st.session_state.get("business_episode") not in indices:
        st.session_state["business_episode"] = indices[0]
    selected = st.selectbox("选择源任务", indices, key="business_episode", format_func=lambda index: f"任务 {index}")
    return next(ep for ep in episodes if ep["episode_index"] == selected)


def set_frame(key: str, value: int):
    st.session_state[key] = value


def render_preview(batch: dict, ep: dict):
    from .importing import preview_batch

    row_key = f"batch_frame_{batch['batch_id']}_{ep['episode_index']}"
    maximum = ep["row_count"] - 1
    st.session_state.setdefault(row_key, 0)
    st.session_state[row_key] = max(0, min(maximum, int(st.session_state[row_key])))
    with st.expander("图像与状态／动作预览", expanded=True):
        controls = st.columns([1, 1, 1, 4])
        for column, label, frame in zip(controls, ("首帧", "中间帧", "末帧"), (0, ep["row_count"] // 2, maximum)):
            column.button(label, key=f"{row_key}_{label}", on_click=set_frame, args=(row_key, frame))
        st.slider("任务内记录位置", min_value=0, max_value=max(1, maximum), key=row_key, disabled=maximum == 0)
        row = int(st.session_state[row_key])
        try:
            preview = preview_batch(batch, ep["episode_index"], row)
            left, right = st.columns([1.3, 1])
            if preview.get("image") is not None:
                left.image(preview["image"], width="stretch")
            else:
                left.warning("此记录暂无可读取图像；请查看质量问题与文件证据。")
            right.write(f"**源任务 {ep['episode_index']} · 记录 {row + 1} / {ep['row_count']}**")
            right.caption("导入后的原始预览不代表训练格式转换已经完成。")
            table = preview.get("table")
            if isinstance(table, pd.DataFrame) and len(table):
                current = table.iloc[min(row, len(table) - 1)]
                for field, names_key, label in (("observation.state", "state_names", "状态"), ("action", "action_names", "动作")):
                    if field not in table:
                        continue
                    names = preview.get(names_key, [])
                    try:
                        right.dataframe(pd.DataFrame({f"{label}字段": names, "原始值": current[field]}), hide_index=True, width="stretch")
                        values = np.stack(table[field].to_numpy()).astype(float)
                        if values.ndim != 2 or values.shape[1] != len(names):
                            raise ValueError("字段维度与预览说明不一致")
                        x = table["timestamp"] if "timestamp" in table else np.arange(len(table))
                        figure = go.Figure()
                        for index, name in enumerate(names):
                            figure.add_trace(go.Scatter(x=x, y=values[:, index], name=name, mode="lines", line={"width": 1.3, "color": COLORS[index % len(COLORS)]}))
                        cursor = float(x.iloc[row] if hasattr(x, "iloc") else x[row])
                        if np.isfinite(cursor):
                            figure.add_vline(x=cursor, line_dash="dot", line_color="#0f172a")
                        apply_signal_layout(figure, x_title="记录时间 / s" if "timestamp" in table else "记录位置")
                        st.subheader(f"原始{label}值")
                        st.plotly_chart(figure, width="stretch", key=f"{row_key}_{field}")
                    except (TypeError, ValueError, IndexError) as exc:
                        st.warning(f"{label}预览不完整：{explain_error(exc)}")
            source = preview.get("source", {})
            st.caption(source.get("timestamp_semantics", "保留来源数值。记录时间与视频定位不代表硬件同步验证。"))
            if preview.get("issues"):
                with st.expander("预览检查信息"):
                    st.json(preview["issues"])
        except Exception as exc:
            failure("任务预览失败", exc)


def quality_evidence(batch: dict, ep: dict):
    issues = ep["quality"].get("issues", [])
    if issues:
        st.warning(f"任务 {ep['episode_index']} · {issues[0].get('message', '发现数据问题')}" + (f"（共 {len(issues)} 项）" if len(issues) > 1 else ""))
        st.dataframe(pd.DataFrame([{"规则": i.get("rule"), "行": i.get("row_index"), "字段／文件": i.get("field", i.get("file", "")), "问题": i.get("message", "")} for i in issues]), hide_index=True, width="stretch")
        chosen = st.selectbox("选择问题证据", range(len(issues)), format_func=lambda index: f"{index + 1}. {issues[index].get('message', '')}", key=f"biz_issue_{batch['batch_id']}_{ep['episode_index']}")
        issue = issues[chosen]
        if issue.get("row_index") is not None:
            row_key = f"batch_frame_{batch['batch_id']}_{ep['episode_index']}"
            st.button("定位到问题记录", on_click=set_frame, args=(row_key, int(issue["row_index"])))
        with st.expander("完整问题与文件证据"):
            st.json(issue)
    run_id = ep["quality"].get("run_id")
    if run_id:
        st.button("查看对应质检运行", on_click=open_run, args=(run_id,))


def save_disposition(batch_id: str, ep_id: int, revision: int, prefix: str):
    perform(lambda: service().get_store().set_disposition(batch_id, ep_id,
        st.session_state[prefix + "decision"], reason=st.session_state[prefix + "reason"], expected_revision=revision),
        "筛选决定已保存。确认候选集后再提交标注。")


def render_selection(batch: dict):
    st.caption("逐任务保留、隔离或排除；原始文件不删帧、不覆盖。筛选变化后需要重新确认并审核。")
    counts = batch["counts"]
    st.write(" · ".join(f"{label} {counts[key]['episodes']} 条 / {counts[key]['rows']} 帧" for key, label in DISPOSITIONS.items()))
    if batch["cleaning_confirmed"]:
        st.markdown(status_badge("completed", "候选集已确认"), unsafe_allow_html=True)
        st.button("前往数据标注", type="primary", on_click=goto_node, args=("annotate",))
        st.caption("筛选或质检变化后，需要重新确认候选集。")
    else:
        kept = [item for item in batch["episodes"] if item["disposition"] == "keep"]
        unchecked = sum(item["quality"]["status"] != "passed" for item in kept)
        reason = "暂无保留任务，请先选择通过质检的任务。" if not kept else (f"还有 {unchecked} 条保留任务未通过质检，请隔离、排除或重新检查。" if unchecked else "")
        st.button("确认筛选候选集", type="primary", disabled=bool(reason), on_click=perform,
            args=(lambda: service().get_store().confirm_cleaning(batch["batch_id"], expected_revision=batch["revision"]), "候选集已确认，可开始数据标注。"))
        st.caption(reason or "确认后进入数据标注；请先保存下方的筛选决定。")
    st.dataframe(styled_status_table(inventory(batch)), hide_index=True, width="stretch")
    ep = choose_episode(batch)
    if ep:
        prefix = f"disposition_{batch['batch_id']}_{ep['episode_index']}_"
        hydration = prefix + "revision"
        if st.session_state.get(hydration) != batch["revision"]:
            st.session_state[prefix + "decision"] = ep["disposition"]
            st.session_state[prefix + "reason"] = ep["selection_reason"]
            st.session_state[hydration] = batch["revision"]
        actions, preview = st.columns([1, 1.8], gap="large")
        with actions:
            st.markdown(status_badge(ep["quality"]["status"], QUALITY.get(ep["quality"]["status"])), unsafe_allow_html=True)
            with st.form(prefix + "form"):
                st.selectbox("筛选决定", list(DISPOSITIONS), format_func=DISPOSITIONS.get, key=prefix + "decision")
                st.text_area("处理原因（隔离或排除必填）", key=prefix + "reason")
                st.form_submit_button("保存筛选决定", type="secondary", on_click=save_disposition, args=(batch["batch_id"], ep["episode_index"], batch["revision"], prefix))
            quality_evidence(batch, ep)
        with preview:
            render_preview(batch, ep)


def save_annotation(batch_id: str, ep_id: int, revision: int, prefix: str, submit: bool):
    def action():
        raw_tags = st.session_state[prefix + "tags"].replace("，", ",")
        updated = service().get_store().save_annotation(batch_id, ep_id,
            task=st.session_state[prefix + "task"], outcome=st.session_state[prefix + "outcome"],
            tags=[part.strip() for part in raw_tags.split(",") if part.strip()],
            notes=st.session_state[prefix + "notes"], expected_revision=revision)
        if submit:
            service().get_store().submit_annotation(batch_id, ep_id, expected_revision=updated["revision"])
    perform(action, "标注已保存并提交审核。" if submit else "草稿已保存；修改后的标注需要重新提交审核。")


def review_annotation(batch_id: str, ep_id: int, revision: int, approve: bool, reason_key: str):
    try:
        updated = service().get_store().review_annotation(batch_id, ep_id, approve=approve,
            reason=st.session_state.get(reason_key, ""), expected_revision=revision)
        kept = [ep for ep in updated["episodes"] if ep["disposition"] == "keep"]
        if approve and kept and all(ep["review"]["status"] == "approved" for ep in kept):
            if updated["source"].get("kind") == "hdf5":
                try:
                    run_id = service().start_final_quality(batch_id, updated["revision"], auto_continue=True)
                    notice("全部保留任务已审核，后台将执行最终质检，通过后自动转换并验证交付包。")
                    open_run(run_id)
                except Exception as exc:
                    notice(f"审核已保存，自动处理未能启动：{explain_error(exc)}。请在数据质检页重新启动。", error=True)
            else:
                notice("审核已保存。当前 SO-100 配置可继续最终质检；转换与交付暂只支持 HDF5。")
        else:
            notice("审核通过，等待其余保留任务审核。" if approve else "已退回，请到数据标注页修改后重新提交。")
    except Exception as exc:
        notice(explain_error(exc), error=True)
        st.session_state["business_diagnostic"] = error_details(exc, include_traceback=True)


def render_annotations(batch: dict):
    if not batch["cleaning_confirmed"]:
        st.warning("请先确认清洗候选集，再提交审核。")
    ep = choose_episode(batch, only_kept=True)
    if not ep:
        return
    prefix = f"annotation_{batch['batch_id']}_{ep['episode_index']}_"
    if st.session_state.get(prefix + "revision") != batch["revision"]:
        for field in ("task", "outcome", "notes"):
            st.session_state[prefix + field] = ep["annotation"][field]
        st.session_state[prefix + "tags"] = ", ".join(ep["annotation"]["tags"])
        st.session_state[prefix + "revision"] = batch["revision"]
    actions, preview = st.columns([1, 1.8], gap="large")
    with actions:
        st.markdown(status_badge(ep["review"]["status"], REVIEW[ep["review"]["status"]]), unsafe_allow_html=True)
        if ep["review"].get("reason"):
            st.info(ep["review"]["reason"])
        with st.form(prefix + "form"):
            st.text_area("任务描述", key=prefix + "task")
            st.selectbox("机器人任务执行结果", list(OUTCOMES), format_func=OUTCOMES.get, key=prefix + "outcome")
            st.text_input("标签（逗号分隔）", key=prefix + "tags")
            st.text_area("标注备注", key=prefix + "notes")
            st.form_submit_button("保存并提交审核", type="primary", on_click=save_annotation, args=(batch["batch_id"], ep["episode_index"], batch["revision"], prefix, True), disabled=not batch["cleaning_confirmed"])
            if not batch["cleaning_confirmed"]:
                st.caption("无法提交：候选集尚未确认，请先返回数据筛选。")
            else:
                st.caption("提交需填写任务描述，并选择任务执行结果。")
            st.form_submit_button("保存草稿", type="secondary", on_click=save_annotation, args=(batch["batch_id"], ep["episode_index"], batch["revision"], prefix, False))
        st.button("前往标注审核", on_click=goto_node, args=("review",))
        st.caption("机器人执行失败可以是有效示教记录，与数据损坏不同。")
    with preview:
        render_preview(batch, ep)


def render_reviews(batch: dict):
    kept = [item for item in batch["episodes"] if item["disposition"] == "keep"]
    pending = sum(item["review"]["status"] != "approved" for item in kept)
    all_approved = bool(kept) and not pending and batch["cleaning_confirmed"]
    if all_approved:
        st.markdown(status_badge("approved", "全部保留任务审核通过"), unsafe_allow_html=True)
        st.button("前往数据质检", type="primary", on_click=goto_node, args=("quality",))
    elif pending:
        st.caption(f"还有 {pending} 条保留任务未完成审核。")
    st.dataframe(styled_status_table(inventory(batch)), hide_index=True, width="stretch")
    ep = choose_episode(batch, only_kept=True)
    if not ep:
        return
    actions, preview = st.columns([1, 1.8], gap="large")
    with actions:
        review_status = ep["review"]["status"]
        st.markdown(status_badge(review_status, REVIEW[review_status]), unsafe_allow_html=True)
        annotation = ep["annotation"]
        st.write("**任务描述**")
        st.write(annotation.get("task") or "尚未填写")
        st.write(f"**机器人任务执行结果：** {OUTCOMES.get(annotation.get('outcome'), '尚未标注')}")
        st.write(f"**标签：** {'、'.join(annotation.get('tags', [])) or '无'}")
        if annotation.get("notes"):
            st.write("**标注备注**")
            st.write(annotation["notes"])
        with st.expander("原始标注 JSON"):
            st.json(annotation)
        if ep["review"].get("reason"):
            st.info(ep["review"]["reason"])
        reason_key = f"review_{batch['batch_id']}_{ep['episode_index']}_reason"
        st.text_input("审核意见／退回原因（退回必填）", key=reason_key)
        can_review = review_status == "submitted" and batch["cleaning_confirmed"]
        st.button("审核通过", type="secondary" if all_approved else "primary", disabled=not can_review, on_click=review_annotation, args=(batch["batch_id"], ep["episode_index"], batch["revision"], True, reason_key))
        if not can_review:
            if not batch["cleaning_confirmed"]:
                st.caption("无法审核：候选集已变化，请先重新确认。")
            elif review_status == "approved":
                st.caption("此任务已审核通过，请选择其他待审核任务。" if pending else "此任务已审核通过。")
            else:
                st.caption("无法审核：此任务尚未提交，请在数据标注中保存并提交。")
                st.button("前往数据标注", on_click=goto_node, args=("annotate",))
        st.button("退回修改", type="secondary", disabled=not can_review or not st.session_state.get(reason_key, "").strip(), on_click=review_annotation, args=(batch["batch_id"], ep["episode_index"], batch["revision"], False, reason_key))
        if can_review and not st.session_state.get(reason_key, "").strip():
            st.caption("退回前请填写具体原因。")
        st.caption("最后一条保留任务审核通过后，HDF5 批次自动继续最终质检 → 转换 → 打包验证。")
    with preview:
        render_preview(batch, ep)


def render_conversion(batch: dict, *, final_quality_passed=False, current_output_valid=False, active=False):
    blocked = []
    kept = [ep for ep in batch["episodes"] if ep["disposition"] == "keep"]
    pending = sum(ep["review"]["status"] != "approved" for ep in kept)
    if active:
        blocked.append("当前批次正在处理，请等待当前运行完成。")
    if pending:
        blocked.append(f"还有 {pending} 条保留任务未完成审核，请先完成标注审核。")
    if not final_quality_passed:
        blocked.append("当前内容版本尚未通过最终数据质检，请先完成数据质检节点。")
    if batch["source"].get("kind") != "hdf5":
        blocked.append("当前业务转换仅支持已验证的 HDF5 配置；SO-100 可导入、预览、质检和标注。")
    try:
        spec = service().get_store().conversion_spec(batch["batch_id"])
    except Exception as exc:
        blocked.append(explain_error(exc))
        spec = None
    if current_output_valid:
        st.markdown(status_badge("passed", "当前转换产物已验证"), unsafe_allow_html=True)
        st.button("前往打包交付", type="primary", on_click=goto_node, args=("deliver",))
    st.button("重新转换并验证" if current_output_valid else "转换并验证候选集", type="secondary" if current_output_valid else "primary", disabled=bool(blocked), on_click=start_operation,
              args=(service().start_batch_conversion, batch["batch_id"], batch["revision"]))
    if blocked:
        # 区分两类原因：批次类型不在转换范围内（设计边界）与前置条件未满足（可推进）。
        scope_notes = [m for m in blocked if "仅支持已验证的 HDF5" in m]
        prerequisites = [m for m in blocked if m not in scope_notes]
        if scope_notes:
            st.info(
                "**当前批次类型不在格式转换范围内**（这是设计边界，不是故障）。\n\n"
                f"{scope_notes[0]}\n\n"
                "SO-100 本身就是 LeRobot v2.0 数据集，已经是官方格式，本节点无需再写入；"
                "要看本平台的转换链路，请在左侧「当前批次」切换到 HDF5 批次（Panda / Lift）。"
            )
        if prerequisites:
            st.warning("**转换前置条件未满足：**\n\n"
                       + "\n".join(f"- {m}" for m in prerequisites))
        if len(blocked) > 2:
            with st.expander("查看全部前置要求"):
                for message in blocked:
                    st.write(message)
    if spec:
        st.dataframe(pd.DataFrame([{"源任务": ep["source_episode_index"], "输出任务": ep["output_episode_index"], "记录数": ep["row_count"], "人工任务描述": ep["annotation"]["task"]} for ep in spec["episodes"]]), hide_index=True, width="stretch")
        st.caption("保留来源原始描述，人工标注映射随输出记录。")
        with st.expander("候选快照详情"):
            st.code(spec["snapshot_id"], language=None)
            st.json(spec)
    st.subheader("转换产物历史")
    if not batch.get("outputs"):
        st.info("尚无已登记转换产物。运行完成不自动等于数据已经验证。")
    for output in reversed(batch.get("outputs", [])):
        with st.container(border=True):
            current = bool(output.get("current") and current_output_valid)
            st.markdown(status_badge("passed" if current else "stale", "当前产物 · 已验证" if current else "历史／已过期"), unsafe_allow_html=True)
            st.caption(f"{short_id(output['output_id'])} · 内容版本 {output['content_version']}")
            st.button("查看格式转换运行", key=f"output_run_{output['output_id']}", on_click=open_run, args=(output["run_id"],))
            with st.expander("产物与验证证据"):
                st.write("产物编号")
                st.code(output["output_id"], language=None)
                st.write("转换运行编号")
                st.code(output["run_id"], language=None)
                st.json(output)
            if current:
                render_output_inventory(output)
                render_output_preview(output)
            elif output.get("current"):
                st.caption("当前流程的输入或验证证据已失效；此产物仅供历史追溯，不提供当前训练数据下载。")


def render_output_inventory(output: dict):
    from .dataset import dataset_fingerprint
    try:
        root = Path(output["artifact_path"])
        if dataset_fingerprint(root) != output.get("validation", {}).get("output_fingerprint"):
            raise ValueError("转换文件指纹已变化，不能下载为当前已验证训练数据")
        records = []
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in {".parquet", ".mp4", ".json", ".md"}:
                records.append({"文件": path.relative_to(root).as_posix(), "字节": path.stat().st_size,
                                "SHA-256": hashlib.sha256(path.read_bytes()).hexdigest()})
        st.markdown("**实际训练数据文件清单**")
        display_records = [{"文件": record["文件"], "字节": record["字节"]} for record in records]
        st.dataframe(pd.DataFrame(display_records), hide_index=True, width="stretch")
        snapshot = output["snapshot"]
        manifest = {"output_id": output["output_id"], "content_version": output["content_version"],
            "format_version": "LeRobotDataset v3.0", "episode_count": len(snapshot["episodes"]),
            "row_count": sum(e["row_count"] for e in snapshot["episodes"]),
            "snapshot_id": snapshot["snapshot_id"], "output_fingerprint": output["validation"]["output_fingerprint"], "files": records}
        st.caption(f"LeRobotDataset v3.0 · {manifest['episode_count']} 条任务 · {manifest['row_count']} 条记录。完整数据由打包交付节点导出 ZIP。")
        with st.expander("文件校验值详情"):
            for record in records:
                st.caption(record["文件"])
                st.code(record["SHA-256"], language=None)
        st.download_button("下载训练数据文件清单 JSON", json.dumps(manifest, ensure_ascii=False, indent=2),
            f"{output['output_id']}-files.json", "application/json", key=f"output_inventory_{output['output_id']}")
        st.download_button("下载格式转换证据 JSON", json.dumps(output["validation"], ensure_ascii=False, indent=2),
            f"{output['output_id']}-verification.json", "application/json", key=f"output_proof_{output['output_id']}")
    except Exception as exc:
        failure("实际训练数据清单无法核验", exc)


def render_output_preview(output: dict):
    from .dataset import dataset_fingerprint, load_episode
    from .video import read_video_frame

    with st.expander("预览当前 LeRobot 输出与视频偏移"):
        try:
            root = Path(output["artifact_path"])
            if dataset_fingerprint(root) != output.get("validation", {}).get("output_fingerprint"):
                raise ValueError("转换产物与登记指纹不一致，请查看转换运行后重新验证")
            episodes = output["snapshot"]["episodes"]
            mapping = {item["output_episode_index"]: item["source_episode_index"] for item in episodes}
            key = output["output_id"]
            selected = st.selectbox("输出任务", list(mapping), key=f"output_episode_{key}",
                format_func=lambda value: f"输出任务 {value} ← 源任务 {mapping[value]}")
            episode = load_episode(root, selected)
            count = len(episode.table)
            row_key = f"output_row_{key}_{selected}"
            row = st.slider("输出记录位置", 0, max(1, count - 1), key=row_key, disabled=count < 2)
            row = min(row, count - 1)
            record = episode.table.iloc[row]
            timestamp = float(record["timestamp"])
            frame = read_video_frame(episode.video_path, timestamp + episode.video_start_time)
            a, b = st.columns([1.2, 1])
            a.image(frame["image"], width="stretch")
            b.caption(f"LeRobot v3.0 · 源任务 {mapping[selected]} → 输出任务 {selected} · 记录 {row + 1} / {count}")
            b.caption(f"任务时间 {timestamp:.6f} s · 文件偏移 {episode.video_start_time:.6f} s · 视频 PTS {frame['pts_time']:.6f} s")
            b.dataframe(pd.DataFrame({"状态字段": episode.state_names, "状态值": record["observation.state"]}), hide_index=True, width="stretch")
            b.dataframe(pd.DataFrame({"动作字段": episode.action_names, "动作值": record["action"]}), hide_index=True, width="stretch")
        except Exception as exc:
            failure("当前转换输出无法预览", exc)


def render_delivery(batch: dict, *, conversion_passed=False, current_delivery_valid=False, active=False):
    from .delivery import verified_delivery

    current_items = [item for item in reversed(batch.get("outputs", [])) if item.get("current")]
    has_current_delivery = current_delivery_valid and any(item.get("current") for item in batch.get("deliveries", []))
    if current_items:
        chosen = st.selectbox("选择当前已验证输出", range(len(current_items)), format_func=lambda index: f"内容版本 {current_items[index]['content_version']} · {short_id(current_items[index]['output_id'])}", key=f"delivery_output_{batch['batch_id']}")
        output = current_items[chosen]
        blocked_reason = ""
        try:
            service().get_store().delivery_spec(batch["batch_id"], output["output_id"])
        except Exception as exc:
            blocked_reason = explain_error(exc)
        if not conversion_passed:
            blocked_reason = blocked_reason or "转换结果或上游证据尚未通过验证，请先完成格式转换。"
        if active:
            blocked_reason = "当前批次正在处理，请等待当前运行完成。"
        st.button("重新生成并验证交付包" if has_current_delivery else "生成并验证交付包", type="secondary" if has_current_delivery else "primary", disabled=bool(blocked_reason), on_click=start_operation,
                  args=(service().start_delivery, batch["batch_id"], output["output_id"], batch["revision"]))
        if blocked_reason:
            st.caption("暂不能打包：" + blocked_reason)
        with st.expander("所选转换产物详情"):
            st.code(output["output_id"], language=None)
    else:
        st.warning("没有当前有效的已验证输出，请先完成候选集转换。")
        st.button("前往格式转换", type="primary", on_click=goto_node, args=("convert",))
    st.subheader("交付记录")
    if not batch.get("deliveries"):
        st.info("尚无已登记交付包。")
    primary_download_used = False
    for item in reversed(batch.get("deliveries", [])):
        with st.container(border=True):
            is_current = bool(item.get("current") and current_delivery_valid)
            st.markdown(status_badge("passed" if is_current else "stale", "当前交付包 · 已验证" if is_current else "历史／已过期"), unsafe_allow_html=True)
            st.caption(f"{short_id(item['delivery_id'])} · 内容版本 {item['content_version']}")
            with st.expander("交付详情与校验值"):
                st.write("交付编号")
                st.code(item["delivery_id"], language=None)
                st.write("打包运行编号")
                st.code(item["packaging_run_id"], language=None)
                st.write("登记的交付包 SHA-256")
                st.code(item.get("zip_sha256", "未登记"), language=None)
                st.json(item)
            st.button("查看打包与验证运行", key=f"delivery_run_{item['delivery_id']}", on_click=open_run, args=(item["packaging_run_id"],))
            if not item.get("current"):
                st.caption("业务内容已变化，此历史包不作为当前版本提供下载。")
                continue
            if not current_delivery_valid:
                st.caption("当前流程的输入、检查证据或交付包已失效；此包仅供历史追溯，不作为当前有效交付下载。")
                continue
            try:
                verified = verified_delivery(batch, item["delivery_id"])
                path = Path(verified["path"])
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != verified["sha256"]:
                    raise ValueError("交付包在校验后发生变化，已停止提供下载")
                st.write(f"{verified['size_bytes'] / 1024 / 1024:.2f} MB · {verified.get('format_version', 'LeRobot')} · {verified.get('episode_count', '—')} 条任务 · {verified.get('row_count', '—')} 帧")
                st.download_button("下载已验证交付包 ZIP", data, path.name, "application/zip", type="secondary" if primary_download_used else "primary", key=f"download_{item['delivery_id']}")
                primary_download_used = True
                st.button("重新独立验证此包", disabled=active, key=f"verify_delivery_{item['delivery_id']}", on_click=start_operation,
                    args=(service().start_verify_delivery, str(path)))
                if active:
                    st.caption("当前批次正在处理，请等待结束后重新验证。")
            except Exception as exc:
                failure("此包不能作为当前有效交付下载", exc)


def render_audit(batch: dict):
    with st.expander("业务审计历史（独立于程序日志）"):
        events = batch.get("history", [])
        labels = {"create_batch": "登记批次", "apply_quality_report": "回写质检结果", "set_disposition": "修改筛选决定", "confirm_cleaning": "确认候选集",
                  "save_annotation": "保存标注草稿", "submit_annotation": "提交标注审核", "review_annotation": "人工审核", "register_verified_output": "登记已验证输出", "register_delivery": "登记交付包"}
        st.dataframe(pd.DataFrame([{"修订": item.get("revision"), "时间": item.get("at_utc"), "操作": labels.get(item.get("operation"), item.get("operation")), "决定与原因": json.dumps(item.get("detail", {}), ensure_ascii=False)} for item in events]), hide_index=True, width="stretch")
        st.download_button("下载业务审计 JSONL", service().get_store().audit_jsonl(batch["batch_id"]), f"{batch['batch_id']}-audit.jsonl", "application/x-ndjson")


def render_business_page(page: str):
    from .ui_flow import render
    render(LEGACY_ROUTES.get(page, page))
