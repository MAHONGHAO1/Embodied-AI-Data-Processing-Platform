"""Structured, storage-safe DTOs for persisted QRDF quality facts."""

from __future__ import annotations

import re
from typing import Any

from data.services.preprocess_facts import sanitize_fact_text

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def _safe_text(value: object, *, limit: int = 1000) -> str | None:
    return sanitize_fact_text(value, limit=limit)


def _identifier(value: object) -> str | None:
    safe = _safe_text(value, limit=128)
    return safe if safe and _IDENTIFIER.fullmatch(safe) else None


def _messages(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for raw in value[:200]:
        safe = _safe_text(raw)
        if safe and safe not in result:
            result.append(safe)
    return result


def sanitize_quality_fact(value: object) -> dict[str, Any]:
    """Allowlist validator facts while excluding all storage locations."""
    source = value if isinstance(value, dict) else {}
    result: dict[str, Any] = {}
    level = _identifier(source.get("level"))
    if level:
        result["level"] = level
    if isinstance(source.get("ok"), bool):
        result["ok"] = source["ok"]
    score = source.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        result["score"] = score
    for key in ("episode_count", "error_count", "warning_count"):
        number = source.get(key)
        if isinstance(number, int) and not isinstance(number, bool) and number >= 0:
            result[key] = number
    for key in ("warnings", "errors"):
        messages = _messages(source.get(key))
        if messages:
            result[key] = messages

    issues: list[dict[str, str]] = []
    raw_issues = source.get("issues")
    for raw in raw_issues[:500] if isinstance(raw_issues, list) else []:
        if not isinstance(raw, dict):
            continue
        issue: dict[str, str] = {}
        severity = _identifier(raw.get("severity") or raw.get("level"))
        code = _identifier(raw.get("code"))
        message = _safe_text(raw.get("message"))
        if severity:
            issue["severity"] = severity
        if code:
            issue["code"] = code
        if message:
            issue["message"] = message
        if issue:
            issues.append(issue)
    if issues:
        result["issues"] = issues
    return result
