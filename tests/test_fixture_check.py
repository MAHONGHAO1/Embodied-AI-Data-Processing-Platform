"""verify-fixtures must fail loudly when a declared rule stops firing."""
import json
import shutil
from pathlib import Path


from robodata.cli import main
from robodata.fixture_check import expected_rules, render_table, run_suite

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "known-bad"


def test_real_suite_passes_completely():
    result = run_suite()
    assert result["count"] == 6
    assert result["passed"] == 6
    assert result["ok"] is True
    assert all(row["declared"] for row in result["rows"])


def test_expected_rules_are_read_from_markdown():
    rules = expected_rules(FIXTURES / "timestamp-jump" / "EXPECTED.md")
    assert rules == ["timestamp_interval", "video_timestamp_mapping"]


def test_missing_declaration_is_a_failure(tmp_path):
    broken = tmp_path / "fixtures" / "nan-joint"
    shutil.copytree(FIXTURES / "nan-joint", broken)
    (broken / "EXPECTED.md").write_text("# 没写应当命中什么\n", encoding="utf-8")
    result = run_suite(tmp_path / "fixtures")
    assert result["ok"] is False
    assert result["rows"][0]["declared"] == []
    assert "（未声明）" in render_table(result)


def test_wrong_declaration_is_detected(tmp_path):
    broken = tmp_path / "fixtures" / "frame-gap"
    shutil.copytree(FIXTURES / "frame-gap", broken)
    (broken / "EXPECTED.md").write_text(
        "# 帧索引跳号\n\n应当命中的判据：\n\n- `numeric_finite`\n\n实测：\n\n- `frame_index`\n",
        encoding="utf-8")
    result = run_suite(tmp_path / "fixtures")
    assert result["ok"] is False
    assert result["rows"][0]["missing"] == ["numeric_finite"]
    assert "未命中：numeric_finite" in render_table(result)


def test_cli_returns_zero_on_good_suite(capsys):
    assert main(["verify-fixtures"]) == 0
    assert "6/6 通过" in capsys.readouterr().out


def test_cli_returns_one_on_empty_root(tmp_path, capsys):
    # 空目录不能悄悄说「全部通过」
    assert main(["verify-fixtures", "--root", str(tmp_path)]) == 1
    assert "0/0" in capsys.readouterr().out


def test_cli_json_output_is_machine_readable(capsys):
    assert main(["verify-fixtures", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["count"] == 6
    assert len(payload["rows"]) == 6
