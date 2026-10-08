"""Signed download tokens (time-limited, resource-bound)."""

from __future__ import annotations

import time
import uuid
from typing import Any

from jose import JWTError, jwt

from data.config import settings

ALGORITHM = "HS256"
DEFAULT_TTL_SECONDS = 600
MAX_USES_CLAIM = "max_uses"


def create_signed_download_token(
    *,
    resource_type: str,
    resource_id: str | int,
    subject: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    max_uses: int = 5,
    extra: dict[str, Any] | None = None,
) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "typ": "download",
        "rt": resource_type,
        "rid": str(resource_id),
        "sub": subject or "anonymous",
        "iat": now,
        "nbf": now,
        "exp": now + max(30, ttl_seconds),
        "jti": str(uuid.uuid4()),
        MAX_USES_CLAIM: max(1, max_uses),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def verify_signed_download_token(
    token: str,
    *,
    resource_type: str,
    resource_id: str | int,
) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise ValueError("invalid or expired signature") from exc
    if payload.get("typ") != "download":
        raise ValueError("not a download token")
    if payload.get("rt") != resource_type or str(payload.get("rid")) != str(resource_id):
        raise ValueError("resource mismatch")
    return payload


def consume_download_jti(jti: str, max_uses: int) -> bool:
    """Consume one bounded-use token through required Redis state."""
    if not jti:
        return True
    from data.infra.redis_client import redis_service

    with redis_service.operation() as client:
        key = f"quicdata:download:jti:{jti}"
        count = client.incr(key)
        if count == 1:
            client.expire(key, DEFAULT_TTL_SECONDS * 2)
        return count <= max(1, max_uses)
