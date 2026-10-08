"""HTTP security response headers: CSP / HSTS / X-Frame-Options, etc."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp


def _browser_direct_csp_sources(settings: object) -> dict[str, tuple[str, ...]]:
    """Return exact raw/process page origins from the shared OSS policy."""
    if not getattr(settings, "oss_browser_direct_enabled", False):
        return {}
    from data.security.browser_oss import browser_csp_sources

    return browser_csp_sources(
        str(getattr(settings, "oss_browser_endpoint", "") or "").strip(),
        {
            "raw": str(getattr(settings, "oss_bucket_raw", "") or "").strip(),
            "process": str(getattr(settings, "oss_bucket_process", "") or "").strip(),
            "official": str(getattr(settings, "oss_bucket_official", "") or "").strip(),
            "export": str(getattr(settings, "oss_bucket_export", "") or "").strip(),
        },
    )


def validate_browser_direct_content_security_policy(
    policy: str,
    *,
    endpoint: str,
    buckets: dict[str, str],
) -> None:
    """Public wrapper used by deployment configuration validation and tests."""
    from data.security.browser_oss import (
        validate_browser_direct_content_security_policy as validate,
    )

    validate(policy, endpoint=endpoint, buckets=buckets)


def default_content_security_policy(settings: object) -> str:
    browser_sources = _browser_direct_csp_sources(settings)
    connect_suffix = (
        " " + " ".join(browser_sources.get("connect-src", ()))
        if browser_sources.get("connect-src")
        else ""
    )
    image_suffix = (
        " " + " ".join(browser_sources.get("img-src", ())) if browser_sources.get("img-src") else ""
    )
    media_suffix = (
        " " + " ".join(browser_sources.get("media-src", ()))
        if browser_sources.get("media-src")
        else ""
    )
    return (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        f"img-src 'self' data: blob:{image_suffix}; "
        "font-src 'self' data:; "
        f"connect-src 'self'{connect_suffix}; "
        f"media-src 'self' blob:{media_suffix}; "
        "worker-src 'self' blob:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )


# The static baseline remains useful for review and tests. Browser-direct
# origins are appended only at response time after the public endpoint is
# validated against the active settings.
DEFAULT_CSP = default_content_security_policy(None)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach browser security headers to all responses (preserves business semantics)."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        from data.config import settings

        if not getattr(settings, "security_headers_enabled", True):
            return response

        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        csp = (
            getattr(settings, "content_security_policy", "") or ""
        ).strip() or default_content_security_policy(settings)
        response.headers.setdefault("Content-Security-Policy", csp)

        hsts_max_age = int(getattr(settings, "hsts_max_age", 0) or 0)
        hsts_enabled = getattr(settings, "hsts_enabled", None)
        if hsts_enabled is False:
            hsts_max_age = 0
        elif hsts_max_age <= 0 and getattr(settings, "is_production", False):
            hsts_max_age = 31536000
        if hsts_max_age > 0:
            response.headers.setdefault(
                "Strict-Transport-Security",
                f"max-age={hsts_max_age}; includeSubDomains",
            )
        return response
