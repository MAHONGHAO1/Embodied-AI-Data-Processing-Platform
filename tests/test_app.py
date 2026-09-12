"""Exercise the Chinese UI and real persisted asynchronous runs locally."""

import json
from pathlib import Path
import shutil
import time

import pytest
from streamlit.testing.v1 import AppTest

from robodata import config, ui_runs, ui_conversion, ui_navigation
from robodata.config import DEFAULT_DATA_ROOT, PROJECT_ROOT


HAS_SAMPLE = (DEFAULT_DATA_ROOT / "meta" / "info.json").is_file()
requires_sample = pytest.mark.skipif(not HAS_SAMPLE, reason="fixed public sample not downloaded")


@pytest.fixture(autouse=True)
def isolate_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBODATA_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("ROBODATA_DATA_ROOT", str(DEFAULT_DATA_ROOT))


def create_app():
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=120)
    app.session_state["page"] = "数据概览"
    return app.run()


def navigate(app, route):
    """Use the same visible navigation controls as the user, without state injection."""
    parent = ui_navigation.main_for(route)
    if parent:
        if app.radio(key="main_navigation").value != parent:
            app.radio(key="main_navigation").set_value(parent).run()
            assert not app.exception
        if app.session_state["page"] != route:
            app.radio(key=f"section_view_{parent}").set_value(route).run()
    else:
        app.button(key=f"technical_{route}").click().run()
    assert not app.exception
    assert app.session_state["page"] == route
    if parent:
        assert app.radio(key="main_navigation").value == parent
    return app


def click(app, label):
    next(button for button in app.button if button.label == label).click().run()
    assert not app.exception
    return app


def finish_run(app):
    run_id = app.session_state["selected_run_id"]
    deadline = time.monotonic() + 120
    state = ui_runs.load_run(run_id)
    while state["status"] in ui_runs.ACTIVE_STATUSES and time.monotonic() < deadline:
        time.sleep(0.1)
        state = ui_runs.load_run(run_id)
    assert state["status"] not in ui_runs.ACTIVE_STATUSES, state
    app.run()
    assert not app.exception
    return state


def test_missing_samples_do_not_block_history(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBODATA_DATA_ROOT", str(tmp_path / "not-downloaded"))
    app = create_app()
    assert not app.exception
    assert app.title[0].value == "准备公开样本"
    assert any("先获取" in item.value for item in app.info)
    assert any("robodata fetch-sample" in code.value for code in app.code)
    navigate(app, "运行记录")
    assert not app.exception
    assert app.title[0].value == "运行记录"
    assert any("尚无运行记录" in item.value for item in app.info)


@requires_sample
def test_browse_real_data_each_click_starts_new_run_and_history_survives_refresh():
    app = create_app()
    assert not app.exception
    assert app.metric[0].value == "5 / 5"
    assert app.metric[1].value == "3,562"
    navigate(app, "任务浏览")
    assert not app.exception
    click(app, "中间帧")
    assert app.slider(key="selected_row").value > 0
    click(app, "末帧")
    assert app.slider(key="selected_row").value == app.slider(key="selected_row").max
    app.selectbox(key="selected_episode").set_value(4).run()
    assert app.slider(key="selected_row").value == 0
    click(app, "运行基础质检")
    assert app.title[0].value == "运行记录"
    first = finish_run(app)
    assert first["status"] == "completed"
    first_id = first["run_id"]
    assert app.query_params["run_id"] == [first_id]
    click(app, "查看本次质检报告")
    assert app.title[0].value == "质检报告"
    assert app.metric[0].value == "0"
    assert len(app.get("download_button")) == 2
    assert any("未重新执行" in item.value for item in app.caption)
    app.number_input(key="interval_tolerance").set_value(15.0).run()
    assert len(app.get("download_button")) == 2
    click(app, "运行基础质检")
    second = finish_run(app)
    assert second["run_id"] != first_id
    assert second["request"]["params"]["interval_relative_tolerance"] == 0.15
    assert len(ui_runs.available_runs()) == 2
    app.selectbox(key="history_picker").set_value(first_id).run()
    assert app.number_input(key="interval_tolerance").value == 10.0
    refreshed = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=120)
    refreshed.query_params["run_id"] = first_id
    refreshed.run()
    assert not refreshed.exception
    assert refreshed.title[0].value == "运行记录"
    assert refreshed.selectbox(key="history_picker").value == first_id
    assert len(ui_runs.available_runs()) == 2


@requires_sample
@pytest.mark.parametrize("mode", ["异常演示副本", "视频缺失演示副本"])
def test_demo_run_report_diagnostics_and_row_jump(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    app = create_app()
    app.selectbox(key="data_mode").set_value(mode).run()
    assert app.title[0].value == "准备演示副本"
    click(app, "准备演示副本")
    assert any("故障注入测试副本" in item.value for item in app.warning)
    click(app, "运行基础质检")
    state = finish_run(app)
    report = ui_runs.load_report(state)
    assert report["source"]["source_kind"] == "injected_test"
    assert report["summary"]["passed_episode_count"] == 4
    click(app, "查看本次质检报告")
    assert not app.exception
    if mode == "异常演示副本":
        assert report["summary"]["data_issue_count"] == 7
        click(app, "跳转定位")
        assert app.selectbox(key="selected_episode").value == 0
        assert app.slider(key="selected_row").value == 50
    else:
        assert report["summary"]["load_failure_count"] == 1
        assert any(item.label == "原始错误详情（排查用）" for item in app.expander)
        assert not any(button.label == "跳转定位" for button in app.button)
        navigate(app, "任务浏览")
        assert any("所需文件不存在" in item.value for item in app.error)
        app.selectbox(key="selected_episode").set_value(1).run()
        assert not app.error
    app.selectbox(key="data_mode").set_value("原始公开样本").run()
    navigate(app, "质检报告")
    assert any("尚未生成报告" in item.value for item in app.info)
    assert len(ui_runs.available_runs()) == 1


@requires_sample
def test_timeout_is_execution_failure_and_history_reads_with_missing_metadata(tmp_path, monkeypatch):
    source = tmp_path / "input_copy"
    shutil.copytree(DEFAULT_DATA_ROOT, source)
    monkeypatch.setenv("ROBODATA_DATA_ROOT", str(source))
    app = create_app()
    app.selectbox(key="execution_scenario").set_value("运行超时演示").run()
    click(app, "运行基础质检")
    state = finish_run(app)
    assert state["status"] == "completed_with_errors"
    assert any(node["status"] == "timed_out" for node in state["nodes"])
    metrics = {item.label: item.value for item in app.metric}
    assert metrics["失败或超时节点"] == "1"
    assert metrics["节点报告的数据异常"] == "0"
    assert any("未改动源数据" in item.value for item in app.warning)
    run_id = state["run_id"]
    events = ui_runs.load_events(run_id)
    app.selectbox(key=f"log_episode_{run_id}").set_value(0).run()
    app.selectbox(key=f"log_node_{run_id}").set_value("video_decode").run()
    app.selectbox(key=f"log_level_{run_id}").set_value("ERROR").run()
    assert not app.exception
    filtered = ui_runs.filter_events(events, 0, "video_decode", "ERROR")
    assert filtered
    assert all(json.loads(line)["episode_index"] == 0 for line in ui_runs.events_jsonl(filtered).splitlines())
    (source / "meta/info.json").unlink()
    app.run()
    assert not app.exception
    assert app.title[0].value == "运行记录"
    click(app, "查看本次质检报告")
    assert not app.exception
    assert len(app.get("download_button")) == 2
    assert any("当前输入已变化或无法读取" in item.value for item in app.warning)
    assert ui_runs.load_report(state)["summary"]["data_issue_count"] == 0


def test_conversion_page_explains_missing_inputs_and_unregistered_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_conversion, "input_path", lambda: tmp_path / "absent.hdf5")
    monkeypatch.setattr(ui_conversion, "environment_python", lambda: tmp_path / "absent-python.exe")
    monkeypatch.setattr(ui_conversion, "registered_exports", lambda: [])
    monkeypatch.setattr(ui_conversion, "latest_export", lambda: None)
    app = create_app()
    navigate(app, "公开样本转换")
    assert not app.exception
    assert app.title[0].value == "公开样本转换"
    assert any("统计不一致" in item.value for item in app.warning)
    assert any("frame_index / 20" in item.value for item in app.info)
    assert any("fetch-hdf5" in code.value for code in app.code)
    assert any("tools/conversion" in code.value for code in app.code)
    assert next(button for button in app.button if button.label == "开始格式转换").disabled
    app.selectbox(key="data_mode").set_value("已验证转换样本").run()
    navigate(app, "数据概览")
    assert not app.exception
    assert any("尚无可用的已验证输出" in item.value for item in app.info)
    assert next(button for button in app.button if button.label == "运行基础质检").disabled


@pytest.mark.skipif(not (ui_conversion.input_path().is_file() and ui_conversion.environment_python().is_file()), reason="pinned HDF5 and independent conversion environment required")
def test_real_conversion_registered_output_dynamic_dimensions_and_video_offset(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBODATA_EXPORT_ROOT", str(tmp_path / "exports"))
    app = create_app()
    navigate(app, "公开样本转换")
    click(app, "开始格式转换")
    state = finish_run(app)
    assert state["request"]["kind"] == "conversion"
    assert state["status"] == "completed", state
    assert any(item.value == "官方加载与批读取验证" for item in app.subheader)
    assert any(button.label == "下载官方验证结果 JSON" for button in app.get("download_button"))
    navigate(app, "公开样本转换")
    assert len(ui_conversion.registered_exports()) == 1
    click(app, "浏览这份转换样本")
    assert app.selectbox(key="data_mode").value == "已验证转换样本"
    assert app.metric[0].value == "3 / 3"
    assert app.metric[1].value == "174"
    assert any("v3.0" in item.value for item in app.caption)
    navigate(app, "任务浏览")
    app.selectbox(key="selected_episode").set_value(1).run()
    assert not app.exception
    assert not app.error
    tables = [item.value for item in app.dataframe]
    assert any("状态字段" in table.columns and len(table) == 9 for table in tables)
    assert any("动作字段" in table.columns and len(table) == 7 for table in tables)
    assert any("文件偏移" in item.value and "文件偏移 0.000000" not in item.value for item in app.caption)
    click(app, "中间帧")
    assert not app.error
    click(app, "末帧")
    assert not app.error
    click(app, "运行基础质检")
    quality_state = finish_run(app)
    assert quality_state["request"]["episode_indices"] == [0, 1, 2]
    assert quality_state["request"]["source_kind"] == "converted_public"
    assert ui_runs.load_report(quality_state)["summary"]["row_count"] == 174


@requires_sample
def test_advanced_timeouts_reach_the_run_request(monkeypatch):
    from robodata import runtime

    captured = {}

    def capture_start(request, runs_root=None):
        captured.update(request)
        return "capture-request-only"

    monkeypatch.setattr(runtime, "start_run", capture_start)
    monkeypatch.setattr(ui_runs, "available_runs", lambda: [])
    app = create_app()
    app.number_input(key="default_timeout").set_value(45.0).run()
    app.number_input(key="video_timeout").set_value(90.0).run()
    click(app, "运行基础质检")
    assert captured["timeouts"] == {"default": 45.0, "video_decode": 90.0}
    assert captured["episode_indices"] == [0, 1, 2, 3, 4]


def test_verify_only_history_matches_registered_path_and_navigation_stays_selected(monkeypatch):
    # Reuse verified public output read-only; only emulate the UI history identity.
    exports = ui_conversion.registered_exports()
    if not exports:
        pytest.skip("A registered real v3 output is required")
    export = exports[0]
    original_state_path = PROJECT_ROOT / "work/runs" / export["run_id"] / "state.json"
    if not original_state_path.is_file():
        pytest.skip("The registered source run is unavailable")
    state = json.loads(original_state_path.read_text(encoding="utf-8"))
    state["run_id"] = "verify-only-ui-identity"
    state["request"]["verify_only"] = True
    state["output_path"] = str(ui_conversion.export_path(export))
    state["request"]["export_path"] = state["output_path"]
    state["request"]["timeouts"] = {"default": 37, "video_decode": 83}
    monkeypatch.setattr(ui_runs, "load_run", lambda run_id: state)
    monkeypatch.setattr(ui_runs, "available_runs", lambda: [state])
    monkeypatch.setattr(ui_runs, "load_events", lambda run_id: [])
    monkeypatch.setattr(ui_runs, "process_logs", lambda value: [])
    monkeypatch.setattr(ui_conversion, "registered_exports", lambda: [export])
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=120)
    app.query_params["run_id"] = state["run_id"]
    app.run()
    assert not app.exception
    assert app.selectbox(key="data_mode").value == "已验证转换样本"
    assert Path(app.session_state["history_data_root"]) == ui_conversion.export_path(export)
    assert app.number_input(key="default_timeout").value == 37
    assert app.number_input(key="video_timeout").value == 83
    details = next(item for item in app.expander if item.label == "完整官方验证证据")
    assert not details.proto.expanded
    click(app, "查看本次质检报告")
    assert app.session_state["report_matches_input"] is True
    assert not any("当前输入已变化或无法读取" in item.value for item in app.warning)
    for page in ("数据概览", "公开样本转换", "任务浏览", "运行记录", "公开样本转换"):
        navigate(app, page)
        assert not app.exception
        assert app.session_state["page"] == page
        assert app.title[0].value == page
        app.run()
        assert app.title[0].value == page


@pytest.mark.parametrize("source_kind,scenario,expected", [
    ("injected_test", "anomalies", "异常演示副本"),
    ("injected_test", "missing_video", "视频缺失演示副本"),
    ("converted_public", None, "已验证转换样本"),
])
def test_cli_history_mode_uses_saved_source_after_input_disappears(tmp_path, monkeypatch, source_kind, scenario, expected):
    state = {"run_id": "cli-detected-source", "status": "completed", "nodes": [],
             "request": {"kind": "quality", "source_kind": "original_public",
                         "data_root": str(tmp_path / "input-no-longer-exists")},
             "report_path": None, "episode_completed": 0, "episode_total": 0}
    source = {"source_kind": source_kind}
    if scenario:
        source["demo_manifest"] = {"source_kind": "injected_test", "scenario": scenario}
    monkeypatch.setattr(ui_runs, "load_run", lambda run_id: state)
    monkeypatch.setattr(ui_runs, "available_runs", lambda: [state])
    monkeypatch.setattr(ui_runs, "load_report", lambda value: {"source": source})
    monkeypatch.setattr(ui_runs, "load_events", lambda run_id: [])
    monkeypatch.setattr(ui_runs, "process_logs", lambda value: [])
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=120)
    app.query_params["run_id"] = state["run_id"]
    app.run()
    assert not app.exception
    assert app.title[0].value == "运行记录"
    assert app.selectbox(key="data_mode").value == expected
