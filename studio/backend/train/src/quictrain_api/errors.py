"""Shared service error type (avoid circular imports)."""

from __future__ import annotations

from typing import Any


class ServiceError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.status_code = status_code
        self.details = details or {}
        self.retryable = retryable
