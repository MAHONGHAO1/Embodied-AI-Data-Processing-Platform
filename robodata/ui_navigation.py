"""Navigation over the eight business nodes.

The eight nodes mirror the standard embodied-data delivery chain:

    上传／导入 → 数据清洗 → 数据筛选 → 数据标注
    → 标注审核 → 数据质检 → 格式转换 → 打包交付
"""

from __future__ import annotations

import streamlit as st

ROUTE_LABELS = {
    "flow_overview": "批次总览", "import": "上传／导入", "batch_tasks": "任务浏览",
    "batch_source": "来源与字段", "clean": "数据清洗", "select": "数据筛选",
    "annotate": "数据标注", "review": "标注审核", "quality": "数据质检",
    "convert": "格式转换", "batch_report": "质检报告", "deliver": "打包交付",
}

# 八个业务节点，顺序与交付链路一致。
NODE_ORDER = ("import", "clean", "select", "annotate", "review",
              "quality", "convert", "deliver")

BUSINESS_ROUTES = tuple(ROUTE_LABELS)
MAIN_BUSINESS = ("flow_overview",) + NODE_ORDER
MAIN_OPTIONS = MAIN_BUSINESS + ("runs",)

# 主导航之下的页面内二级导航。
SUB_ROUTES = {
    "flow_overview": ("flow_overview", "batch_tasks", "batch_source"),
    "quality": ("quality", "batch_report"),
}

TECHNICAL_ROUTES = ("数据概览", "任务浏览", "质检报告", "公开样本转换")
TECHNICAL_LABELS = {"数据概览": "样本数据概览", "任务浏览": "样本任务浏览",
                    "质检报告": "样本质检报告", "公开样本转换": "公开样本转换"}

# 以下两项为旧版五分组导航留下的兼容别名，保留给历史调用方与测试。
STAGE_LABELS = {
    "batches": "批次管理", "cleaning": "清洗筛选", "annotation": "标注审核",
    "conversion": "转换验证", "delivery": "交付中心", "runs": "运行记录",
}
STAGE_ROUTES = {
    "batches": ("flow_overview", "import", "batch_tasks", "batch_source"),
    "cleaning": ("clean", "select"), "annotation": ("annotate", "review"),
    "conversion": ("quality", "convert", "batch_report"), "delivery": ("deliver",),
}


def main_label(page: str) -> str:
    """主导航显示名：八个业务节点带 01–08 序号以强化链路感。"""
    if page == "runs":
        return "运行记录"
    label = ROUTE_LABELS.get(page, page)
    if page in NODE_ORDER:
        return f"{NODE_ORDER.index(page) + 1:02d} {label}"
    return label


def main_for(page: str) -> str | None:
    """当前页面所属的主导航项。"""
    if page == "运行记录":
        return "runs"
    if page in MAIN_OPTIONS:
        return page
    for parent, routes in SUB_ROUTES.items():
        if page in routes:
            return parent
    return None


def stage_for(page: str) -> str | None:
    return next((stage for stage, routes in STAGE_ROUTES.items() if page in routes), None)


def primary_for(page: str) -> str | None:
    return stage_for(page) or ("runs" if page == "运行记录" else None)


def navigate(page: str):
    st.session_state["page"] = page
    st.session_state["navigation_pending"] = True
    st.query_params["page"] = page
    if page in BUSINESS_ROUTES:
        st.query_params.pop("run_id", None)


def select_main():
    page = st.session_state["main_navigation"]
    navigate("运行记录" if page == "runs" else page)


def select_subpage(parent: str):
    navigate(st.session_state[f"section_view_{parent}"])


def sidebar(page: str):
    # 技术页面不属于任何业务节点，置空以取消主导航选中态。
    st.session_state["main_navigation"] = main_for(page)
    st.radio("工作台导航", MAIN_OPTIONS, key="main_navigation", index=None,
             label_visibility="collapsed", format_func=main_label,
             on_change=select_main)
    with st.expander("高级工具", expanded=page in TECHNICAL_ROUTES):
        for route in TECHNICAL_ROUTES:
            st.button(TECHNICAL_LABELS[route], key=f"technical_{route}",
                      on_click=open_sample_tool, args=(route,), width="stretch")


def open_sample_tool(page: str):
    if st.session_state.get("selected_run_id"):
        from . import ui_runs
        try:
            state = ui_runs.load_run(st.session_state["selected_run_id"])
            business_run = state.get("request", {}).get("kind") == "business"
        except Exception:
            business_run = True
        if business_run:
            for key in ("selected_run_id", "history_picker", "history_data_root", "quality_report"):
                st.session_state.pop(key, None)
            st.query_params.pop("run_id", None)
    navigate(page)


def subnavigation(page: str):
    parent = next((p for p, routes in SUB_ROUTES.items() if page in routes), None)
    if not parent or len(SUB_ROUTES[parent]) <= 1:
        return
    key = f"section_view_{parent}"
    st.radio("页面内导航", SUB_ROUTES[parent], key=key, horizontal=True,
             label_visibility="collapsed", format_func=ROUTE_LABELS.get,
             on_change=select_subpage, args=(parent,))
