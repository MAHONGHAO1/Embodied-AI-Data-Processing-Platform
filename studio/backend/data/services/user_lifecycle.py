"""Authoritative user activation transitions."""

from __future__ import annotations

from sqlalchemy.orm import Session

from data.database import User
from data.realtime.revocation import revoke_realtime_session
from data.services.job_runs import cancel_unclaimed_jobs_for_actor


class UserLifecycleError(ValueError):
    pass


class UserLifecycleConflict(UserLifecycleError):
    pass


def set_user_active(
    db: Session,
    *,
    actor_user_id: int,
    target_user_id: int,
    is_active: bool,
) -> tuple[User, list[str]]:
    target = db.query(User).filter(User.id == target_user_id).with_for_update().one_or_none()
    if target is None:
        raise UserLifecycleError("用户不存在")
    desired = bool(is_active)
    if target.is_active == desired:
        return target, []
    if not desired and int(target.id) == int(actor_user_id):
        raise UserLifecycleConflict("不能停用当前登录用户")
    if not desired and target.role == "admin":
        active_admins = (
            db.query(User)
            .filter(User.role == "admin", User.is_active.is_(True))
            .with_for_update()
            .all()
        )
        if len(active_admins) <= 1:
            raise UserLifecycleConflict("不能停用最后一个启用中的管理员")
    target.is_active = desired
    cancelled_jobs: list[str] = []
    if not desired:
        revoke_realtime_session(target)
        target.browser_session_epoch = int(target.browser_session_epoch or 0) + 1
        cancelled_jobs = cancel_unclaimed_jobs_for_actor(db, actor_id=target.id)
    db.flush()
    return target, cancelled_jobs


def set_user_role(
    db: Session,
    *,
    target_user_id: int,
    role: str,
) -> tuple[User, str, list[str]]:
    target = db.query(User).filter(User.id == target_user_id).with_for_update().one_or_none()
    if target is None:
        raise UserLifecycleError("用户不存在")
    desired_role = str(role).strip()
    old_role = str(target.role)
    if old_role == desired_role:
        return target, old_role, []
    if target.is_active and old_role == "admin" and desired_role != "admin":
        active_admins = (
            db.query(User)
            .filter(User.role == "admin", User.is_active.is_(True))
            .with_for_update()
            .all()
        )
        if len(active_admins) <= 1:
            raise UserLifecycleConflict("不能修改最后一个启用中的管理员角色")
    target.role = desired_role
    revoke_realtime_session(target)
    target.browser_session_epoch = int(target.browser_session_epoch or 0) + 1
    cancelled_jobs = cancel_unclaimed_jobs_for_actor(db, actor_id=target.id)
    db.flush()
    return target, old_role, cancelled_jobs
