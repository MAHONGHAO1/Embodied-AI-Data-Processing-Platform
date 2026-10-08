"""Classification for database failures that are safe to report as concurrency conflicts."""

from __future__ import annotations

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

_CONCURRENCY_SQLSTATES = frozenset({"40001", "40P01"})
_SERIALIZATION_PHRASES = (
    "deadlock detected",
    "could not serialize access",
    "serialization failure",
)
_SQLITE_CONCURRENCY_PHRASES = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "database is busy",
    "database busy",
    "sqlite_busy",
    "sqlite_locked",
)


def is_concurrency_operational_error(
    db: Session | None,
    exc: OperationalError,
) -> bool:
    """Recognize only known retryable concurrency failures, never generic outages."""
    original = getattr(exc, "orig", None)
    sqlstate = str(getattr(original, "sqlstate", "") or getattr(original, "pgcode", "")).upper()
    if sqlstate in _CONCURRENCY_SQLSTATES:
        return True

    message = str(original or "").lower()
    if any(phrase in message for phrase in _SERIALIZATION_PHRASES):
        return True
    try:
        dialect = db.get_bind().dialect.name if db is not None else ""
    except Exception:
        dialect = ""
    return dialect == "sqlite" and any(phrase in message for phrase in _SQLITE_CONCURRENCY_PHRASES)
