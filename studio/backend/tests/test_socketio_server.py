import pytest

from data.config import settings
from data.infra.redis_client import RedisUnavailableError
from data.realtime.socketio import create_socket_server


def test_socket_server_uses_asgi_websocket_transport_and_configured_origins(monkeypatch):
    monkeypatch.setattr(settings, "cors_origins", "http://127.0.0.1:8010")

    server = create_socket_server()

    assert server.async_mode == "asgi"
    assert server.eio.cors_allowed_origins == ["http://127.0.0.1:8010"]
    assert server.eio.transports == ["websocket"]


def test_socket_server_uses_the_selected_loopback_port_when_development_cors_is_unset(monkeypatch):
    monkeypatch.setattr(settings, "cors_origins", "")
    monkeypatch.setattr(settings, "api_port", 8010)

    server = create_socket_server()

    assert server.eio.cors_allowed_origins == ["http://localhost:8010", "http://127.0.0.1:8010"]


def test_socket_server_has_no_process_memory_manager_fallback(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", "")

    with pytest.raises(RedisUnavailableError, match="redis_unavailable"):
        create_socket_server()
