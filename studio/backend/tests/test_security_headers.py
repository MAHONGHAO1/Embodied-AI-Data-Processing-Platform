from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from data.config import settings
from data.security.headers import SecurityHeadersMiddleware, default_content_security_policy


def _directive(policy: str, name: str) -> str:
    return next(
        directive.strip()
        for directive in policy.split(";")
        if directive.strip().startswith(f"{name} ")
    )


def test_browser_direct_csp_allows_fetch_only_to_raw_bucket_origin():
    settings = SimpleNamespace(
        oss_browser_direct_enabled=True,
        oss_browser_endpoint="https://oss-cn-beijing.aliyuncs.com",
        oss_bucket_raw="quic-data-platform",
    )

    policy = default_content_security_policy(settings)
    connect_src = _directive(policy, "connect-src")

    assert (
        connect_src == "connect-src 'self' https://quic-data-platform.oss-cn-beijing.aliyuncs.com"
    )


def test_browser_direct_csp_rejects_an_invalid_raw_bucket_source():
    settings = SimpleNamespace(
        oss_browser_direct_enabled=True,
        oss_browser_endpoint="https://oss-cn-beijing.aliyuncs.com",
        oss_bucket_raw="raw; connect-src https://attacker.example",
    )

    policy = default_content_security_policy(settings)

    assert _directive(policy, "connect-src") == "connect-src 'self'"


def test_browser_direct_csp_keeps_connect_src_same_origin_when_disabled():
    settings = SimpleNamespace(
        oss_browser_direct_enabled=False,
        oss_browser_endpoint="https://oss-cn-beijing.aliyuncs.com",
        oss_bucket_raw="quic-data-platform",
    )

    policy = default_content_security_policy(settings)

    assert _directive(policy, "connect-src") == "connect-src 'self'"


def test_production_hsts_can_be_disabled_for_http_uat(monkeypatch):
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/")
    def home():
        return {"ok": True}

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "hsts_max_age", 31536000)
    monkeypatch.setattr(settings, "hsts_enabled", False)

    with TestClient(app) as client:
        response = client.get("/")

    assert "strict-transport-security" not in response.headers
