"""Business page controls against real isolated state; reader/runner stubs explicit.

These tests validate UI transitions and guards, not official data conversion.
Real conversion and delivery evidence is recorded by the end-to-end acceptance.
"""

from pathlib import Path
from types import SimpleNamespace
import hashlib
import json

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from robodata import importing, ui_business, ui_runs, ui_flow, ui_navigation
from robodata.config import PROJECT_ROOT
from robodata_business import BatchStore
from test_app import navigate


@pytest.fixture
def business_ui(tmp_path, monkeypatch):
    store = BatchStore(tmp_path / "business")
    calls = []

    def record(kind, *args, **kwargs):
        calls.append((kind, args, kwargs))
        return "test-business-run"

    api = SimpleNamespace(get_store=lambda: store, safe_list_batches=store.safe_list_batches,
        start_import=lambda *a, **kw: record("import", *a, **kw),
        start_batch_quality=lambda *a, **kw: record("quality", *a, **kw),
        start_final_quality=lambda *a, **kw: record("final_quality", *a, **kw),
        start_batch_conversion=lambda *a, **kw: record("conversion", *a, **kw),
        start_delivery=lambda *a, **kw: record("delivery", *a, **kw),
        start_verify_delivery=lambda *a, **kw: record("verify_delivery", *a, **kw),
        reconcile_run=lambda *a, **kw: {"status": "registered", "message": "测试回写已核对"})
    monkeypatch.setattr(ui_business, "service", lambda: api)
    monkeypatch.setenv("ROBODATA_BUSINESS_ROOT", str(store.root))
    monkeypatch.setenv("ROBODATA_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr(ui_runs, "available_runs", lambda: [])
    monkeypatch.setattr(importing, "preview_batch", lambda batch, episode_index, row: {
        "image": np.zeros((8, 8, 3), dtype=np.uint8),
        "table": pd.DataFrame({"timestamp": np.arange(5) / 20,
            "observation.state": [[1.0, 2.0]] * 5, "action": [[0.0]] * 5}),
        "state_names": ["位置 0", "位置 1"], "action_names": ["动作 0"],
        "issues": [], "source": {"timestamp_semantics": "测试预览，仅验证界面"}})
    return store, calls


def app_for(batch_id=None, page="flow_overview"):
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=30)
    app.session_state["page"] = page
    if batch_id:
        app.query_params["batch_id"] = batch_id
    app.run()
    assert not app.exception
    return app


def click(app, label):
    next(button for button in app.button if button.label == label).click().run()
    assert not app.exception
    return app


def field(app, group, label):
    return next(item for item in getattr(app, group) if item.label == label)


def make_batch(store, *, bad=False, source=None):
    source = source or {"kind": "hdf5", "input_fingerprint": "ui-fixture", "test_label": "合成界面测试"}
    state = store.create_batch("界面测试批次（非真实验收）",
        source,
        [{"episode_index": 0, "row_count": 5}, {"episode_index": 2, "row_count": 5}])
    state = store.apply_quality_report(state["batch_id"], {"input_fingerprint": source["input_fingerprint"], "episodes": [
        {"episode_index": index, "row_count": 5, "status": "issues" if bad and index == 2 else "passed",
         "coverage": [{"rule": "ui_fixture_rule", "status": "performed"}],
         "issues": [{"rule": "non_finite", "row_index": 3, "field": "action", "file": "fixture.hdf5",
                     "message": "动作中包含 NaN", "evidence": {"value": "NaN"}}] if bad and index == 2 else []}
        for index in (0, 2)]}, run_id="ui-fixture-quality", expected_revision=state["revision"])
    return state


def test_home_import_controls_and_damaged_batch_isolation(business_ui):
    store, calls = business_ui
    state = make_batch(store)
    (store.root / ("batch_" + "a" * 32 + ".json")).write_text("{broken", encoding="utf-8")
    app = app_for()
    assert app.title[0].value == "批次总览"
    assert app.radio(key="main_navigation").options == [ui_navigation.main_label(p) for p in ui_navigation.MAIN_OPTIONS]
    assert any("其余批次仍可使用" in item.value for item in app.warning)
    navigate(app, "import")
    app.selectbox(key="import_method").set_value("上传 HDF5 文件").run()
    assert len(app.get("file_uploader")) == 1
    click(app, "开始导入")
    assert any("先选择" in item.value for item in app.error)
    app.selectbox(key="import_method").set_value("本机 HDF5 路径").run()
    app.text_input(key="import_path").set_value("C:/local/example.hdf5")
    app.text_input(key="import_label").set_value("本地导入测试")
    click(app, "开始导入")
    assert calls == [("import", ("C:/local/example.hdf5", "hdf5", "本地导入测试"), {"test_label": None})]
    assert app.session_state["selected_run_id"] == "test-business-run"
    assert app.title[0].value == "运行记录"
    assert store.get_batch(state["batch_id"])["total_rows"] == 10


def test_quality_problem_jump_and_failed_keep_is_rejected(business_ui):
    store, calls = business_ui
    state = make_batch(store, bad=True)
    app = app_for(state["batch_id"], "select")
    app.selectbox(key="business_episode").set_value(2).run()
    click(app, "定位到问题记录")
    assert app.slider(key=f"batch_frame_{state['batch_id']}_2").value == 3
    field(app, "selectbox", "筛选决定").set_value("keep")
    click(app, "保存筛选决定")
    assert any("未通过完整质检" in item.value for item in app.error)
    assert store.get_batch(state["batch_id"])["episodes"][1]["disposition"] == "quarantine"
    field(app, "selectbox", "筛选决定").set_value("exclude")
    field(app, "text_area", "处理原因（隔离或排除必填）").set_value("异常任务不进入当前交付")
    click(app, "保存筛选决定")
    click(app, "确认筛选候选集")
    current = store.get_batch(state["batch_id"])
    assert current["cleaning_confirmed"]
    assert current["counts"]["keep"]["rows"] == 5
    assert current["counts"]["exclude"]["rows"] == 5
    navigate(app, "clean")
    click(app, "重新执行基础清洗检查")
    assert calls[-1] == ("quality", (state["batch_id"], current["revision"]), {})


def test_review_return_revise_resubmit_approve_and_whole_batch_gate(business_ui):
    store, calls = business_ui
    state = make_batch(store)
    state = store.confirm_cleaning(state["batch_id"], expected_revision=state["revision"])
    app = app_for(state["batch_id"], "annotate")
    field(app, "text_area", "任务描述").set_value("抬起方块")
    field(app, "selectbox", "机器人任务执行结果").set_value("failure")
    click(app, "保存并提交审核")
    navigate(app, "review")
    assert next(button for button in app.button if button.label == "退回修改").disabled
    assert any("退回前请填写" in item.value for item in app.caption)
    field(app, "text_input", "审核意见／退回原因（退回必填）").set_value("补充失败判断依据").run()
    click(app, "退回修改")
    assert store.get_batch(state["batch_id"])["episodes"][0]["review"]["status"] == "returned"
    navigate(app, "annotate")
    field(app, "text_area", "标注备注").set_value("方块未达到目标高度，记录仍完整有效")
    click(app, "保存并提交审核")
    navigate(app, "review")
    click(app, "审核通过")
    current = store.get_batch(state["batch_id"])
    assert current["episodes"][0]["annotation"]["outcome"] == "failure"
    assert current["episodes"][0]["review"]["status"] == "approved"
    navigate(app, "convert")
    assert next(button for button in app.button if button.label == "转换并验证候选集").disabled
    assert any("还有 1 条保留任务未完成审核" in item.value for item in app.warning)
    navigate(app, "annotate")
    app.selectbox(key="business_episode").set_value(2).run()
    field(app, "text_area", "任务描述").set_value("抬起方块")
    field(app, "selectbox", "机器人任务执行结果").set_value("uncertain")
    click(app, "保存并提交审核")
    navigate(app, "review")
    click(app, "审核通过")
    current = store.get_batch(state["batch_id"])
    assert calls[-1] == ("final_quality", (state["batch_id"], current["revision"]), {"auto_continue": True})
    reopened = app_for(state["batch_id"], "convert")
    assert next(button for button in reopened.button if button.label == "转换并验证候选集").disabled
    table = next(item.value for item in reopened.dataframe if "输出任务" in item.value.columns)
    assert table["源任务"].tolist() == [0, 2]
    assert table["输出任务"].tolist() == [0, 1]


def test_old_page_rejects_conflict_without_overwriting_new_annotation(business_ui):
    store, _ = business_ui
    state = make_batch(store)
    state = store.confirm_cleaning(state["batch_id"], expected_revision=state["revision"])
    app = app_for(state["batch_id"], "annotate")
    field(app, "text_area", "任务描述").set_value("旧页面准备覆盖的文字")
    field(app, "selectbox", "机器人任务执行结果").set_value("success")
    latest = store.save_annotation(state["batch_id"], 0, task="另一页面的新标注", outcome="uncertain", expected_revision=state["revision"])
    click(app, "保存草稿")
    assert any("页面内容已过期" in item.value for item in app.error)
    saved = store.get_batch(state["batch_id"])
    assert saved["revision"] == latest["revision"]
    assert saved["episodes"][0]["annotation"]["task"] == "另一页面的新标注"
    assert field(app, "text_area", "任务描述").value == "另一页面的新标注"


def delivery_flow_fixture(store, tmp_path):
    """Real file hashes and authoritative state, explicitly synthetic payloads."""
    from robodata.business import _candidate_snapshot
    from robodata.dataset import dataset_fingerprint
    raw = store.root / "managed/ui/source/test.hdf5"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"synthetic UI source, not a robot HDF5")
    manifest = {"files": [{"path": "test.hdf5", "exists": True, "size": raw.stat().st_size,
                           "sha256": hashlib.sha256(raw.read_bytes()).hexdigest()}], "interpretation": {"synthetic_ui": True}}
    manifest_path = raw.parent.parent / "input_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    state = make_batch(store, source={"kind": "hdf5", "input_fingerprint": importing._digest(manifest),
        "test_label": "合成界面测试", "managed_path": str(raw.parent.parent), "manifest_path": str(manifest_path)})
    batch_id = state["batch_id"]
    state = store.confirm_cleaning(batch_id, expected_revision=state["revision"])
    for index in (0, 2):
        state = store.save_annotation(batch_id, index, task="UI 交付控件测试", outcome="uncertain", expected_revision=state["revision"])
        state = store.submit_annotation(batch_id, index, expected_revision=state["revision"])
        state = store.review_annotation(batch_id, index, approve=True, expected_revision=state["revision"])
    basic_dir = tmp_path / "runs/ui-fixture-quality"
    basic_dir.mkdir(parents=True)
    basic = basic_dir / "quality_report.json"
    basic.write_text('{"synthetic_ui":true}', encoding="utf-8")
    (basic_dir / "registration.json").write_text(json.dumps({"evidence_sha256": {"quality_report.json": hashlib.sha256(basic.read_bytes()).hexdigest()}}), encoding="utf-8")
    (basic_dir / "state.json").write_text(json.dumps({"run_id": basic_dir.name, "status": "completed",
        "request": {"operation": "quality", "batch_id": batch_id, "content_version": 1},
        "nodes": [{"node_id": "quality", "status": "completed"}], "report_path": str(basic)}), encoding="utf-8")
    final_dir = tmp_path / "runs/ui-final"
    final_dir.mkdir(parents=True)
    final = final_dir / "final_quality.json"
    final.write_text(json.dumps({"synthetic_ui": True, "basic_report_path": str(basic),
        "basic_report_sha256": hashlib.sha256(basic.read_bytes()).hexdigest()}), encoding="utf-8")
    final.with_suffix(".html").write_text("<p>Synthetic UI report</p>", encoding="utf-8")
    evidence_dir = store.root / "final_quality" / batch_id
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "ui-final.json").write_text(json.dumps({"run_id": "ui-final", "accepted": True,
        "content_version": state["content_version"], "snapshot_id": _candidate_snapshot(state)["snapshot_id"],
        "report_path": str(final), "report_sha256": hashlib.sha256(final.read_bytes()).hexdigest(),
        "report_html_sha256": hashlib.sha256(final.with_suffix(".html").read_bytes()).hexdigest()}), encoding="utf-8")
    snapshot = store.conversion_spec(batch_id)
    output = store.root / "outputs/ui-fixture"
    (output / "meta").mkdir(parents=True)
    (output / "meta/info.json").write_text('{"synthetic_ui":true}', encoding="utf-8")
    state = store.register_verified_output(batch_id, snapshot, run_id="ui-output-only",
        artifact_path=str(output), validation={"official_loader_passed": True,
        "numeric_consistency_passed": True, "episode_count": 2, "row_count": 10,
        "output_fingerprint": dataset_fingerprint(output), "snapshot_id": snapshot["snapshot_id"]}, expected_revision=state["revision"])
    path = tmp_path / "ui-download-fixture.zip"
    path.write_bytes(b"explicit-ui-download-fixture")
    state = store.register_delivery(batch_id, state["outputs"][0]["output_id"], snapshot_id=snapshot["snapshot_id"],
        zip_path=str(path), zip_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), packaging_run_id="ui-package-only",
        archive_verified=True, expected_revision=state["revision"])
    return state, {"zip": path, "source": raw, "basic": basic, "final_html": final.with_suffix(".html")}


def test_current_delivery_download_hash_and_stale_guard(business_ui, tmp_path):
    store, _ = business_ui
    state, files = delivery_flow_fixture(store, tmp_path)
    batch_id = state["batch_id"]
    path = files["zip"]
    app = app_for(batch_id, "deliver")
    assert any(item.label == "下载已验证交付包 ZIP" for item in app.get("download_button"))
    path.write_bytes(b"changed-after-registration")
    app.run()
    assert not app.exception
    assert not any(item.label == "下载已验证交付包 ZIP" for item in app.get("download_button"))
    assert any("已失效" in item.value for item in app.caption)
    store.save_annotation(batch_id, 0, task="产生新内容版本", outcome="uncertain", expected_revision=state["revision"])
    app.run()
    assert not app.exception
    assert not any(item.label == "下载已验证交付包 ZIP" for item in app.get("download_button"))
    assert any("历史包不作为当前版本" in item.value for item in app.caption)


@pytest.mark.parametrize("target,delete", [("source", False), ("basic", False), ("basic", True), ("final_html", False), ("final_html", True)])
def test_upstream_evidence_loss_blocks_current_data_and_zip_downloads(business_ui, tmp_path, monkeypatch, target, delete):
    store, _ = business_ui
    state, files = delivery_flow_fixture(store, tmp_path)
    monkeypatch.setattr(ui_business, "render_output_preview", lambda output: None)
    app = app_for(state["batch_id"], "deliver")
    assert any(item.label == "下载已验证交付包 ZIP" for item in app.get("download_button"))
    navigate(app, "convert")
    assert any(item.label == "下载训练数据文件清单 JSON" for item in app.get("download_button"))
    if delete:
        files[target].unlink()
    else:
        files[target].write_bytes(b"changed upstream evidence")
    app.run()
    assert not app.exception
    assert not any(item.label in {"下载训练数据文件清单 JSON", "下载格式转换证据 JSON"} for item in app.get("download_button"))
    assert next(button for button in app.button if button.label == "转换并验证候选集").disabled
    navigate(app, "deliver")
    assert not app.exception
    assert not any(item.label == "下载已验证交付包 ZIP" for item in app.get("download_button"))
    assert next(button for button in app.button if button.label == "生成并验证交付包").disabled
    assert store.get_batch(state["batch_id"])["deliveries"][0]["current"]  # A version-only guard would incorrectly allow this.


def test_business_run_pending_registration_recovery_and_return_link(business_ui, monkeypatch):
    store, _ = business_ui
    batch = make_batch(store)
    state = {"run_id": "business-pending-ui", "status": "completed", "nodes": [], "episode_total": 2,
             "request": {"kind": "business", "operation": "quality", "batch_id": batch["batch_id"], "content_version": 2},
             "business_registration": {"status": "pending", "message": "产物已保存，待回写"}, "batch_id": batch["batch_id"], "artifacts": {}}
    monkeypatch.setattr(ui_runs, "available_runs", lambda: [state])
    monkeypatch.setattr(ui_runs, "load_run", lambda run_id: state)
    monkeypatch.setattr(ui_runs, "load_report", lambda value: None)
    monkeypatch.setattr(ui_runs, "load_events", lambda run_id: [])
    monkeypatch.setattr(ui_runs, "process_logs", lambda value: [])
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=30)
    app.query_params["run_id"] = state["run_id"]
    app.run()
    assert not app.exception
    assert app.title[0].value == "运行记录"
    assert any("待回写" in item.value for item in app.warning)
    click(app, "核对并恢复业务登记")
    assert any("测试回写已核对" in item.value for item in app.success)
    click(app, "返回关联批次")
    assert app.title[0].value == "批次总览"
    assert app.session_state["selected_batch_id"] == batch["batch_id"]


def test_all_eight_nodes_remain_visible_without_inputs(business_ui):
    app = app_for()
    for node_id, label in ui_business.NODE_LABELS.items():
        navigate(app, node_id)
        assert app.title[0].value == ui_navigation.ROUTE_LABELS[node_id]
        assert app.radio(key="main_navigation").options == [ui_navigation.main_label(p) for p in ui_navigation.MAIN_OPTIONS]
        assert any("尚无可用批次" in item.value for item in app.info)
    navigate(app, "import")
    assert len(app.selectbox(key="import_method").options) == 3


def test_nodes_have_distinct_controls_and_current_record_exports(business_ui):
    store, _ = business_ui
    batch = make_batch(store)
    expected = {"clean": ("重新执行基础清洗检查", "保存筛选决定"),
                "select": ("保存筛选决定", "重新执行基础清洗检查"),
                "annotate": ("保存草稿", "审核通过"),
                "review": ("审核通过", "保存草稿"),
                "quality": ("执行最终质检并自动继续", "转换并验证候选集"),
                "convert": ("转换并验证候选集", "执行最终质检并自动继续")}
    app = app_for(batch["batch_id"])
    for node_id, (present, absent) in expected.items():
        navigate(app, node_id)
        assert not app.exception
        assert app.title[0].value == ui_navigation.ROUTE_LABELS[node_id]
        labels = [button.label for button in app.button]
        assert present in labels and absent not in labels
        assert any(item.label == "节点输入、进度与输出" and item.proto.expanded for item in app.expander)
        assert not any("流程证据暂时无法读取" in item.value for item in app.error)
    assert next(b for b in app.button if b.label == "转换并验证候选集").disabled
    for node_id, label in (("select", "导出筛选清单 JSON"), ("annotate", "导出标注文件 JSON"),
                           ("review", "导出审核记录 JSON")):
        navigate(app, node_id)
        assert any(button.label == label for button in app.get("download_button"))
    navigate(app, "quality")
    assert next(b for b in app.button if b.label == "执行最终质检并自动继续").disabled


def test_legacy_sample_converter_is_separate_from_batch_converter(business_ui):
    app = app_for(page="格式转换")
    assert app.title[0].value == "公开样本转换"
    assert app.session_state["page"] == "公开样本转换"
    navigate(app, "convert")
    assert app.title[0].value == "格式转换"
    assert "data_mode" not in [item.key for item in app.selectbox]


def test_automatic_final_run_exposes_conversion_and_archive_evidence_separately(business_ui, tmp_path, monkeypatch):
    """Recorded UI fixtures prevent final-quality runs from hiding real evidence.

    This verifies presentation and scope labels, not official compatibility.
    """
    store, _ = business_ui
    batch = make_batch(store)
    converted = tmp_path / "official.json"
    archived = tmp_path / "archive.json"
    converted.write_text(json.dumps({"status": "passed", "details": {
        "checked_numeric_frames": 116, "batch_size": 2,
        "validation_scope": "源 float32 数值比对与官方离线读取"}}, ensure_ascii=False), encoding="utf-8")
    archived.write_text(json.dumps({"status": "passed", "details": {
        "checked_numeric_frames": 0, "output_numeric_frames_checked": 116,
        "batch_size": 2, "validation_scope": "交付包官方离线读取，未重新读取源文件"}}, ensure_ascii=False), encoding="utf-8")
    state = {"run_id": "automatic-final-evidence-ui", "status": "completed", "nodes": [],
             "request": {"kind": "business", "operation": "final_quality", "batch_id": batch["batch_id"]},
             "batch_id": batch["batch_id"],
             "artifacts": {"official_verify": str(converted), "archive_verify": str(archived)}}
    monkeypatch.setattr(ui_runs, "available_runs", lambda: [state])
    monkeypatch.setattr(ui_runs, "load_run", lambda run_id: state)
    monkeypatch.setattr(ui_runs, "load_events", lambda run_id: [])
    monkeypatch.setattr(ui_runs, "process_logs", lambda value: [])
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=30)
    app.query_params["run_id"] = state["run_id"]
    app.run()
    assert not app.exception
    metrics = {item.label: item.value for item in app.metric}
    assert metrics["数值比对帧数"] == "116"
    assert metrics["输出数值检查帧数"] == "116"
    assert any("未重新读取源文件" in item.value for item in app.caption)
    downloads = [item.label for item in app.get("download_button")]
    assert "下载官方验证结果 JSON" in downloads
    assert "下载交付包官方验证结果 JSON" in downloads
    # A completed check-only attempt has no conversion; never borrow old files.
    state["artifacts"] = {}
    state["request"]["auto_continue"] = False
    app.run()
    assert not app.exception
    assert not any("数值比对帧数" == item.label for item in app.metric)
    assert not any("官方验证结果" in item.label for item in app.get("download_button"))


@pytest.mark.parametrize("raw,expected", [
    ("C:/data/test.hdf5", "C:/data/test.hdf5"),
    ('"C:/data/test.hdf5"', "C:/data/test.hdf5"),
    ("'C:/data/test.hdf5'", "C:/data/test.hdf5"),
    ("   C:/data/test.hdf5   ", "C:/data/test.hdf5"),
    # 资源管理器「复制文件地址」粘贴到已有工作目录后面时出现的形态
    ('C:/work/"C:/data/test.hdf5"', "C:/data/test.hdf5"),
    ("", ""),
    ("   ", ""),
])
def test_clean_input_path_accepts_quoted_and_prefixed_paste(raw, expected):
    assert ui_business.clean_input_path(raw) == expected


def test_clean_input_path_keeps_plain_windows_path():
    path = r"C:\Users\demo\data\robomimic\test.hdf5"
    assert ui_business.clean_input_path(path) == path
