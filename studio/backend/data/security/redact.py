"""Sensitive field redaction (API responses / logs)."""

from __future__ import annotations

import copy
import logging
import re
import traceback
from typing import Any

# Redact if any leaf path name matches (recursive dict)
SECRET_FIELD_NAMES = frozenset(
    {
        "access_key_secret",
        "secret_key",
        "password",
        "password_hash",
        "token",
        "refresh_token",
        "dashscope_api_key",
        "qwen_vl_api_key",
        "authorization",
        "policy",
        "signature",
        "security_token",
        "private_key",
        "client_secret",
    }
)

REDACTED = "***REDACTED***"
FILTERED_LOG_MESSAGE = "[filtered] possible secret material omitted"
FILTERED_EXCEPTION_SUFFIX = "[filtered sensitive exception material omitted]"
FORMAT_ERROR_SUFFIX = "[logging arguments omitted due to formatting error]"

_SENSITIVE_LOG_ASSIGNMENT = re.compile(
    rf"(?i)(?<![a-z0-9_])(?:{'|'.join(re.escape(name) for name in sorted(SECRET_FIELD_NAMES))})"
    r"(?![a-z0-9_])['\"]?\s*[:=]"
)
_LOG_RECORD_BUILTIN_FIELDS = frozenset(logging.makeLogRecord({}).__dict__)

logger = logging.getLogger("quicdata.security")


def _is_secret_key(key: str) -> bool:
    k = key.lower()
    if k in SECRET_FIELD_NAMES:
        return True
    if k.endswith("_secret") or k.endswith("_password"):
        return True
    return False


def redact_secrets(data: Any, *, placeholder: str = REDACTED) -> Any:
    """Deep copy and redact sensitive fields; returns non-dict/list objects as is."""
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for key, value in data.items():
            if _is_secret_key(str(key)) and value not in (None, "", placeholder):
                # Preserve ciphertext envelope structure markers so frontend does not mistake them for empty configs
                if isinstance(value, dict) and value.get("__enc__"):
                    out[key] = {"__enc__": True, "redacted": True}
                else:
                    out[key] = placeholder
            else:
                out[key] = redact_secrets(value, placeholder=placeholder)
        return out
    if isinstance(data, list):
        return [redact_secrets(item, placeholder=placeholder) for item in data]
    return data


def safe_copy_config(data: dict[str, Any]) -> dict[str, Any]:
    """External configuration copy: deep copy + redaction."""
    return redact_secrets(copy.deepcopy(data))


class SecretFilter(logging.Filter):
    """Filter potential plaintext secret substrings from log records (coarse-grained)."""

    @staticmethod
    def _contains_secret_material(value: str) -> bool:
        return bool(_SENSITIVE_LOG_ASSIGNMENT.search(value) or "-----BEGIN" in value)

    @staticmethod
    def _redact_text_value(value: str) -> str:
        match = _SENSITIVE_LOG_ASSIGNMENT.search(value)
        if match:
            return f"{value[: match.end()]} {REDACTED}"
        if "-----BEGIN" in value:
            return FILTERED_LOG_MESSAGE
        return value

    @classmethod
    def _redact_log_value(cls, value: Any) -> Any:
        if isinstance(value, str):
            return cls._redact_text_value(value)
        if isinstance(value, dict):
            structured = redact_secrets(value)
            return {key: cls._redact_log_value(item) for key, item in structured.items()}
        if isinstance(value, list):
            return [cls._redact_log_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(cls._redact_log_value(item) for item in value)
        return value

    @classmethod
    def _redact_extra_fields(cls, record: logging.LogRecord) -> None:
        for key in set(record.__dict__) - _LOG_RECORD_BUILTIN_FIELDS:
            structured = redact_secrets({key: record.__dict__[key]})[key]
            record.__dict__[key] = cls._redact_log_value(structured)

    @staticmethod
    def _is_missing_key_diagnostic(value: str) -> bool:
        match = _SENSITIVE_LOG_ASSIGNMENT.search(value)
        return bool(match and value[match.end() :].strip().lower() == "not configured")

    @classmethod
    def _exception_contains_secret_material(cls, record: logging.LogRecord) -> bool:
        if record.exc_text and cls._contains_secret_material(record.exc_text):
            return True
        if not record.exc_info:
            return False
        try:
            rendered = "".join(traceback.format_exception(*record.exc_info))
        except Exception:
            return True
        return cls._contains_secret_material(rendered)

    def filter(self, record: logging.LogRecord) -> bool:
        self._redact_extra_fields(record)
        record.args = self._redact_log_value(record.args)
        formatting_failed = False
        try:
            msg = record.getMessage()
        except Exception:
            formatting_failed = True
            try:
                base_message = self._redact_text_value(str(record.msg))
            except Exception:
                base_message = FILTERED_LOG_MESSAGE
            record.msg = f"{base_message} {FORMAT_ERROR_SUFFIX}"
            record.args = ()
            msg = record.getMessage()

        message_contains_secret = not formatting_failed and self._contains_secret_material(msg)
        exception_contains_secret = self._exception_contains_secret_material(record)
        if exception_contains_secret:
            safe_message = FILTERED_LOG_MESSAGE if message_contains_secret else msg
            record.msg = f"{safe_message} {FILTERED_EXCEPTION_SUFFIX}"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        elif message_contains_secret:
            if self._is_missing_key_diagnostic(msg):
                record.msg = self._redact_text_value(msg)
            else:
                record.msg = FILTERED_LOG_MESSAGE
            record.args = ()
        return True
