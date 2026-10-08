"""Shared DB rebind helper for isolated SQLite tests."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import quictrain_api.db as db
from quictrain_api.settings import get_settings


def rebind_database() -> None:
    get_settings.cache_clear()
    settings = get_settings()
    connect_args = (
        {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
    )
    db.engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)
    db.SessionLocal = sessionmaker(bind=db.engine, expire_on_commit=False, autoflush=False)
    db.settings = settings
    if settings.env != "production" and settings.database_url.startswith("sqlite"):
        db.Base.metadata.create_all(db.engine)
        db._ensure_sqlite_dev_columns(db.engine)
    try:
        import quictrain_api.main as main_mod

        main_mod.settings = settings
        main_mod.SessionLocal = db.SessionLocal
    except Exception:
        pass
