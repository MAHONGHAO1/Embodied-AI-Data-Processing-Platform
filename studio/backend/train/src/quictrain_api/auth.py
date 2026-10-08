"""Approved local bootstrap / password sessions / OIDC reservation and project RBAC helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from quictrain_core import new_id
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import AuthSessionRecord, ProjectMembershipRecord, UserRecord, get_session, utcnow
from .errors import ServiceError
from .settings import get_settings

ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}
_PBKDF2_ROUNDS = 390_000


@dataclass(frozen=True)
class Actor:
    user_id: str
    display_name: str
    global_role: str
    project_role: str | None = None
    email: str | None = None

    def require_project_role(self, minimum: str) -> None:
        if self.project_role is None:
            raise ServiceError(
                "PROJECT_ACCESS_DENIED",
                "当前身份无权访问该项目。",
                status_code=403,
            )
        if ROLE_RANK.get(self.project_role, 0) < ROLE_RANK.get(minimum, 99):
            raise ServiceError(
                "INSUFFICIENT_ROLE",
                f"需要项目角色 {minimum} 或更高。",
                status_code=403,
                details={"role": self.project_role, "required": minimum},
            )


def hash_bootstrap_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str, *, salt_hex: str | None = None) -> str:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        scheme, rounds_s, salt_hex, digest_hex = encoded.split("$", 3)
    except ValueError:
        return False
    if scheme != "pbkdf2_sha256":
        return False
    try:
        rounds = int(rounds_s)
    except ValueError:
        return False
    salt = bytes.fromhex(salt_hex)
    expected = bytes.fromhex(digest_hex)
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(actual, expected)


def issue_session(session: Session, user: UserRecord) -> tuple[str, AuthSessionRecord]:
    settings = get_settings()
    raw = secrets.token_urlsafe(32)
    record = AuthSessionRecord(
        id=new_id("ses"),
        user_id=user.id,
        token_hash=hash_bootstrap_token(raw),
        expires_at=utcnow() + timedelta(hours=max(1, settings.session_ttl_hours)),
        created_at=utcnow(),
    )
    session.add(record)
    session.flush()
    return raw, record


def revoke_session_token(session: Session, raw_token: str) -> None:
    token_hash = hash_bootstrap_token(raw_token)
    record = session.scalar(
        select(AuthSessionRecord).where(AuthSessionRecord.token_hash == token_hash)
    )
    if record is not None and record.revoked_at is None:
        record.revoked_at = utcnow()


def _user_from_session_token(session: Session, token: str) -> UserRecord | None:
    token_hash = hash_bootstrap_token(token)
    record = session.scalar(
        select(AuthSessionRecord).where(AuthSessionRecord.token_hash == token_hash)
    )
    if record is None or record.revoked_at is not None:
        return None
    expires = record.expires_at
    if expires.tzinfo is None:
        from datetime import UTC

        expires = expires.replace(tzinfo=UTC)
    if expires <= utcnow():
        return None
    return session.get(UserRecord, record.user_id)


def _b64url_json(segment: str) -> dict[str, Any]:
    padding = "=" * (-len(segment) % 4)
    raw = base64.urlsafe_b64decode(segment + padding)
    return json.loads(raw.decode("utf-8"))


def resolve_oidc_claims(token: str) -> dict[str, Any]:
    """OIDC Bearer resolution.

    Production: configure issuer/audience/jwks and verify signatures (RESERVED).
    Dev: QUICTRAIN_OIDC_DEV_UNSIGNED=1 accepts header.payload without signature verify.
    """

    settings = get_settings()
    if not settings.oidc_issuer or not settings.oidc_audience:
        raise ServiceError(
            "OIDC_NOT_CONFIGURED",
            "auth_mode=oidc 需要 QUICTRAIN_OIDC_ISSUER 与 QUICTRAIN_OIDC_AUDIENCE。"
            " JWKS 校验预留：设置 QUICTRAIN_OIDC_JWKS_URL 后在生产启用签名验证。",
            status_code=501,
            details={
                "issuer": settings.oidc_issuer,
                "audience": settings.oidc_audience,
                "jwks_url": settings.oidc_jwks_url,
                "status": "RESERVED_PENDING_IDP",
            },
        )
    parts = token.split(".")
    if len(parts) < 2:
        raise ServiceError("UNAUTHENTICATED", "OIDC token 格式无效。", status_code=401)
    if not settings.oidc_dev_unsigned and not settings.oidc_jwks_url:
        raise ServiceError(
            "OIDC_JWKS_REQUIRED",
            "生产 OIDC 需要 QUICTRAIN_OIDC_JWKS_URL；开发可用 OIDC_DEV_UNSIGNED=1。",
            status_code=501,
            details={"status": "RESERVED_PENDING_JWKS"},
        )
    # Signature verification against JWKS is reserved for IdP wiring.
    claims = _b64url_json(parts[1])
    if claims.get("iss") and claims["iss"] != settings.oidc_issuer:
        raise ServiceError("UNAUTHENTICATED", "OIDC issuer 不匹配。", status_code=401)
    aud = claims.get("aud")
    if isinstance(aud, list):
        if settings.oidc_audience not in aud:
            raise ServiceError("UNAUTHENTICATED", "OIDC audience 不匹配。", status_code=401)
    elif aud and aud != settings.oidc_audience:
        raise ServiceError("UNAUTHENTICATED", "OIDC audience 不匹配。", status_code=401)
    return claims


def resolve_actor(
    session: Session,
    *,
    authorization: str | None = None,
    actor_header: str | None = None,
    project_id: str | None = None,
) -> Actor:
    settings = get_settings()
    mode = settings.auth_mode

    user: UserRecord | None = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        if mode == "studio":
            # Direct QuicStudio JWT validation against the shared database
            try:
                from fastapi import HTTPException

                from data.utils.helpers import validate_access_token

                payload = validate_access_token(token, session)
                global_role = payload.get("role", "annotator")
                project_role = "admin" if global_role == "admin" else "viewer"
                user_id = str(payload.get("sub", ""))
                email = payload.get("email") or ""
                display_name = email.split("@")[0] if email else "User"
                return Actor(
                    user_id=user_id,
                    display_name=display_name,
                    global_role=global_role,
                    project_role=project_role,
                    email=email,
                )
            except HTTPException as exc:
                raise ServiceError(
                    "UNAUTHENTICATED", str(exc.detail), status_code=exc.status_code
                ) from exc
            except Exception:
                # Fallback for non-JWT tokens (e.g. test session tokens or bootstrap tokens)
                user = _user_from_session_token(session, token)
                if user is None:
                    token_hash = hash_bootstrap_token(token)
                    user = session.scalar(
                        select(UserRecord).where(UserRecord.bootstrap_token_hash == token_hash)
                    )
                if (
                    user is None
                    and settings.bootstrap_admin_token
                    and token == settings.bootstrap_admin_token
                ):
                    user = session.get(UserRecord, 1) or session.get(UserRecord, "usr_demo")
        elif mode == "oidc":
            claims = resolve_oidc_claims(token)
            subject = str(claims.get("sub") or "")
            email = str(claims.get("email") or f"{subject}@oidc.local")
            display = str(claims.get("name") or email)
            if not subject:
                raise ServiceError("UNAUTHENTICATED", "OIDC sub 缺失。", status_code=401)
            user = session.scalar(select(UserRecord).where(UserRecord.email == email))
            if user is None:
                user = session.get(UserRecord, f"usr_{subject[:24]}")
            if user is None:
                user = UserRecord(
                    id=f"usr_{hashlib.sha256(subject.encode()).hexdigest()[:16]}",
                    display_name=display,
                    email=email,
                    role="user",
                    active=True,
                )
                session.add(user)
                session.flush()
        else:
            # Prefer opaque login sessions, then per-user bootstrap tokens.
            user = _user_from_session_token(session, token)
            if user is None:
                token_hash = hash_bootstrap_token(token)
                user = session.scalar(
                    select(UserRecord).where(UserRecord.bootstrap_token_hash == token_hash)
                )
            if (
                user is None
                and settings.bootstrap_admin_token
                and token == settings.bootstrap_admin_token
            ):
                user = session.get(UserRecord, 1) or session.get(UserRecord, "usr_demo")
    elif mode == "open":
        user_id = actor_header or "usr_demo"
        user = session.get(UserRecord, user_id)
        if user is None and (user_id in {"usr_demo", "1"}):
            user = UserRecord(
                id="usr_demo",
                display_name="Kirito",
                email="kirito@quicrobot.local",
                role="admin",
            )
    elif mode in {"studio", "local", "bootstrap"}:
        raise ServiceError(
            "UNAUTHENTICATED",
            "需要登录后的 Authorization: Bearer <token>。",
            status_code=401,
        )
    elif mode == "oidc":
        raise ServiceError(
            "UNAUTHENTICATED",
            "需要 Authorization: Bearer <oidc-access-token>。",
            status_code=401,
        )
    else:
        raise ServiceError(
            "AUTH_MODE_UNSUPPORTED",
            f"不支持的 auth_mode={mode}；请使用 studio、open、local、bootstrap 或 oidc。",
            status_code=501,
        )

    if user is None or not user.active:
        raise ServiceError("UNAUTHENTICATED", "身份无效或未激活。", status_code=401)

    project_role: str | None = None
    if mode == "studio":
        project_role = "admin" if user.role == "admin" else "viewer"
    elif project_id:
        membership = session.scalar(
            select(ProjectMembershipRecord).where(
                ProjectMembershipRecord.project_id == project_id,
                ProjectMembershipRecord.user_id == str(user.id),
            )
        )
        if membership is None and mode == "open" and str(user.id) in {"usr_demo", "1"}:
            if project_id == settings.default_project_id:
                project_role = "admin"
            else:
                project_role = None
        elif membership is not None:
            project_role = membership.role
        else:
            project_role = None

    return Actor(
        user_id=str(user.id),
        display_name=user.display_name,
        global_role=user.role,
        project_role=project_role,
        email=user.email,
    )


def get_actor(
    request: Request,
    session: Session = Depends(get_session),
    authorization: Annotated[str | None, Header()] = None,
    x_quic_actor: Annotated[str | None, Header(alias="X-Quic-Actor")] = None,
) -> Actor:
    project_id = request.headers.get("X-Quic-Project") or get_settings().default_project_id
    return resolve_actor(
        session,
        authorization=authorization,
        actor_header=x_quic_actor,
        project_id=project_id,
    )


def require_project_access(
    *,
    actor: Actor,
    minimum_role: str = "viewer",
) -> Actor:
    actor.require_project_role(minimum_role)
    return actor


def enforce_actor(
    session: Session,
    *,
    project_id: str,
    authorization: str | None = None,
    actor_header: str | None = None,
    minimum_role: str = "viewer",
) -> Actor:
    """Resolve identity and enforce a minimum project role in one step."""

    actor = resolve_actor(
        session,
        authorization=authorization,
        actor_header=actor_header,
        project_id=project_id,
    )
    actor.require_project_role(minimum_role)
    return actor


VALID_PROJECT_ROLES = frozenset(ROLE_RANK)


def identity_status() -> dict[str, Any]:
    settings = get_settings()
    return {
        "auth_mode": settings.auth_mode,
        "login_required": settings.auth_mode in {"studio", "local", "bootstrap", "oidc"},
        "login_methods": (
            ["jwt"]
            if settings.auth_mode == "studio"
            else (
                ["password"]
                if settings.auth_mode in {"local", "bootstrap", "open"}
                else (["oidc"] if settings.auth_mode == "oidc" else [])
            )
        ),
        "stable_domain_ready": settings.stable_domain_ready,
        "public_base_url": settings.public_base_url,
        "break_glass_basic_auth_enabled": settings.break_glass_basic_auth_enabled,
        "oidc": {
            "issuer": settings.oidc_issuer,
            "audience": settings.oidc_audience,
            "jwks_url": settings.oidc_jwks_url,
            "dev_unsigned": settings.oidc_dev_unsigned,
            "status": (
                "configured"
                if settings.oidc_issuer and settings.oidc_audience
                else "RESERVED_PENDING_IDP"
            ),
        },
    }
