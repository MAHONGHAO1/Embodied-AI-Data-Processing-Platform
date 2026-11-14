"""CLI exit codes are a contract for CI: 0 clean, 2 load failure, 3 data issue, 4 incomplete."""
from pathlib import Path


from robodata.cli import main

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "known-bad"


def _check(tmp_path, *extra) -> int:
    return main([
        "check", "--data-root", str(FIXTURES / "frame-gap"), "--skip-video",
        "--output", str(tmp_path / "reports"), "--runs-root", str(tmp_path / "runs"),
        *extra,
    ])


def test_data_issue_exits_3(tmp_path):
    assert _check(tmp_path) == 3


def test_load_failure_exits_2(tmp_path):
    code = main([
        "check", "--data-root", str(FIXTURES / "missing-camera"),
        "--output", str(tmp_path / "reports"), "--runs-root", str(tmp_path / "runs"),
    ])
    assert code == 2


def test_skipped_checks_pass_by_default_but_fail_when_asked(tmp_path):
    # missing-camera 的缺陷只在视频里；跳过视频就没有数据异常，只有未检查项。
    argv = ["check", "--data-root", str(FIXTURES / "missing-camera"), "--skip-video",
            "--output", str(tmp_path / "reports"), "--runs-root", str(tmp_path / "runs")]
    assert main(argv) == 0
    assert main(argv + ["--fail-on-incomplete"]) == 4


def test_junit_file_is_written_next_to_report(tmp_path):
    code = _check(tmp_path, "--format", "junit")
    assert code == 3
    written = list((tmp_path / "runs").rglob("quality_junit.xml"))
    assert written, "要求 junit 就要真的产出文件"
    assert "<testsuite" in written[0].read_text(encoding="utf-8")
