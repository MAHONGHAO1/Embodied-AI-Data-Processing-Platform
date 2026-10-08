from sqlalchemy import UniqueConstraint

from data.database import Base, Batch, Episode, ImportSession, JobRun, User, WorkItem, Workspace


def test_users_have_a_realtime_session_epoch():
    assert "realtime_session_epoch" in User.__table__.c
    assert User.__table__.c.realtime_session_epoch.nullable is False


def test_realtime_resources_have_monotonic_versions():
    for model in (Workspace, Batch, ImportSession, Episode, WorkItem, JobRun):
        assert "realtime_version" in model.__table__.c
        assert model.__table__.c.realtime_version.nullable is False


def test_realtime_outbox_is_unique_per_resource_version():
    table = Base.metadata.tables.get("realtime_events")

    assert table is not None
    assert {
        "event_id",
        "resource_type",
        "resource_id",
        "resource_version",
        "event_name",
        "safe_payload",
        "created_at",
        "published_at",
        "attempt_count",
        "last_error",
    }.issubset(table.c.keys())
    assert any(
        set(constraint.columns.keys()) == {"resource_type", "resource_id", "resource_version"}
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    )
