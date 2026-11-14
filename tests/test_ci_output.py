"""Known-bad fixtures must keep firing their declared rules, and CI renderers must stay valid."""
import json
from pathlib import Path
from xml.etree.ElementTree import fromstring

import pytest

from robodata.ci_output import to_junit, to_sarif
from robodata.quality import run_batch

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "known-bad"

EXPECTED = {
    "missing-camera": {"video_decode"},
    "frame-gap": {"frame_index"},
    "nan-joint": {"numeric_finite"},
    "length-mismatch": {"row_count"},
    "timestamp-jump": {"timestamp_interval", "video_timestamp_mapping"},
    "video-length": {"video_frame_count"},
}


def _report(name: str) -> dict:
    return run_batch(FIXTURES / name, episode_indices=[0], check_video=True)


@pytest.mark.parametrize("name,rules", sorted(EXPECTED.items()))
def test_known_bad_fixture_hits_expected_rules(name, rules):
    assert (FIXTURES / name / "EXPECTED.md").is_file(), "每个故障样本都要写明应当命中什么"
    report = _report(name)
    fired = {item["rule"] for item in report["issues"] if item["category"] != "not_checked"}
    assert rules <= fired, f"{name} 期望命中 {rules}，实测命中 {fired}"
    assert report["summary"]["issue_count"] > 0


def test_known_bad_fixtures_are_all_registered():
    on_disk = {path.name for path in FIXTURES.iterdir() if path.is_dir()}
    assert on_disk == set(EXPECTED), "磁盘上的故障样本与测试登记不一致"


def test_junit_rendering_is_valid_and_counts_failures():
    report = _report("frame-gap")
    root = fromstring(to_junit(report))
    assert root.tag == "testsuite"
    cases = root.findall("testcase")
    assert cases, "至少要有一条用例"
    assert int(root.attrib["tests"]) == len(cases)
    assert int(root.attrib["failures"]) == 1
    failing = [case for case in cases if case.find("failure") is not None]
    assert len(failing) == 1
    assert failing[0].attrib["name"] == "frame_index"


def test_junit_marks_unperformed_rules_as_skipped():
    report = _report("missing-camera")
    root = fromstring(to_junit(report))
    skipped = root.findall("./testcase/skipped")
    assert skipped, "没执行到的判据要标成 skipped，不能算通过"


def test_sarif_rendering_is_valid():
    report = _report("timestamp-jump")
    payload = json.loads(to_sarif(report))
    assert payload["version"] == "2.1.0"
    run = payload["runs"][0]
    assert run["results"], "有问题就该有结果条目"
    rule_ids = {item["ruleId"] for item in run["results"]}
    assert EXPECTED["timestamp-jump"] <= rule_ids
    for item in run["results"]:
        assert item["level"] in {"error", "warning", "note"}
        assert item["message"]["text"]
    assert run["properties"]["limitations"] == report["limitations"]
