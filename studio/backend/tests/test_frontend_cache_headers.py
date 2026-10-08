import pytest


def test_frontend_entrypoint_and_scripts_are_revalidated(client):
    index = client.get("/")
    script = client.get("/js/app.js")

    assert index.status_code == 200
    assert index.headers["cache-control"] == "no-cache"
    assert script.status_code == 200
    assert script.headers["cache-control"] == "no-cache"


def test_only_static_text_assets_use_gzip_compression(client, monkeypatch):
    from data.infra import storage_provider
    from data.services import task_dispatcher

    monkeypatch.setattr(
        task_dispatcher, "worker_status_snapshot", lambda: {"available": True, "worker_count": 1}
    )
    monkeypatch.setattr(
        storage_provider,
        "get_storage_provider",
        lambda: type("ReadyProvider", (), {"healthcheck": lambda self: None})(),
    )
    script = client.get("/js/app.js", headers={"Accept-Encoding": "gzip"})
    css = client.get("/css/app.css", headers={"Accept-Encoding": "gzip"})
    vendor = client.get(
        "/vendor/qrcode-generator/1.5.0/qrcode.js",
        headers={"Accept-Encoding": "gzip"},
    )
    health = client.get("/health", headers={"Accept-Encoding": "gzip"})

    assert script.status_code == 200
    assert script.headers["content-encoding"] == "gzip"
    assert "Accept-Encoding" in script.headers["vary"]
    assert script.headers["cache-control"] == "no-cache"
    assert css.status_code == 200
    assert css.headers["content-encoding"] == "gzip"
    assert vendor.status_code == 200
    assert vendor.headers["content-encoding"] == "gzip"
    assert vendor.headers["cache-control"] == "no-cache"
    assert health.status_code == 200
    assert "content-encoding" not in health.headers


def test_health_returns_503_when_worker_is_missing(client, monkeypatch):
    from data.services import task_dispatcher

    monkeypatch.setattr(
        task_dispatcher, "worker_status_snapshot", lambda: {"available": False, "worker_count": 0}
    )
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["infra"]["celery"] is False


def test_health_returns_503_when_storage_is_down(client, monkeypatch):
    from data.infra import storage_provider

    class DownProvider:
        def healthcheck(self):
            from data.infra.object_storage import StorageNotReady

            raise StorageNotReady("test storage down")

    monkeypatch.setattr(storage_provider, "get_storage_provider", lambda: DownProvider())
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["infra"]["storage"]["ready"] is False


@pytest.mark.parametrize(
    "path",
    (
        "/%2e%2e/README.md",
        "/..%2fREADME.md",
        "/%252e%252e/README.md",
    ),
)
def test_frontend_fallback_rejects_paths_outside_frontend_root(client, path):
    response = client.get(path)

    assert response.status_code == 404
    assert "QuicData is the robotics data platform" not in response.text
