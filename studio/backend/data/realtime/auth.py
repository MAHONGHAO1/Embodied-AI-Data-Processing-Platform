"""Authentication for Socket.IO connections without URL or token persistence."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from data.config import settings
from data.database import User
from data.utils.helpers import decode_token


class RealtimeAuthenticationError(PermissionError):
    """Raised when a Socket.IO handshake cannot establish a trusted user."""


@dataclass(frozen=True)
class RealtimeIdentity:
    user_id: int
    email: str
    role: str
    expires_at: int
    session_epoch: int


def authenticate_socket(
    db: Session,
    *,
    auth: object,
    origin: str | None,
) -> RealtimeIdentity:
    """Validate a browser handshake against the current database user record."""
    _require_allowed_origin(origin)
    token = _access_token_from_auth(auth)
    payload = decode_token(token, expected_type="access")
    if not payload:
        raise RealtimeAuthenticationError("access token is invalid or expired")
    try:
        user_id = int(payload["sub"])
        expires_at = int(payload["exp"])
        session_epoch = int(payload.get("rte", 0))
    except (KeyError, TypeError, ValueError) as exc:
        raise RealtimeAuthenticationError("access token is invalid or expired") from exc

    user = db.get(User, user_id)
    if user is None or not user.is_active or user.must_change_password:
        raise RealtimeAuthenticationError("socket user is unavailable")
    if session_epoch != user.realtime_session_epoch:
        raise RealtimeAuthenticationError("socket session has been revoked")
    return RealtimeIdentity(
        user_id=user.id,
        email=user.email,
        role=user.role,
        expires_at=expires_at,
        session_epoch=session_epoch,
    )


def _access_token_from_auth(auth: object) -> str:
    if not isinstance(auth, dict):
        raise RealtimeAuthenticationError("access token is required")
    token = auth.get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise RealtimeAuthenticationError("access token is required")
    return token.strip()


def _require_allowed_origin(origin: str | None) -> None:
    if not origin:
        raise RealtimeAuthenticationError("socket origin is required")
    allowed_origins = settings.cors_origin_list
    if "*" not in allowed_origins and origin not in allowed_origins:
        raise RealtimeAuthenticationError("socket origin is not allowed")
