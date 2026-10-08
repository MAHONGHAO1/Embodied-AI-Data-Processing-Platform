"""Shared detectors for sensitive values that must never enter public facts."""

from __future__ import annotations

import re

CLOUD_ACCESS_KEY_VALUE = re.compile(
    r"(?<![A-Za-z0-9])(?:(?:AKIA|ASIA)[A-Z0-9]{16}|LTAI[A-Za-z0-9]{12,32})(?![A-Za-z0-9])"
)
ABSOLUTE_PATH_FRAGMENT = re.compile(
    r"(?<![A-Za-z0-9/\\])(?:/(?!/)[^\s,;)\]}\"']+|[A-Za-z]:[\\/][^\s,;)\]}\"']+|\\[^\s,;)\]}\"']+)"
)
SENSITIVE_URI_REFERENCE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:"
    r"[a-z][a-z0-9+.-]*://[^\s,;)\]}\"']+|"
    r"(?:urn|file|jdbc|mongodb|postgresql?|redis|oss|nas|s3|gs):[^\s,;)\]}\"']+"
    r")"
)
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def contains_cloud_access_key_value(value: str) -> bool:
    return CLOUD_ACCESS_KEY_VALUE.search(value) is not None


def is_absolute_path(value: str) -> bool:
    return value.startswith(("/", "\\")) or _WINDOWS_ABSOLUTE_PATH.match(value) is not None


def contains_absolute_path_fragment(value: str) -> bool:
    stripped = value.strip()
    return is_absolute_path(stripped) or ABSOLUTE_PATH_FRAGMENT.search(value) is not None


def contains_sensitive_uri_reference(value: str) -> bool:
    return SENSITIVE_URI_REFERENCE.search(value) is not None
