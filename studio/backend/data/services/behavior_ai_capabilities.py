"""Public, path-free capability payloads for manual behavior AI suggestions."""

from __future__ import annotations

from collections.abc import Iterable


def build_behavior_ai_capability(
    *,
    enabled: bool,
    eligible: bool,
    rgb_topics: Iterable[object] = (),
    disabled_reason: str = "",
) -> dict[str, object]:
    """Serialize the small UI gate without exposing preview artifacts or paths."""

    topics = _public_rgb_topics(rgb_topics)
    active = bool(enabled)
    can_request = active and bool(eligible) and bool(topics)
    reason = "" if can_request else _safe_reason(disabled_reason) or _default_reason(active, topics)
    return {
        "enabled": active,
        "eligible": can_request,
        "disabled_reason": reason,
        "rgb_topics": topics,
        "default_rgb_topic": _preferred_rgb_topic(topics),
    }


def _public_rgb_topics(values: Iterable[object]) -> list[str]:
    topics = {
        value.strip()
        for value in values
        if isinstance(value, str) and 0 < len(value.strip()) <= 512
    }
    return sorted(topics)


def _preferred_rgb_topic(topics: list[str]) -> str:
    for topic in topics:
        normalized = topic.casefold()
        if normalized in {"/camera/head/rgb", "/camera/front/rgb"}:
            return topic
    for topic in topics:
        normalized = topic.casefold()
        if "head" in normalized or "front" in normalized:
            return topic
    return topics[0] if topics else ""


def _safe_reason(value: object) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _default_reason(enabled: bool, topics: list[str]) -> str:
    if not enabled:
        return "ai_disabled"
    if not topics:
        return "ai_preview_unavailable"
    return "ai_target_not_eligible"
