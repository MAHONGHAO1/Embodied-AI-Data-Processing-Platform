from __future__ import annotations

import pytest

from data.config import settings
from data.infra.redis_client import RedisService, RedisUnavailableError, redis_service
from data.security import alerts, rate_limit, refresh_store


def test_redis_service_rejects_a_missing_url(monkeypatch):
    service = RedisService()
    monkeypatch.setattr(settings, "redis_url", "")

    with pytest.raises(RedisUnavailableError, match="redis_unavailable"):
        service.connect_required()


def test_redis_service_translates_connection_failures(monkeypatch):
    class BrokenClient:
        def ping(self):
            raise OSError("connection details must not escape")

    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/15")
    monkeypatch.setattr("redis.from_url", lambda *_args, **_kwargs: BrokenClient())
    service = RedisService()

    with pytest.raises(RedisUnavailableError, match="redis_unavailable"):
        service.connect_required()


def test_rate_limit_has_no_process_memory_fallback():
    redis_service.client.delete("quicdata:rl:required-test")

    assert rate_limit.get_hit_count("required-test", window_seconds=60) == 0
    assert rate_limit.record_hit("required-test", window_seconds=60) == 1
    assert rate_limit.get_hit_count("required-test", window_seconds=60) == 1
    assert not hasattr(rate_limit, "_memory_hits")


def test_browser_session_store_hashes_opaque_ids_and_has_no_memory_fallback():
    session_id = refresh_store.create_browser_session(
        user_id=9,
        email="redis-required@example.com",
        role="viewer",
    )
    keys = list(redis_service.client.scan_iter(match=f"{refresh_store.SESSION_PREFIX}*"))

    assert session_id
    assert all(session_id not in key for key in keys)
    assert refresh_store.get_browser_session(session_id)["email"] == "redis-required@example.com"
    assert not hasattr(refresh_store, "_memory_store")


def test_browser_session_revocation_uses_a_per_user_index():
    first = refresh_store.create_browser_session(
        user_id=901,
        email="first-indexed@example.com",
        role="viewer",
        browser_session_epoch=3,
    )
    second = refresh_store.create_browser_session(
        user_id=902,
        email="second-indexed@example.com",
        role="viewer",
        browser_session_epoch=4,
    )

    assert refresh_store.revoke_all_for_user(901) == 1
    assert refresh_store.get_browser_session(first) is None
    assert refresh_store.get_browser_session(second)["browser_session_epoch"] == 4


def test_corrupt_browser_session_record_fails_closed():
    session_id = "corrupt-browser-session"
    redis_service.client.setex(refresh_store._session_key(session_id), 60, "not-json")

    assert refresh_store.get_browser_session(session_id) is None
    assert redis_service.client.get(refresh_store._session_key(session_id)) is None


def test_security_alert_history_has_no_process_memory_fallback(monkeypatch):
    monkeypatch.setattr(alerts, "_recent_persisted_alerts", lambda **_kwargs: [])

    assert alerts.list_recent_alerts() == []
    assert not hasattr(alerts, "_memory_alerts")


def test_health_fails_closed_when_redis_ping_fails(monkeypatch):
    from data.main import health

    def unavailable():
        raise RedisUnavailableError()

    monkeypatch.setattr(redis_service, "connect_required", unavailable)

    with pytest.raises(RedisUnavailableError, match="redis_unavailable"):
        health()


def test_celery_processes_verify_redis_and_do_not_retry_startup_forever(monkeypatch):
    from data import celery_app as celery_module
    from scripts import verify_redis

    calls = []

    class RequiredRedis:
        def connect_required(self):
            calls.append("connect")

        def disconnect(self):
            calls.append("disconnect")

    monkeypatch.setattr(verify_redis, "RedisService", RequiredRedis)

    verify_redis.main()

    assert calls == ["connect", "disconnect"]
    assert celery_module.celery_app.conf.broker_connection_retry_on_startup is False
