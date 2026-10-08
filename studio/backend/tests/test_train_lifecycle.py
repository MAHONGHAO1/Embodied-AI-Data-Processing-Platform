from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select


def test_train_starts_after_the_main_schema_check(monkeypatch):
    import data.main as main
    import train.mount as train_mount

    events = []
    monkeypatch.setattr(main.runtime, "assert_schema_current", lambda: events.append("schema"))
    monkeypatch.setattr(main.redis_service, "connect_required", lambda: None)
    monkeypatch.setattr(main, "bind_realtime_event_loop", lambda: None)
    monkeypatch.setattr(main, "_train_mounted", True)
    monkeypatch.setattr(train_mount, "startup_train", lambda: events.append("train-start"))
    monkeypatch.setattr(train_mount, "shutdown_train", lambda: events.append("train-stop"))

    with TestClient(main.http_app):
        assert events == ["schema", "train-start"]

    assert events == ["schema", "train-start", "train-stop"]


def test_train_job_creator_uses_the_studio_users_schema(monkeypatch):
    from data.database import SessionLocal as StudioSessionLocal
    from data.database import User
    from quictrain_api import service
    from quictrain_api.db import SessionLocal as TrainSessionLocal

    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(auth_mode="studio"))
    with StudioSessionLocal() as studio_session:
        user = studio_session.scalar(select(User).where(User.email == "admin@quicdata.com"))
    assert user is not None

    with TrainSessionLocal() as train_session:
        assert service._resolve_creator_display_name(train_session, user.id) == user.display_name
