"""Runtime database revision checks shared by API, workers, and CLI tools."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from data.config import settings

ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = ROOT / "alembic.ini"


def alembic_config(database_url: str | None = None) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ROOT / "backend" / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url or settings.database_url)
    return config


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision() -> str | None:
    from data.database import engine

    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        return context.get_current_revision()


def assert_schema_current() -> None:
    current = current_revision()
    head = head_revision()
    if current != head:
        raise RuntimeError(
            "database revision is not at Alembic head; run make db-upgrade or make prod-migrate "
            f"(current={current or 'none'}, head={head})"
        )


def upgrade_database(database_url: str | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)
