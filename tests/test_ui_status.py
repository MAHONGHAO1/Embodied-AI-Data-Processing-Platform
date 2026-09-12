"""Regression cases for the distinction between execution and data quality."""

import pandas as pd
import pytest

from robodata.ui_status import aggregate_status, status_badge, status_style, styled_status_table


def node(status="completed", outcome="passed"):
    return {"status": status, "outcome": outcome}


@pytest.mark.parametrize("status", ["ready", "queued", "waiting_human", "pending", "skipped", "cancelled"])
def test_waiting_or_inactive_steps_do_not_look_running(status):
    assert status_style(status)[0] == "muted"
    assert aggregate_status([node(), node(status, None)])[0] == "muted"


@pytest.mark.parametrize("status", ["failed", "timed_out", "interrupted", "completed_with_errors"])
def test_execution_errors_stay_red_even_while_other_nodes_run(status):
    assert status_style(status)[0] == "error"
    assert aggregate_status([node("running", None), node(status, None)])[0] == "error"


def test_completed_check_can_reject_data_without_becoming_execution_failure():
    nodes = [node(), node("completed", "rejected"), node("waiting_upstream", None)]
    assert status_style(nodes[1]["status"])[0] == "success"
    assert status_style(nodes[1]["outcome"])[0] == "warning"
    assert aggregate_status(nodes)[0] == "warning"
    assert aggregate_status([node("completed", "execution_error")])[0] == "error"


def test_running_with_data_findings_preserves_both_facts():
    tone, label = aggregate_status([node("completed", "rejected"), node("running", None)])
    assert tone == "running"
    assert "处理中" in label and "问题" in label


@pytest.mark.parametrize("nodes", [
    [], [node("superseded")], [node("completed", None)], [node("completed", "unknown")],
    [node("unexpected", "passed")], [node(None, None)], [node("passed", "passed")],
])
def test_absent_stale_or_unverified_states_never_become_success(nodes):
    assert aggregate_status(nodes)[0] == "muted"


def test_success_requires_every_supplied_node_completed_and_accepted():
    nodes = [node() for _ in range(8)]
    assert aggregate_status(nodes) == ("success", "流程已完成")
    nodes[-1] = node("ready", None)
    assert aggregate_status(nodes)[0] == "muted"


def test_old_failed_attempt_does_not_override_current_success():
    current = node()
    current["history"] = [{"status": "failed", "current": False}]
    assert aggregate_status([current])[0] == "success"


def test_unknown_badge_and_custom_label_are_html_escaped():
    hostile = '\"><img src=x onerror="alert(1)">&'
    assert status_style(hostile) == ("muted", hostile)
    unknown = status_badge(hostile)
    assert 'class="rd-status rd-status-muted"' in unknown
    assert "<img" not in unknown and "&lt;img" in unknown and "&quot;" in unknown
    override = status_badge("completed", "<script>alert('x')</script>")
    assert 'class="rd-status rd-status-success"' in override
    assert "<script>" not in override and "&lt;script&gt;" in override
    assert status_style(None) == ("muted", "未知状态")


def test_table_colors_chinese_states_without_rewriting_data_or_free_text():
    frame = pd.DataFrame({
        "执行状态": ["执行完成", "运行中", "执行超时", "等待启动", pd.NA],
        "数据结论": ["发现数据问题", "检查通过", "尚无结论", "未知新状态", "失败原因见日志"],
        "任务": [0, 1, 2, 3, 4],
    })
    original = frame.copy(deep=True)
    styled = styled_status_table(frame)
    rendered = styled.to_html()
    pd.testing.assert_frame_equal(frame, original)
    pd.testing.assert_frame_equal(styled.data, original)
    assert "执行完成" in rendered and "发现数据问题" in rendered
    # Inspect the actual generated cell styles, including nullable and free text.
    cell_styles = styled._compute().ctx
    assert dict(cell_styles[(0, 0)])["color"] == "#166534"
    assert dict(cell_styles[(0, 1)])["color"] == "#9a3412"
    assert dict(cell_styles[(1, 0)])["color"] == "#1d4ed8"
    assert dict(cell_styles[(2, 0)])["color"] == "#991b1b"
    assert dict(cell_styles[(3, 0)])["color"] == "#475569"
    assert not cell_styles[(4, 0)]
    assert not cell_styles[(3, 1)] and not cell_styles[(4, 1)]
    assert all(not cell_styles[(row, 2)] for row in range(5))
