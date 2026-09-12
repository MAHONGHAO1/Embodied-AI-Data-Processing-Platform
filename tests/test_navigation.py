"""Regressions for user-visible stage navigation and batch context."""

import re
import json

from streamlit.testing.v1 import AppTest

from robodata import importing, ui_business, ui_flow, ui_navigation, ui_runs
from robodata.config import PROJECT_ROOT
from test_app import navigate
from test_business_app import app_for, business_ui, make_batch


def test_main_navigation_keeps_one_batch_across_eight_nodes_and_history(business_ui):
    store, _ = business_ui
    batch = make_batch(store)
    store.create_batch("另一个批次", {"kind": "hdf5", "input_fingerprint": "unrelated"},
                       [{"episode_index": 9, "row_count": 99}])
    app = app_for(batch["batch_id"])
    expected_options = [ui_navigation.main_label(page) for page in ui_navigation.MAIN_OPTIONS]
    for page in ui_navigation.MAIN_OPTIONS:
        app.radio(key="main_navigation").set_value(page).run()
        assert not app.exception
        assert app.radio(key="main_navigation").options == expected_options
        if page == "runs":
            assert app.title[0].value == "运行记录"
            continue
        assert app.title[0].value == ui_navigation.ROUTE_LABELS[page]
        assert app.session_state["selected_batch_id"] == batch["batch_id"]
        assert app.query_params["batch_id"] == [batch["batch_id"]]
        assert app.selectbox(key="batch_picker").value == batch["batch_id"]
        assert "data_mode" not in [item.key for item in app.selectbox]
        assert not any(button.key.startswith("flow_nav_") for button in app.button if button.key)
        assert len([button for button in app.button if button.proto.type == "primary"]) <= 1
    navigate(app, "运行记录")
    assert app.title[0].value == "运行记录"
    assert app.radio(key="main_navigation").options == expected_options
    navigate(app, "select")
    assert app.session_state["selected_batch_id"] == batch["batch_id"]


def test_business_url_refresh_restores_page_and_clears_old_run_id(business_ui):
    store, _ = business_ui
    batch = make_batch(store)
    app = app_for(batch["batch_id"])
    navigate(app, "review")
    assert app.query_params["page"] == ["review"]
    refreshed = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=30)
    refreshed.query_params["batch_id"] = batch["batch_id"]
    refreshed.query_params["page"] = app.query_params["page"]
    refreshed.query_params["run_id"] = "obsolete-history-run"
    refreshed.run()
    assert not refreshed.exception
    assert refreshed.session_state["page"] == "review"
    assert refreshed.radio(key="main_navigation").value == "review"
    assert refreshed.session_state["selected_batch_id"] == batch["batch_id"]
    assert "run_id" not in refreshed.query_params
    refreshed.run()
    assert refreshed.title[0].value == "标注审核"


def test_batch_task_and_report_views_use_selected_batch_not_independent_sample(business_ui, monkeypatch):
    store, _ = business_ui
    batch = make_batch(store, bad=True)
    store.create_batch("不应显示的独立批次", {"kind": "hdf5", "input_fingerprint": "different-input"},
                       [{"episode_index": 9, "row_count": 99}])
    calls = []
    preview = importing.preview_batch

    def tracked_preview(selected_batch, episode_index, row):
        calls.append((selected_batch["batch_id"], episode_index, row))
        return preview(selected_batch, episode_index, row)

    monkeypatch.setattr(importing, "preview_batch", tracked_preview)
    app = app_for(batch["batch_id"])
    navigate(app, "batch_tasks")
    app.selectbox(key="business_episode").set_value(2).run()
    assert calls[-1][:2] == (batch["batch_id"], 2)
    assert app.selectbox(key="business_episode").options == ["任务 0", "任务 2"]
    assert "data_mode" not in [item.key for item in app.selectbox]
    navigate(app, "batch_report")
    app.selectbox(key="business_episode").set_value(2).run()
    assert app.session_state["selected_batch_id"] == batch["batch_id"]
    assert "data_mode" not in [item.key for item in app.selectbox]
    assert any("动作中包含 NaN" in str(item.value) for item in [*app.dataframe, *app.warning, *app.markdown])
    assert all(call[0] == batch["batch_id"] for call in calls)


def test_completed_flow_still_marks_the_stage_currently_being_viewed(business_ui, monkeypatch):
    store, _ = business_ui
    batch = make_batch(store)
    flow = ui_flow.empty_flow()
    for node in flow["nodes"]:
        node.update(status="completed", outcome="passed", reason="已完成的合成界面场景")
    monkeypatch.setattr(ui_flow, "flow_for", lambda selected: flow)
    app = app_for(batch["batch_id"])
    navigate(app, "clean")
    progress = next(item.value for item in app.markdown if '<div class="rd-progress"' in item.value)
    selected = re.findall(r'<a class="([^"]*rd-progress-step[^"]*)"[^>]*>(.*?)</a>', progress)
    assert len(selected) == len(ui_navigation.NODE_ORDER)
    assert any("selected" in classes and "数据清洗" in text for classes, text in selected)
    assert all("selected" not in classes for classes, text in selected if "数据清洗" not in text)
    assert progress.count(f"batch_id={batch['batch_id']}") == len(ui_navigation.NODE_ORDER)


def test_overview_source_view_and_advanced_tools_stay_in_context(business_ui):
    store, _ = business_ui
    batch = make_batch(store)
    app = app_for(batch["batch_id"])
    assert not next(item for item in app.expander if item.label == "高级工具").proto.expanded
    assert not any("本机单用户" in item.value or "每次保存核对版本" in item.value for item in app.caption)
    next(b for b in app.button if b.label == "浏览本批次任务").click().run()
    assert app.session_state["page"] == "batch_tasks"
    navigate(app, "batch_source")
    table = next(item.value for item in app.dataframe if "分量顺序" in item.value.columns)
    assert list(table["维度"]) == [2, 1]
    assert list(table["分量顺序"]) == ["位置 0、位置 1", "动作 0"]
    assert app.query_params["batch_id"] == [batch["batch_id"]]


def test_batch_report_history_is_scoped_downloadable_and_survives_bad_report(business_ui, tmp_path, monkeypatch):
    store, _ = business_ui
    batch = make_batch(store)
    final = tmp_path / "final_quality.json"
    output = tmp_path / "quality_report.json"
    for path, count in ((final, 18), (output, 22)):
        path.write_text(json.dumps({"summary": {"episode_count": 2, "row_count": 10,
            "issue_count": 0, "coverage_performed": count, "coverage_total": count}, "episodes": []}), encoding="utf-8")
        path.with_suffix(".html").write_text(f"<html><body>{count}</body></html>", encoding="utf-8")
    selected = {"run_id": "batch-history", "batch_id": batch["batch_id"], "started_at": "2026-09-10T12:00:00+00:00",
        "request": {"kind": "business", "operation": "final_quality", "content_version": 2},
        "report_path": str(output), "artifacts": {"final_quality_report": str(final)}}
    unrelated = {**selected, "run_id": "unrelated-history", "batch_id": "another-batch"}
    monkeypatch.setattr(ui_runs, "available_runs", lambda: [selected, unrelated])
    opened = []
    monkeypatch.setattr(ui_business, "open_run", lambda run_id: opened.append(run_id))
    app = app_for(batch["batch_id"], "batch_report")
    picker = app.selectbox(key=f"batch_report_history_{batch['batch_id']}")
    assert len(picker.options) == 2
    assert all("unrelated-history" not in option for option in picker.options)
    assert "最终数据质检" in picker.options[0] and "输出质检" in picker.options[1]
    for label in ("最终数据质检", "输出质检"):
        app.selectbox(key=picker.key).set_value(f"batch-history:{label}").run()
        assert not app.exception
        assert {"下载历史报告 JSON", "下载历史报告 HTML"}.issubset({b.label for b in app.get("download_button")})
        assert any("原运行时间 2026-09-10T12:00:00+00:00" in item.value for item in app.caption)
    output.write_text("{broken", encoding="utf-8")
    app.run()
    assert not app.exception
    assert any("历史报告无法读取" in item.value for item in app.error)
    assert not any(b.label == "下载历史报告 JSON" for b in app.get("download_button"))
    app.button(key="batch_report_open_run").click().run()
    assert opened == ["batch-history"]


def test_sample_tools_drop_business_report_context_and_keep_batch(business_ui, monkeypatch):
    store, _ = business_ui
    batch = make_batch(store)
    app = app_for(batch["batch_id"])
    monkeypatch.setattr(ui_runs, "load_run", lambda run_id: {"request": {"kind": "business"}})
    app.session_state["selected_run_id"] = "old-business-run"
    app.session_state["history_data_root"] = "old-business-output"
    app.session_state["quality_report"] = {"summary": {"row_count": 999}}
    app.query_params["run_id"] = "old-business-run"
    navigate(app, "质检报告")
    assert "selected_run_id" not in app.session_state
    assert "history_data_root" not in app.session_state
    assert "run_id" not in app.query_params
    assert any(item.key == "data_mode" for item in app.selectbox)
    assert app.session_state["selected_batch_id"] == batch["batch_id"]
    navigate(app, "flow_overview")
    assert app.selectbox(key="batch_picker").value == batch["batch_id"]


def test_quarantined_count_flags_data_problems_but_not_manual_exclusion():
    """进度条用隔离数把「执行成功但有数据问题」和「干净通过」区分开。

    exclude 是人工决定不保留，正常批次（含真实闭环验收批次）也会出现，
    因此不能计入；否则正常批次会被误标成告警色。
    """
    mixed = {"episodes": [{"disposition": "keep"}, {"disposition": "quarantine"},
                          {"disposition": "exclude"}, {"disposition": "quarantine"}]}
    assert ui_flow._quarantined_count(mixed) == 2
    assert ui_flow._quarantined_count({"episodes": [{"disposition": "exclude"}]}) == 0
    assert ui_flow._quarantined_count({"episodes": [{"disposition": "keep"}]}) == 0
    assert ui_flow._quarantined_count({"episodes": []}) == 0
    assert ui_flow._quarantined_count(None) == 0
    assert ui_flow._quarantined_count({}) == 0
