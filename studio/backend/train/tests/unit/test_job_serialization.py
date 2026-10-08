from types import SimpleNamespace

from quictrain_api.db import UserRecord
from quictrain_api.service import _resolve_creator_display_name


class RecordingSession:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def get(self, model, identifier):
        self.calls.append((model, identifier))
        return self.records.get(model)


def test_creator_name_uses_quicstudio_user_schema_in_studio_mode(monkeypatch):
    from data.database import User as StudioUser

    monkeypatch.setattr(
        "quictrain_api.service.get_settings", lambda: SimpleNamespace(auth_mode="studio")
    )
    session = RecordingSession({StudioUser: SimpleNamespace(display_name="admin")})

    assert _resolve_creator_display_name(session, 1) == "admin"
    assert session.calls == [(StudioUser, 1)]


def test_creator_name_uses_quictrain_user_schema_outside_studio_mode(monkeypatch):
    monkeypatch.setattr(
        "quictrain_api.service.get_settings", lambda: SimpleNamespace(auth_mode="local")
    )
    session = RecordingSession({UserRecord: SimpleNamespace(display_name="Admin")})

    assert _resolve_creator_display_name(session, 1) == "Admin"
    assert session.calls == [(UserRecord, "1")]
