"""API response field formatting: datetime display and frame-rate ceiling."""

from __future__ import annotations

import math
from datetime import datetime, timezone


def format_api_datetime(value: datetime | str | None) -> str | None:
    """Return an ISO-8601 UTC timestamp so browsers can localize it safely."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
    elif isinstance(value, datetime):
        dt = value
    else:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_api_fps(fps: float | int | None, *, default: int = 30) -> int:
    """Round the calculated FPS up to the nearest integer for API display and annotation time conversion."""
    if fps is None:
        return default
    try:
        value = float(fps)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return math.ceil(value)
