"""Canonical behavior-annotation result and quality values."""

from __future__ import annotations

from collections.abc import Mapping

ACTION_KINDS = frozenset({"standard", "custom"})


SUCCESS_ALIASES = {
    "success": 1,
    "successful": 1,
    "成功": 1,
    "true": 1,
    "1": 1,
    "failure": 0,
    "fail": 0,
    "failed": 0,
    "失败": 0,
    "false": 0,
    "0": 0,
    "unknown": -1,
    "无法判断": -1,
    "-1": -1,
}

QUALITY_ALIASES = {
    "good": 1,
    "quality_good": 1,
    "质量良好": 1,
    "true": 1,
    "1": 1,
    "bad": 0,
    "poor": 0,
    "quality_bad": 0,
    "质量较差": 0,
    "false": 0,
    "0": 0,
}


def parse_success(value: object) -> int:
    """Normalize a current or historical success value to the shared enum."""
    return _parse_value(
        value,
        aliases=SUCCESS_ALIASES,
        allowed={1, 0, -1},
        field_name="success",
    )


def parse_quality(value: object) -> int:
    """Normalize a current or historical quality value to the shared enum."""
    return _parse_value(
        value,
        aliases=QUALITY_ALIASES,
        allowed={1, 0},
        field_name="quality",
    )


def parse_behavior_action(value: object) -> dict[str, str]:
    """Normalize the legacy string shape and validate the revision action envelope."""
    if isinstance(value, str):
        return {"kind": "standard", "key": value.strip() or "other"}
    if not isinstance(value, Mapping):
        raise ValueError("action must be a standard or custom action object")
    kind = str(value.get("kind") or "").strip().lower()
    if kind == "standard":
        key = str(value.get("key") or "").strip().lower()
        if not key:
            raise ValueError("standard action key is required")
        return {"kind": kind, "key": key}
    if kind == "custom":
        return {"kind": kind, "text": str(value.get("text") or "")}
    raise ValueError("action kind must be standard or custom")


def _parse_value(
    value: object,
    *,
    aliases: dict[str, int],
    allowed: set[int],
    field_name: str,
) -> int:
    if value is None:
        return 1
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in allowed:
        return value
    if isinstance(value, float) and value.is_integer() and int(value) in allowed:
        return int(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if not normalized:
            return 1
        alias = aliases.get(normalized)
        if alias is not None:
            return alias
    allowed_values = ", ".join(str(item) for item in sorted(allowed))
    raise ValueError(f"{field_name} must be one of {allowed_values}")
