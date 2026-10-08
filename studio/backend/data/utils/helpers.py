"""Common utilities: response wrappers, password hashing, JWT, and permissions."""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from data.config import get_runtime_config, settings
from data.database import User, get_db

security = HTTPBearer(auto_error=False)
logger = logging.getLogger("quicdata.auth")

ALGORITHM = "HS256"
_pwd_hasher = None


def success(data: Any = None, message: str = "Success") -> dict:
    return {"code": 200, "message": message, "data": data}


def error(code: int, message: str) -> dict:
    return {"code": code, "message": message, "data": None}


def _get_password_hasher():
    global _pwd_hasher
    if _pwd_hasher is None:
        from argon2 import PasswordHasher

        _pwd_hasher = PasswordHasher()
    return _pwd_hasher


def _is_argon2_hash(value: str) -> bool:
    return isinstance(value, str) and value.startswith("$argon2")


def _sha256_legacy(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def hash_password(password: str) -> str:
    """Hash new passwords with argon2id."""
    return _get_password_hasher().hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    if _is_argon2_hash(hashed):
        try:
            return bool(_get_password_hasher().verify(hashed, plain))
        except Exception:
            return False
    # Fallback: legacy unsalted SHA-256
    return _sha256_legacy(plain) == hashed


def needs_rehash(hashed: str) -> bool:
    if not _is_argon2_hash(hashed):
        return True
    try:
        return bool(_get_password_hasher().check_needs_rehash(hashed))
    except Exception:
        return False


def create_access_token(
    user_id: int,
    email: str,
    role: str,
    *,
    must_change_password: bool = False,
    realtime_session_epoch: int = 0,
) -> str:
    now = int(time.time())
    expire = now + max(60, int(settings.access_token_expire_minutes) * 60)
    payload = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "typ": "access",
        "iat": now,
        "nbf": now,
        "exp": expire,
        "jti": str(uuid.uuid4()),
        "must_change_password": bool(must_change_password),
        "rte": max(0, int(realtime_session_epoch)),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def create_token(user_id: int, email: str, role: str) -> str:
    """Alias for create_access_token (kept for backward compatibility)."""
    return create_access_token(user_id, email, role)


def decode_token(token: str, *, expected_type: str | None = "access") -> dict | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except JWTError:
        return None
    typ = payload.get("typ")
    # Legacy tokens without typ/exp are always rejected (force re-login).
    if expected_type and typ != expected_type:
        # Legacy token has no typ: always reject when typ is None.
        if typ is None:
            return None
        return None
    return payload


def issue_access_token(
    user_id: int,
    email: str,
    role: str,
    *,
    must_change_password: bool = False,
    realtime_session_epoch: int = 0,
) -> dict[str, Any]:
    access = create_access_token(
        user_id,
        email,
        role,
        must_change_password=must_change_password,
        realtime_session_epoch=realtime_session_epoch,
    )
    return {
        "token": access,
        "access_token": access,
        "token_type": "bearer",
        "expires_in": max(60, int(settings.access_token_expire_minutes) * 60),
    }


def _database_authoritative_payload(payload: dict, db: Session) -> dict:
    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 无效或已过期"
        ) from exc
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户已停用或不存在")
    if payload.get("email") != user.email or payload.get("role") != user.role:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="用户权限已变更，请重新登录"
        )
    try:
        token_epoch = int(payload.get("rte", 0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 无效或已过期"
        ) from exc
    if token_epoch != int(user.realtime_session_epoch):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="会话已失效")
    return payload


def validate_access_token(token: str, db: Session) -> dict:
    """Validate an Access Token against the current database user state."""
    payload = decode_token(token, expected_type="access")
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 无效或已过期")
    return _database_authoritative_payload(payload, db)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
) -> dict:
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    from data.security.tokens import looks_like_api_token, parse_bearer_token
    from data.services.api_tokens import TokenError

    if looks_like_api_token(credentials.credentials):
        try:
            return parse_bearer_token(db, credentials.credentials)
        except TokenError as exc:
            code = (
                status.HTTP_429_TOO_MANY_REQUESTS
                if exc.code == "rate_limited"
                else status.HTTP_401_UNAUTHORIZED
            )
            raise HTTPException(
                status_code=code,
                detail={"code": exc.code, "message": "令牌无效或不可用"},
            ) from exc
    return validate_access_token(credentials.credentials, db)


def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
) -> dict | None:
    """Allow anonymous access (used with signed downloads); validates Bearer token when present."""
    if not credentials:
        return None
    from data.security.tokens import looks_like_api_token, parse_bearer_token
    from data.services.api_tokens import TokenError

    if looks_like_api_token(credentials.credentials):
        try:
            return parse_bearer_token(db, credentials.credentials)
        except TokenError:
            return None
    return validate_access_token(credentials.credentials, db)


def get_role_permissions(role: str) -> list[str]:
    rbac = get_runtime_config()["rbac"]
    role_cfg = rbac["roles"].get(role) or {}
    return list(role_cfg.get("permissions", []))


def require_permission(user: dict, permission: str) -> None:
    role = user.get("role", "annotator")
    rbac = get_runtime_config()["rbac"]
    role_cfg = rbac["roles"].get(role) or {}
    perms: list[str] = role_cfg.get("permissions", [])
    if "*" in perms:
        return
    if permission in perms:
        return
    prefix = permission.split(":")[0]
    if f"{prefix}:*" in perms:
        return
    try:
        from data.security.audit import emit_audit_event

        emit_audit_event(
            "security.authz.denied",
            actor=str(user.get("email") or user.get("sub") or "unknown"),
            resource=permission,
            detail={"role": role, "permission": permission},
            level="warning",
        )
    except Exception as exc:
        logger.warning(
            "authorization denial audit failed",
            extra={"error_type": type(exc).__name__, "permission": permission},
        )
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="权限不足")
