"""Bearer credential parsing for long lived external tool tokens."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from data.security.audit import emit_audit_event
from data.services.api_tokens import TOKEN_PREFIX, TokenError, resolve_token

logger = logging.getLogger("quicdata.api_tokens")
USAGE_AUDIT_WINDOW_SECONDS = 60


def looks_like_api_token(raw: str | None) -> bool:
    """Whether a Bearer value is a ``qs_<key_id>_<secret>`` token."""

    return str(raw or "").startswith(f"{TOKEN_PREFIX}_")


def parse_bearer_token(db: Session, raw: str) -> dict[str, object]:
    """Resolve an API token, apply usage limits and audit the caller."""

    row, principal = resolve_token(db, raw)
    _enforce_rate_limit(row.id)
    _audit_usage(row=row, principal=principal)
    return principal


def _rate_limit_per_minute() -> int:
    from data.config import settings

    return int(getattr(settings, "api_token_rate_limit_per_minute", 600) or 0)


def _enforce_rate_limit(token_id: int) -> None:
    from data.security.rate_limit import record_hit

    limit = _rate_limit_per_minute()
    if limit <= 0:
        return
    try:
        count = record_hit(f"api-token:{token_id}", window_seconds=60)
    except Exception:  # a limiter outage must not lock external tools out
        logger.warning("api token rate limit unavailable", exc_info=True)
        return
    if count > limit:
        raise TokenError("rate_limited")


def _audit_usage(*, row, principal: dict[str, object]) -> None:
    """Emit at most one usage event per token per window to avoid write storms."""

    from data.infra.redis_client import redis_service

    key = f"quicdata:api-token-use:{row.id}"
    try:
        with redis_service.operation() as client:
            first = client.set(key, "1", nx=True, ex=USAGE_AUDIT_WINDOW_SECONDS)
        if not first:
            return
    except Exception:  # auditing is best effort; never block a request
        logger.warning("api token usage audit skipped", exc_info=True)
        return
    emit_audit_event(
        "api_token.use",
        actor=str(principal.get("email") or ""),
        resource=f"api_token:{row.id}",
        detail={"name": row.name, "user_id": row.user_id},
    )
