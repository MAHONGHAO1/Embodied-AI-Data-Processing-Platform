"""Map a QuicStudio access token onto a QuicTrain project actor.

QuicTrain endpoints keep reading ``Authorization: Bearer``. This module does
not change that contract. It only issues a short-lived QuicTrain session after
the Studio token and train permissions have been checked.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

_TRAIN_SRC = Path(__file__).resolve().parent / "src"
if _TRAIN_SRC.is_dir() and str(_TRAIN_SRC) not in sys.path:
    sys.path.insert(0, str(_TRAIN_SRC))

_TOKEN_TTL_SECONDS = 3600
_tokens: dict[tuple[str, str], tuple[str, float]] = {}


def project_role_for_permissions(permissions: list[str] | set[str] | tuple[str, ...]) -> str | None:
    """train:* / train:write -> operator, * -> admin. train:read alone is not enough for V1."""

    perms = {item for item in permissions if isinstance(item, str)}
    if "*" in perms:
        return "admin"
    if "train:write" in perms or "train:*" in perms:
        return "operator"
    return None


def _permissions_of(user: dict[str, Any]) -> list[str]:
    from data.utils.helpers import get_role_permissions

    return list(get_role_permissions(str(user.get("role") or "viewer")))


def studio_user_from_token(token: str) -> dict[str, Any] | None:
    from fastapi import HTTPException

    from data.database import SessionLocal
    from data.utils.helpers import validate_access_token

    db = SessionLocal()
    try:
        return validate_access_token(token, db)
    except HTTPException:
        return None
    finally:
        db.close()


def sync_studio_user(user: dict[str, Any]) -> tuple[str, str]:
    """Upsert the Studio user into QuicTrain and return ``(user_id, bearer)``."""

    role = project_role_for_permissions(_permissions_of(user))
    if role is None:
        raise PermissionError("train:write")
    subject = str(user.get("sub") or user.get("id") or "")
    if not subject:
        raise PermissionError("train:write")
    cache_key = (subject, role)
    cached = _tokens.get(cache_key)
    now = time.time()
    if cached and cached[1] > now:
        return f"usr_s{subject}"[:48], cached[0]

    from quictrain_core import new_id
    from sqlalchemy import select

    from quictrain_api.auth import issue_session
    from quictrain_api.db import (
        ProjectMembershipRecord,
        SessionLocal,
        UserRecord,
        init_database,
    )
    from quictrain_api.settings import get_settings

    init_database()
    user_id = f"usr_s{subject}"[:48]
    email = f"studio-{subject}@quicstudio.local"[:240]
    display = str(user.get("email") or email).split("@", 1)[0][:120] or "studio"
    project_id = get_settings().default_project_id
    with SessionLocal() as session:
        record = session.get(UserRecord, user_id)
        if record is None:
            record = UserRecord(
                id=user_id,
                display_name=display,
                email=email,
                role="user",
                active=True,
            )
            session.add(record)
            session.flush()
        membership = session.scalar(
            select(ProjectMembershipRecord).where(
                ProjectMembershipRecord.project_id == project_id,
                ProjectMembershipRecord.user_id == user_id,
            )
        )
        if membership is None:
            session.add(
                ProjectMembershipRecord(
                    id=new_id("mem"),
                    project_id=project_id,
                    user_id=user_id,
                    role=role,
                )
            )
        elif membership.role != role:
            membership.role = role
        raw, _session_row = issue_session(session, record)
        session.commit()
    _tokens[cache_key] = (raw, now + _TOKEN_TTL_SECONDS)
    return user_id, raw


def _mounted_path(scope: dict) -> str:
    """Path seen by the mounted app, after Starlette strips ``root_path``."""

    path = scope.get("path") or ""
    root = scope.get("root_path") or ""
    if root and path.startswith(root) and (path == root or path[len(root) : len(root) + 1] == "/"):
        return path[len(root) :] or "/"
    return path


class StudioAuthBridge:
    """ASGI wrapper that replaces a Studio bearer with a QuicTrain session."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = _mounted_path(scope)
        if path == "/health":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers") or []}
        authorization = headers.get(b"authorization", b"").decode("latin1")
        if not authorization.lower().startswith("bearer "):
            await _json(send, 401, "未登录")
            return
        user = studio_user_from_token(authorization.split(" ", 1)[1].strip())
        if user is None:
            await _json(send, 401, "登录已失效")
            return
        try:
            try:
                from train.mount import startup_train
            except ImportError:
                from mount import startup_train

            startup_train()
            _user_id, token = sync_studio_user(user)
        except PermissionError:
            await _json(send, 403, "权限不足")
            return
        except Exception:
            await _json(send, 503, "训练控制面不可用")
            return
        rewritten = dict(scope)
        kept = [
            (key, value)
            for key, value in scope.get("headers") or []
            if key.lower() != b"authorization"
        ]
        kept.append((b"authorization", f"Bearer {token}".encode("latin1")))
        rewritten["headers"] = kept
        await self.app(rewritten, receive, send)


async def _json(send: Any, status: int, message: str) -> None:
    body = (f'{{"error":{{"code":"STUDIO_AUTH","message":"{message}"}}}}').encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
