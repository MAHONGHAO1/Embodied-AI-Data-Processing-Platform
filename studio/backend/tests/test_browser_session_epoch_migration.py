from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url

pytestmark = pytest.mark.skip(
    reason="Inherited quicdata revision-chain tests are obsolete after Task 2 squash to 0001_baseline"
)

ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_REVISION = "0027_workspace_membership"
TARGET_REVISION = "0028_browser_session_epoch"


def _alembic_config(database_url: URL) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend" / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url.render_as_string(hide_password=False))
    return config


def _migration_database_url() -> URL:
    base_url = make_url(os.environ["TEST_DATABASE_URL"])
    database_name = f"{base_url.database}_browser_session_epoch_migration"
    if not re.fullmatch(r"quicdata_test[a-z0-9_]*", database_name):
        raise RuntimeError("migration test database name is not approved")
    return base_url.set(database=database_name)


def _reset_database(database_url: URL) -> None:
    engine = create_engine(database_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database_url.database}" WITH (FORCE)')
            )
            connection.execute(text(f'CREATE DATABASE "{database_url.database}"'))
    finally:
        engine.dispose()


def _drop_database(database_url: URL) -> None:
    engine = create_engine(database_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database_url.database}" WITH (FORCE)')
            )
    finally:
        engine.dispose()


def test_browser_session_epoch_upgrade_and_downgrade_preserve_users():
    database_url = _migration_database_url()
    _reset_database(database_url)
    try:
        config = _alembic_config(database_url)
        command.upgrade(config, PREVIOUS_REVISION)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO users "
                        "(id, email, password_hash, role, is_active, created_at) "
                        "VALUES (1, 'session@example.com', 'hash', 'viewer', true, CURRENT_TIMESTAMP)"
                    )
                )

            command.upgrade(config, TARGET_REVISION)
            assert "browser_session_epoch" in {
                column["name"] for column in inspect(engine).get_columns("users")
            }
            with engine.connect() as connection:
                assert (
                    connection.execute(
                        text("SELECT browser_session_epoch FROM users WHERE id = 1")
                    ).scalar_one()
                    == 0
                )

            command.downgrade(config, PREVIOUS_REVISION)
            assert "browser_session_epoch" not in {
                column["name"] for column in inspect(engine).get_columns("users")
            }
            with engine.connect() as connection:
                assert connection.execute(text("SELECT count(*) FROM users")).scalar_one() == 1
        finally:
            engine.dispose()
    finally:
        _drop_database(database_url)
