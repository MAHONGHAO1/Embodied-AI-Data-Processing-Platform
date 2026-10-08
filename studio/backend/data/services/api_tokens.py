"""Issue, verify, rotate and revoke long lived API tokens for external tools."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from data.database import User
from data.models.api_token import ApiToken

TOKEN_PREFIX = "qs"
ROTATION_GRACE = timedelta(hours=1)
_HASH_ALGORITHM = "pbkdf2_sha256"
_HASH_ITERATIONS = 120_000


class TokenError(Exception):
    """A token that cannot be used; ``code`` is safe to return to callers."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _hash_secret(secret: str, *, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", secret.encode("utf-8"), bytes.fromhex(salt), _HASH_ITERATIONS
    )
    return f"{_HASH_ALGORITHM}${_HASH_ITERATIONS}${salt}${digest.hex()}"


def _matches(secret: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algorithm, iterations, salt, digest = stored.split("$", 3)
    except ValueError:
        return False
    if algorithm != _HASH_ALGORITHM:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", secret.encode("utf-8"), bytes.fromhex(salt), int(iterations)
    )
    return hmac.compare_digest(candidate.hex(), digest)


def _principal(user: User) -> dict[str, object]:
    """Return the same principal shape the JWT path produces."""

    return {"sub": str(user.id), "role": user.role, "email": user.email}


def issue_token(
    db: Session,
    *,
    user_id: int,
    name: str,
    expires_in_days: int | None = 90,
    created_by: int | None = None,
) -> dict[str, object]:
    """Create a token; the plaintext secret is returned exactly once."""

    key_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(days=expires_in_days) if expires_in_days else None
    row = ApiToken(
        user_id=user_id,
        name=name,
        key_id=key_id,
        secret_hash=_hash_secret(secret),
        expires_at=expires_at,
        created_by=created_by,
    )
    db.add(row)
    db.flush()
    return {
        "id": row.id,
        "name": row.name,
        "key_id": key_id,
        "secret": f"{TOKEN_PREFIX}_{key_id}_{secret}",
        "expires_at": expires_at,
        "created_at": row.created_at,
    }


def verify_token(db: Session, raw: str) -> dict[str, object]:
    """Resolve ``qs_<key_id>_<secret>`` into a principal or raise ``TokenError``."""

    _row, principal = resolve_token(db, raw)
    return principal


def resolve_token(db: Session, raw: str) -> tuple[ApiToken, dict[str, object]]:
    """Resolve a token into ``(row, principal)``; callers may apply usage limits."""

    parts = str(raw or "").split("_", 2)
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        raise TokenError("invalid_token")
    key_id, secret = parts[1], parts[2]
    row = db.query(ApiToken).filter(ApiToken.key_id == key_id).one_or_none()
    if row is None:
        raise TokenError("invalid_token")
    if row.revoked_at is not None:
        raise TokenError("revoked")

    now = datetime.utcnow()
    if _matches(secret, row.secret_hash):
        pass
    elif _matches(secret, row.previous_secret_hash):
        if row.rotated_at is None or now - row.rotated_at > ROTATION_GRACE:
            raise TokenError("revoked")
    else:
        raise TokenError("invalid_token")

    if row.expires_at is not None and row.expires_at < now:
        raise TokenError("expired")
    user = db.get(User, row.user_id)
    if user is None or not bool(user.is_active):
        raise TokenError("invalid_token")
    row.last_used_at = now
    db.flush()
    return row, _principal(user)


def rotate_token(db: Session, *, token_id: int) -> dict[str, object]:
    """Issue a new secret for the same named token and keep the old one briefly."""

    row = db.get(ApiToken, token_id)
    if row is None or row.revoked_at is not None:
        raise TokenError("invalid_token")
    secret = secrets.token_urlsafe(32)
    row.previous_secret_hash = row.secret_hash
    row.secret_hash = _hash_secret(secret)
    row.rotated_at = datetime.utcnow()
    db.flush()
    return {
        "id": row.id,
        "name": row.name,
        "key_id": row.key_id,
        "secret": f"{TOKEN_PREFIX}_{row.key_id}_{secret}",
        "expires_at": row.expires_at,
        "rotated_at": row.rotated_at,
    }


def revoke_token(db: Session, *, token_id: int) -> None:
    row = db.get(ApiToken, token_id)
    if row is None:
        raise TokenError("invalid_token")
    row.revoked_at = datetime.utcnow()
    db.flush()
