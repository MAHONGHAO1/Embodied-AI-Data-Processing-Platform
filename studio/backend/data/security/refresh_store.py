"""Required Redis storage for opaque browser sessions."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any

from data.config import settings
from data.infra.redis_client import redis_service

SESSION_PREFIX = "quicdata:auth:session:"
USER_SESSION_PREFIX = "quicdata:auth:user-sessions:"


def _session_digest(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _session_key(session_id: str) -> str:
    return f"{SESSION_PREFIX}{_session_digest(session_id)}"


def _user_session_key(user_id: int) -> str:
    return f"{USER_SESSION_PREFIX}{int(user_id)}"


def create_browser_session(
    *, user_id: int, email: str, role: str, browser_session_epoch: int = 0
) -> str:
    session_id = secrets.token_urlsafe(48)
    now = int(time.time())
    expires_at = now + max(3600, int(settings.refresh_token_expire_days) * 86400)
    payload = {
        "user_id": int(user_id),
        "email": email,
        "role": role,
        "browser_session_epoch": max(0, int(browser_session_epoch)),
        "issued_at": now,
        "expires_at": expires_at,
    }
    session_digest = _session_digest(session_id)
    ttl = expires_at - now
    with redis_service.operation() as client:
        pipe = client.pipeline()
        pipe.setex(_session_key(session_id), ttl, json.dumps(payload))
        pipe.sadd(_user_session_key(user_id), session_digest)
        pipe.expire(_user_session_key(user_id), ttl)
        pipe.execute()
    return session_id


def get_browser_session(session_id: str) -> dict[str, Any] | None:
    if not session_id:
        return None
    with redis_service.operation() as client:
        raw = client.get(_session_key(session_id))
    if not raw:
        return None
    try:
        record = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        with redis_service.operation() as client:
            client.delete(_session_key(session_id))
        return None
    if not isinstance(record, dict):
        with redis_service.operation() as client:
            client.delete(_session_key(session_id))
        return None
    if int(record.get("expires_at") or 0) <= int(time.time()):
        revoke_browser_session(session_id)
        return None
    return record


def revoke_browser_session(session_id: str) -> None:
    if not session_id:
        return
    digest = _session_digest(session_id)
    key = _session_key(session_id)
    with redis_service.operation() as client:
        raw = client.get(key)
        user_id = None
        if raw:
            try:
                user_id = int(json.loads(raw).get("user_id") or 0)
            except (TypeError, ValueError, json.JSONDecodeError):
                user_id = None
        pipe = client.pipeline()
        pipe.delete(key)
        if user_id:
            pipe.srem(_user_session_key(user_id), digest)
        pipe.execute()


def revoke_all_for_user(user_id: int) -> int:
    """Delete one user's sessions through its bounded Redis index."""
    with redis_service.operation() as client:
        index_key = _user_session_key(user_id)
        digests = tuple(client.smembers(index_key))
        if not digests:
            client.delete(index_key)
            return 0
        pipe = client.pipeline()
        pipe.delete(*(f"{SESSION_PREFIX}{digest}" for digest in digests))
        pipe.delete(index_key)
        pipe.execute()
    return len(digests)
