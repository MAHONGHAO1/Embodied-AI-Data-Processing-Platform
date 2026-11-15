"""Verify the known-bad fixture suite in one pass.

The expected rules are read from each fixture's EXPECTED.md rather than from a
hardcoded table here, so a fixture that fails to declare what it should hit is
itself a failure. This is what makes fixtures/known-bad/ something a stranger
can point their own checker at.
"""

from __future__ import annotations

import re
from pathlib import Path

from .quality import run_batch

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "known-bad"
EXPECTED_HEADING = "应当命中的判据："
OBSERVED_HEADING = "实测"
RULE_PATTERN = re.compile(r"`([a-z_][a-z0-9_]*)`")


def expected_rules(markdown_path: Path) -> list[str]:
    """Read the declared rules from EXPECTED.md, up to the observed section."""
    text = markdown_path.read_text(encoding="utf-8")
    if EXPECTED_HEADING not in text:
        return []
    section = text.split(EXPECTED_HEADING, 1)[1].split(OBSERVED_HEADING, 1)[0]
    rules: list[str] = []
    for match in RULE_PATTERN.finditer(section):
        if match.group(1) not in rules:
            rules.append(match.group(1))
    return rules


def run_suite(root: Path | None = None) -> dict:
    """Check every fixture and report declared vs observed rules."""
    base = Path(root) if root else DEFAULT_ROOT
    if not base.is_dir():
        raise ValueError(f"故障样本目录不存在：{base}")

    rows = []
    for path in sorted(p for p in base.iterdir() if p.is_dir()):
        declared = expected_rules(path / "EXPECTED.md") if (path / "EXPECTED.md").is_file() else []
        report = run_batch(path, episode_indices=[0], check_video=True)
        observed = sorted({item["rule"] for item in report["issues"]
                           if item["category"] != "not_checked"})
        missing = [rule for rule in declared if rule not in observed]
        rows.append({
            "fixture": path.name,
            "declared": declared,
            "observed": observed,
            "missing": missing,
            "status": report["episodes"][0]["status"],
            "ok": bool(declared) and not missing,
        })
    return {"root": str(base), "count": len(rows),
            "passed": sum(1 for row in rows if row["ok"]),
            "ok": bool(rows) and all(row["ok"] for row in rows),
            "rows": rows}


def render_table(result: dict) -> str:
    lines = [
        f"{'fixture':<18} {'期望命中':<48} {'实测命中':<48} 结果",
        "-" * 128,
    ]
    for row in result["rows"]:
        declared = ", ".join(row["declared"]) or "（未声明）"
        observed = ", ".join(row["observed"]) or "（无）"
        flag = "OK" if row["ok"] else "FAIL"
        lines.append(f"{row['fixture']:<18} {declared:<48} {observed:<48} {flag}")
        if row["missing"]:
            lines.append(f"{'':<18} 未命中：{', '.join(row['missing'])}")
    lines.append("")
    lines.append(f"{result['passed']}/{result['count']} 通过"
                 + ("" if result["ok"] else "  —— 有判据没命中，或样本没声明期望"))
    return "\n".join(lines)
