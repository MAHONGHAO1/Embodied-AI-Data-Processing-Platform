"""Machine-readable renderings of a quality report for CI systems.

The JSON report is the contract (see docs/DATA_CONTRACT.md). These renderers
turn the same report into the two formats that CI front-ends already render:
JUnit XML for test panels and SARIF for code scanning views.

Both renderers are pure: they take the assembled report dict and return a
string. Nothing is read from disk and no checks are re-run.
"""

from __future__ import annotations

import json
from xml.etree.ElementTree import Element, SubElement, tostring

LEVEL_BY_CATEGORY = {
    "load_failure": "error",
    "data_issue": "error",
    "not_checked": "note",
}


def _episode_rows(report: dict) -> list[dict]:
    return [item for item in report.get("episodes", []) if isinstance(item, dict)]


def _issues_by_key(report: dict) -> dict[tuple, list[dict]]:
    grouped: dict[tuple, list[dict]] = {}
    for issue in report.get("issues", []):
        grouped.setdefault((issue.get("episode_index"), issue.get("rule")), []).append(issue)
    return grouped


def to_junit(report: dict) -> str:
    """One testcase per episode and rule, so a CI panel reads like a checklist."""
    episodes = _episode_rows(report)
    grouped = _issues_by_key(report)
    suite = Element("testsuite", {
        "name": "robodata-quality",
        "tests": "0", "failures": "0", "errors": "0", "skipped": "0",
        "time": str(report.get("runtime_seconds", 0) or 0),
    })
    tests = failures = errors = skipped = 0
    for episode in episodes:
        index = episode.get("episode_index")
        for entry in episode.get("coverage", []):
            rule = entry.get("rule")
            performed = entry.get("status") == "performed"
            hits = grouped.get((index, rule), [])
            blocking = [item for item in hits if item.get("category") != "not_checked"]
            not_checked = [item for item in hits if item.get("category") == "not_checked"]
            case = SubElement(suite, "testcase", {
                "classname": f"episode_{index}", "name": str(rule),
                "time": "0",
            })
            tests += 1
            if blocking:
                failures += 1
                first = blocking[0]
                failure = SubElement(case, "failure", {
                    "message": str(first.get("message", "")),
                    "type": str(first.get("category", "data_issue")),
                })
                failure.text = json.dumps(
                    [{key: item.get(key) for key in
                      ("rule", "severity", "category", "message", "row_index", "evidence")}
                     for item in blocking],
                    ensure_ascii=False, indent=2, default=str)
            elif not performed:
                skipped += 1
                SubElement(case, "skipped", {"message": not_checked[0].get("message", entry.get("detail", "")) if not_checked else str(entry.get("detail", "未执行"))})
            elif not_checked:
                skipped += 1
                SubElement(case, "skipped", {"message": not_checked[0].get("message", "")})
    suite.set("tests", str(tests))
    suite.set("failures", str(failures))
    suite.set("errors", str(errors))
    suite.set("skipped", str(skipped))
    body = tostring(suite, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body


SARIF_SCHEMA = ("https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/"
                "Schemata/sarif-schema-2.1.0.json")


def to_sarif(report: dict) -> str:
    rules_seen: dict[str, dict] = {}
    results = []
    for issue in report.get("issues", []):
        rule_id = str(issue.get("rule"))
        rules_seen.setdefault(rule_id, {"id": rule_id, "name": rule_id})
        level = LEVEL_BY_CATEGORY.get(str(issue.get("category")), "warning")
        if issue.get("severity") == "info" and level == "error":
            level = "note"
        location = {
            "physicalLocation": {
                "artifactLocation": {"uri": str(issue.get("node_id", "quality"))},
            },
        }
        if issue.get("row_index") is not None:
            location["physicalLocation"]["region"] = {"startLine": int(issue["row_index"]) + 1}
        results.append({
            "ruleId": rule_id,
            "level": level,
            "message": {"text": str(issue.get("message", ""))},
            "locations": [location],
            "properties": {key: issue.get(key) for key in
                           ("episode_index", "frame_index", "severity", "category", "evidence")},
        })
    payload = {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "RoboData",
                "version": str(report.get("app_version", "")),
                "informationUri": "https://github.com/MAHONGHAO1/Embodied-AI-Data-Processing-Platform",
                "rules": list(rules_seen.values()),
            }},
            "results": results,
            "properties": {
                "rule_version": report.get("rule_version"),
                "summary": report.get("summary"),
                "limitations": report.get("limitations"),
            },
        }],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)
