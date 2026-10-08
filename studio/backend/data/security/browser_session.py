"""Cookie boundary for opaque browser sessions."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response, status

from data.config import settings
from data.security.refresh_store import create_browser_session

SESSION_COOKIE_NAME = "quicdata_session"


def _cookie_path() -> str:
    return f"{settings.api_prefix.rstrip('/')}/auth"


def set_browser_session_cookie(
    response: Response,
    *,
    user_id: int,
    email: str,
    role: str,
    browser_session_epoch: int,
) -> None:
    session_id = create_browser_session(
        user_id=user_id,
        email=email,
        role=role,
        browser_session_epoch=browser_session_epoch,
    )
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=max(3600, int(settings.refresh_token_expire_days) * 86400),
        path=_cookie_path(),
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_browser_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path=_cookie_path(),
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def browser_session_id(request: Request) -> str:
    return str(request.cookies.get(SESSION_COOKIE_NAME) or "").strip()


def require_same_origin_cookie_request(request: Request) -> None:
    if request.headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="拒绝跨站会话请求")
    origin = request.headers.get("origin", "").strip()
    if not origin:
        return
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="会话请求来源无效")
    host = request.headers.get("host", "").strip().lower()
    allowed = {item.rstrip("/").lower() for item in settings.cors_origin_list}
    if host:
        allowed.update({f"http://{host}", f"https://{host}"})
    if origin.rstrip("/").lower() not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="会话请求来源无效")
