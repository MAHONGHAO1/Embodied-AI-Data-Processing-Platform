"""The delivery contract must be self-verifiable and match docs/DATA_CONTRACT.md."""
import json
from pathlib import Path


from robodata.cli import main
from robodata.contract import (VERDICT_DELIVERABLE, VERDICT_INCOMPLETE, VERDICT_REJECTED,
                               build_contract, verify_contract, verdict, write_contract)
from robodata.quality import run_batch

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "known-bad"

REQUIRED_FIELDS = {
    "schema_version", "app_version", "rule_version", "params", "check_video",
    "input_fingerprint", "result_digest", "source", "summary", "limitations",
    "issues", "contract_version", "verdict", "contract_digest",
}


def _report(name="frame-gap", **kwargs):
    return run_batch(FIXTURES / name, episode_indices=[0], **kwargs)


def test_contract_keeps_every_declared_field():
    contract = build_contract(_report())
    assert REQUIRED_FIELDS <= set(contract)
    assert set(contract["source"]) == {"source_kind", "manifest",
                                       "integrity_status", "integrity_note"}


def test_verdict_three_cases():
    assert verdict({"data_issue_count": 0, "load_failure_count": 0,
                    "not_checked_count": 0}) == VERDICT_DELIVERABLE
    assert verdict({"data_issue_count": 1, "load_failure_count": 0,
                    "not_checked_count": 0}) == VERDICT_REJECTED
    assert verdict({"data_issue_count": 0, "load_failure_count": 1,
                    "not_checked_count": 0}) == VERDICT_REJECTED
    assert verdict({"data_issue_count": 0, "load_failure_count": 0,
                    "not_checked_count": 3}) == VERDICT_INCOMPLETE


def test_data_issue_takes_priority_over_incomplete():
    report = _report("frame-gap", check_video=False)
    assert report["summary"]["not_checked_count"] > 0
    assert verdict(report["summary"]) == VERDICT_REJECTED


def test_contract_digest_is_recomputable_and_detects_tampering():
    contract = build_contract(_report())
    assert verify_contract(contract) is True
    tampered = dict(contract, data_issue_count=None)
    tampered["summary"] = dict(contract["summary"], data_issue_count=0)
    assert verify_contract(tampered) is False
    assert verify_contract({k: v for k, v in contract.items() if k != "contract_digest"}) is False


def test_digest_is_stable_across_key_order():
    contract = build_contract(_report())
    shuffled = {key: contract[key] for key in reversed(list(contract))}
    assert verify_contract(shuffled) is True


def test_result_digest_is_not_the_contract_digest():
    contract = build_contract(_report())
    assert contract["result_digest"] != contract["contract_digest"]


def test_write_contract_emits_two_files_with_verdict_on_first_line(tmp_path):
    report = _report()
    written = write_contract(report, tmp_path / "delivery")
    json_path, md_path = Path(written["json"]), Path(written["md"])
    assert json_path.is_file() and md_path.is_file()
    assert written["verdict"] == VERDICT_REJECTED

    on_disk = json.loads(json_path.read_text(encoding="utf-8"))
    assert verify_contract(on_disk) is True
    first_line = md_path.read_text(encoding="utf-8").splitlines()[0]
    assert first_line == f"# {VERDICT_REJECTED}"


def test_markdown_keeps_limitations_and_lists_unchecked_items(tmp_path):
    report = _report(check_video=False)
    written = write_contract(report, tmp_path / "delivery")
    text = Path(written["md"]).read_text(encoding="utf-8")
    for line in report["limitations"]:
        assert line in text
    assert "## 没有检查到的项" in text


def test_cli_emit_contract_writes_files(tmp_path):
    dest = tmp_path / "delivery"
    code = main(["check", "--data-root", str(FIXTURES / "frame-gap"), "--skip-video",
                 "--output", str(tmp_path / "reports"), "--runs-root", str(tmp_path / "runs"),
                 "--emit-contract", str(dest)])
    assert code == 3
    assert (dest / "QUALITY_REPORT.json").is_file()
    assert (dest / "QUALITY_REPORT.md").is_file()
    assert verify_contract(json.loads((dest / "QUALITY_REPORT.json").read_text(encoding="utf-8")))
