import logging
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from data.config import get_runtime_config, settings
from data.database import User, get_db
from data.infra.redis_client import RedisUnavailableError
from data.realtime.revocation import revoke_realtime_session
from data.realtime.socketio import schedule_realtime_user_revocation
from data.schemas.common import LoginRequest
from data.security.audit import emit_audit_event, list_recent_audit_events
from data.security.browser_session import (
    browser_session_id,
    clear_browser_session_cookie,
    require_same_origin_cookie_request,
    set_browser_session_cookie,
)
from data.security.refresh_store import (
    get_browser_session,
    revoke_all_for_user,
    revoke_browser_session,
)
from data.services.registration_service import (
    actor_can_create_users,
    actor_can_manage_users,
    assignable_roles_for,
    create_user,
    creator_registration_view,
    get_registration_config,
    public_registration_policy,
)
from data.services.user_lifecycle import (
    UserLifecycleConflict,
    UserLifecycleError,
    set_user_active,
    set_user_role,
)
from data.utils.formatting import format_api_datetime
from data.utils.helpers import (
    get_current_user,
    get_optional_user,
    get_role_permissions,
    hash_password,
    issue_access_token,
    needs_rehash,
    require_permission,
    success,
    validate_access_token,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["认证"])
logger = logging.getLogger("quicdata.auth")


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=10)


class UpdateUserRoleRequest(BaseModel):
    role: str = Field(..., min_length=1, max_length=32)


class UpdateUserStatusRequest(BaseModel):
    is_active: bool


class RegisterUserRequest(BaseModel):
    """Self-service registration (only when mode=open)."""

    email: str = Field(..., min_length=3, max_length=128)
    password: str = Field(..., min_length=8, max_length=256)


class CreateUserRequest(BaseModel):
    """User creation on behalf of an administrator."""

    email: str = Field(..., min_length=3, max_length=128)
    password: str = Field(..., min_length=8, max_length=256)
    role: str | None = Field(default=None, min_length=1, max_length=32)


def _user_info(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "is_active": bool(user.is_active),
        "permissions": get_role_permissions(user.role),
        "must_change_password": bool(user.must_change_password),
    }


def _start_browser_session(response: Response, user: User) -> dict:
    set_browser_session_cookie(
        response,
        user_id=user.id,
        email=user.email,
        role=user.role,
        browser_session_epoch=user.browser_session_epoch,
    )
    return issue_access_token(
        user.id,
        user.email,
        user.role,
        must_change_password=bool(user.must_change_password),
        realtime_session_epoch=user.realtime_session_epoch,
    )


def _auth_error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": status_code, "message": message, "data": None},
    )


def _best_effort_revoke_all(user_id: int) -> tuple[int, bool]:
    try:
        return revoke_all_for_user(user_id), False
    except RedisUnavailableError:
        logger.warning("browser session cleanup deferred", extra={"user_id": int(user_id)})
        return 0, True


def _current_bearer_payload(request: Request, db: Session) -> dict | None:
    authorization = request.headers.get("authorization", "")
    if not authorization.lower().startswith("bearer "):
        return None
    try:
        return validate_access_token(authorization[7:].strip(), db)
    except HTTPException:
        return None


@router.post("/login")
def login(body: LoginRequest, response: Response, db: Session = Depends(get_db)):
    from data.security.rate_limit import is_rate_limited, record_hit

    if settings.security_login_rate_limit_enabled:
        block_threshold = int(settings.security_login_block_threshold or 10)
        block_window = int(settings.security_login_block_window_seconds or 600)
        bucket = f"login_block:{body.email.strip().lower()}"
        if is_rate_limited(bucket, threshold=block_threshold, window_seconds=block_window):
            emit_audit_event(
                "security.rate_limit.block",
                actor=body.email,
                detail={"reason": "login_rate_limited", "threshold": block_threshold},
                level="warning",
            )
            return _auth_error(429, "登录尝试过于频繁，请稍后再试")

    user = db.query(User).filter(User.email == body.email).first()
    if not user or not verify_password(body.password, user.password_hash):
        if settings.security_login_rate_limit_enabled:
            record_hit(
                f"login_block:{body.email.strip().lower()}",
                window_seconds=int(settings.security_login_block_window_seconds or 600),
            )
        emit_audit_event(
            "auth.login.fail",
            actor=body.email,
            detail={"reason": "invalid_credentials"},
            level="warning",
        )
        return _auth_error(401, "邮箱或密码错误")

    if not user.is_active:
        emit_audit_event(
            "auth.login.fail",
            actor=body.email,
            resource=str(user.id),
            detail={"reason": "user_inactive"},
            level="warning",
        )
        return _auth_error(401, "用户已停用")

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
        db.commit()
        db.refresh(user)

    tokens = _start_browser_session(response, user)
    emit_audit_event("auth.login.success", actor=user.email, resource=str(user.id))
    return success({**tokens, "userInfo": _user_info(user)})


@router.post("/refresh")
def refresh(request: Request, response: Response, db: Session = Depends(get_db)):
    require_same_origin_cookie_request(request)
    session_id = browser_session_id(request)
    record = get_browser_session(session_id)
    if not record:
        denied = _auth_error(401, "浏览器会话无效或已过期")
        clear_browser_session_cookie(denied)
        return denied
    user_id = int(record["user_id"])
    db_user = db.get(User, user_id)
    record_epoch = int(record.get("browser_session_epoch") or 0)
    if (
        not db_user
        or not db_user.is_active
        or record_epoch != int(db_user.browser_session_epoch or 0)
    ):
        revoke_browser_session(session_id)
        denied = _auth_error(401, "浏览器会话无效、用户已停用或不存在")
        clear_browser_session_cookie(denied)
        return denied
    tokens = issue_access_token(
        db_user.id,
        db_user.email,
        db_user.role,
        must_change_password=bool(db_user.must_change_password),
        realtime_session_epoch=db_user.realtime_session_epoch,
    )
    emit_audit_event("auth.token.refresh", actor=db_user.email, resource=str(user_id))
    return success(
        {
            **tokens,
            "userInfo": _user_info(db_user),
        }
    )


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    require_same_origin_cookie_request(request)
    session_id = browser_session_id(request)
    clear_browser_session_cookie(response)
    record = None
    redis_failed = False
    if session_id:
        try:
            record = get_browser_session(session_id)
            revoke_browser_session(session_id)
        except RedisUnavailableError:
            redis_failed = True
            logger.warning("browser session logout cleanup unavailable")
    payload = _current_bearer_payload(request, db)
    user_id = int(record["user_id"]) if record else int((payload or {}).get("sub") or 0)
    db_user = db.get(User, user_id) if user_id else None
    if redis_failed and db_user is None:
        denied = _auth_error(503, "会话服务暂时不可用，注销未完成，请稍后重试")
        clear_browser_session_cookie(denied)
        emit_audit_event(
            "auth.token.revoke",
            detail={"status": "failed", "reason": "session_store_unavailable"},
            level="warning",
        )
        return denied
    record_is_current = bool(
        db_user
        and record
        and int(record.get("browser_session_epoch") or 0) == int(db_user.browser_session_epoch or 0)
    )
    token_is_current = bool(db_user and payload)
    if db_user is not None and (record_is_current or token_is_current):
        if redis_failed:
            db_user.browser_session_epoch = int(db_user.browser_session_epoch or 0) + 1
        revoke_realtime_session(db_user)
        db.commit()
    emit_audit_event(
        "auth.token.revoke",
        actor=db_user.email if db_user is not None else None,
        resource=str(user_id) if user_id else None,
    )
    if db_user is not None and (record_is_current or token_is_current):
        schedule_realtime_user_revocation(user_id)
    return success({"ok": True})


@router.post("/change-password")
def change_password(
    body: ChangePasswordRequest,
    response: Response,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Revoke all browser sessions for the user after password change."""
    user_id = int(user.get("sub"))
    db_user = db.get(User, user_id)
    if not db_user:
        return _auth_error(404, "用户不存在")
    if not verify_password(body.old_password, db_user.password_hash):
        return _auth_error(400, "原密码错误")
    db_user.password_hash = hash_password(body.new_password)
    db_user.must_change_password = False
    db_user.password_changed_at = datetime.utcnow()
    db_user.browser_session_epoch = int(db_user.browser_session_epoch or 0) + 1
    revoke_realtime_session(db_user)
    db.commit()
    revoked, cleanup_pending = _best_effort_revoke_all(user_id)
    clear_browser_session_cookie(response)
    schedule_realtime_user_revocation(user_id)
    emit_audit_event(
        "auth.password.change",
        actor=db_user.email,
        resource=str(user_id),
        detail={"revoked_sessions": revoked, "session_cleanup_pending": cleanup_pending},
        level="warning",
    )
    return success(
        {
            "ok": True,
            "revoked_sessions": revoked,
            "session_cleanup_pending": cleanup_pending,
        }
    )


@router.put("/users/{user_id}/role")
def update_user_role(
    user_id: int,
    body: UpdateUserRoleRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Administrator updates role; revoke all browser sessions for target user after change."""
    require_permission(user, "*")
    rbac = get_runtime_config()["rbac"]
    if body.role not in (rbac.get("roles") or {}):
        return _auth_error(400, f"未知角色: {body.role}")
    try:
        target, old_role, cancelled_jobs = set_user_role(db, target_user_id=user_id, role=body.role)
        db.commit()
        db.refresh(target)
    except UserLifecycleConflict as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except UserLifecycleError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    revoked, cleanup_pending = _best_effort_revoke_all(user_id)
    schedule_realtime_user_revocation(user_id)
    emit_audit_event(
        "auth.role.change",
        actor=user.get("email"),
        resource=str(user_id),
        detail={
            "from": old_role,
            "to": body.role,
            "revoked_sessions": revoked,
            "session_cleanup_pending": cleanup_pending,
            "cancelled_job_count": len(cancelled_jobs),
        },
        level="warning",
    )
    return success(
        {
            "id": target.id,
            "email": target.email,
            "role": target.role,
            "is_active": bool(target.is_active),
            "revoked_sessions": revoked,
            "cancelled_job_count": len(cancelled_jobs),
            "session_cleanup_pending": cleanup_pending,
        }
    )


@router.patch("/users/{user_id}/status")
def update_user_status(
    user_id: int,
    body: UpdateUserStatusRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "*")
    try:
        target, cancelled_jobs = set_user_active(
            db,
            actor_user_id=int(user["sub"]),
            target_user_id=user_id,
            is_active=body.is_active,
        )
        db.commit()
        db.refresh(target)
    except UserLifecycleConflict as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except UserLifecycleError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    revoked = 0
    cleanup_pending = False
    if not target.is_active:
        revoked, cleanup_pending = _best_effort_revoke_all(target.id)
        schedule_realtime_user_revocation(target.id)
    emit_audit_event(
        "auth.user.status.change",
        actor=user.get("email"),
        resource=str(target.id),
        detail={
            "is_active": bool(target.is_active),
            "revoked_sessions": revoked,
            "session_cleanup_pending": cleanup_pending,
            "cancelled_job_count": len(cancelled_jobs),
        },
        level="warning",
    )
    return success(
        {
            "id": target.id,
            "email": target.email,
            "role": target.role,
            "is_active": bool(target.is_active),
            "revoked_sessions": revoked,
            "cancelled_job_count": len(cancelled_jobs),
            "session_cleanup_pending": cleanup_pending,
        }
    )


@router.post("/users/{user_id}/reset-password")
def reset_user_password(
    user_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Administrator resets a user password to a random one; the user must change it at next login."""
    require_permission(user, "*")
    target = db.get(User, user_id)
    if target is None:
        return _auth_error(404, "用户不存在")
    if not target.is_active:
        return _auth_error(409, "用户已停用，请先启用后再重置密码")
    temporary_password = secrets.token_urlsafe(16)
    target.password_hash = hash_password(temporary_password)
    target.must_change_password = True
    target.password_changed_at = None
    target.browser_session_epoch = int(target.browser_session_epoch or 0) + 1
    revoke_realtime_session(target)
    db.commit()
    revoked, cleanup_pending = _best_effort_revoke_all(target.id)
    schedule_realtime_user_revocation(target.id)
    emit_audit_event(
        "auth.password.reset",
        actor=user.get("email"),
        resource=str(target.id),
        detail={
            "target": target.email,
            "revoked_sessions": revoked,
            "session_cleanup_pending": cleanup_pending,
        },
        level="warning",
    )
    return success(
        {
            "id": target.id,
            "email": target.email,
            "temporary_password": temporary_password,
            "must_change_password": True,
            "revoked_sessions": revoked,
            "session_cleanup_pending": cleanup_pending,
        }
    )


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    role = user.get("role", "viewer")
    return success(
        {
            "id": user.get("sub"),
            "email": user.get("email"),
            "role": role,
            "is_active": True,
            "permissions": get_role_permissions(role),
        }
    )


@router.get("/registration/config")
def registration_config(user: dict | None = Depends(get_optional_user)):
    """Read registration policy. Anonymous access returns only public fields; administrators receive full policy."""
    if user and actor_can_manage_users(user.get("role")):
        return success(creator_registration_view(user))
    return success(public_registration_policy())


@router.post("/register")
def register(body: RegisterUserRequest, response: Response, db: Session = Depends(get_db)):
    """Public self-service registration (only when registration.mode=open)."""
    cfg = get_registration_config()
    if cfg["mode"] != "open":
        return _auth_error(403, "当前未开放自助注册")
    role = cfg.get("self_register_role") or cfg.get("default_role") or "viewer"
    user, err = create_user(db, email=body.email, password=body.password, role=role)
    if err:
        emit_audit_event(
            "auth.register.fail",
            actor=body.email,
            detail={"reason": err, "via": "self"},
            level="warning",
        )
        return _auth_error(400, err)
    emit_audit_event(
        "auth.register.success",
        actor=user.email,
        resource=str(user.id),
        detail={"role": user.role, "via": "self"},
    )
    # Issue session immediately upon successful self-registration (standard SaaS practice)
    tokens = _start_browser_session(response, user)
    return success({**tokens, "userInfo": _user_info(user)})


@router.post("/users")
def create_platform_user(
    body: CreateUserRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Administrator creates user according to registration configuration."""
    require_permission(user, "*")
    cfg = get_registration_config()
    if cfg["mode"] == "disabled":
        return _auth_error(403, "用户注册已禁用")
    if not actor_can_create_users(user.get("role")):
        return _auth_error(403, "无权注册用户")

    target_role = (body.role or cfg.get("default_role") or "viewer").strip()
    allowed = set(assignable_roles_for(user.get("role")))
    if target_role not in allowed:
        return _auth_error(
            403,
            f"当前角色不能注册为 {target_role}；可分配: {', '.join(sorted(allowed)) or '无'}",
        )

    created, err = create_user(
        db,
        email=body.email,
        password=body.password,
        role=target_role,
        must_change_password=True,
    )
    if err:
        emit_audit_event(
            "auth.register.fail",
            actor=user.get("email"),
            detail={"reason": err, "via": "admin", "target": body.email, "role": target_role},
            level="warning",
        )
        return _auth_error(400, err)

    emit_audit_event(
        "auth.register.success",
        actor=user.get("email"),
        resource=str(created.id),
        detail={"role": created.role, "via": "admin", "target": created.email},
        level="warning",
    )
    return success(
        {
            "id": created.id,
            "email": created.email,
            "role": created.role,
            "is_active": bool(created.is_active),
            "must_change_password": bool(created.must_change_password),
            "created_at": format_api_datetime(created.created_at),
        }
    )


@router.get("/users")
def list_users(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """User list: accessible only by platform administrators."""
    require_permission(user, "*")
    users = db.query(User).order_by(User.id.asc()).all()
    return success(
        [
            {
                "id": u.id,
                "email": u.email,
                "role": u.role,
                "is_active": bool(u.is_active),
                "created_at": format_api_datetime(u.created_at),
            }
            for u in users
        ]
    )


@router.get("/roles")
def list_roles(user: dict = Depends(get_current_user)):
    """RBAC role definitions (static configuration, modifiable via /config)."""
    require_permission(user, "workspace:read")
    rbac = get_runtime_config()["rbac"]
    return success(rbac)


@router.get("/security-alerts")
def security_alerts(
    limit: int = 50,
    rule: str | None = None,
    user: dict = Depends(get_current_user),
):
    """Administrator view for recent security alerts (P2)."""
    require_permission(user, "*")
    from data.security.alerts import list_recent_alerts

    return success({"items": list_recent_alerts(limit=limit, rule=rule)})


@router.get("/audit-events")
def audit_events(
    limit: int = 50,
    event: str | None = None,
    user: dict = Depends(get_current_user),
):
    """Administrator view for recent PostgreSQL security audit events."""
    require_permission(user, "*")
    return success({"items": list_recent_audit_events(limit=limit, event=event)})
