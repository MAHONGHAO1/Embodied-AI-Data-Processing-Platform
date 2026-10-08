"""Real control-plane startup must not invent dataset versions."""

import pytest
from fastapi.testclient import TestClient
from quictrain_model_specs import MODEL_REGISTRY
from sqlalchemy import select
from tests.db_helpers import rebind_database

import quictrain_api.db as db
from quictrain_api.service import SEED_DATASETS, seed_catalog
from quictrain_api.settings import Settings


def configure_database(tmp_path, monkeypatch, *, environment="production", examples=False):
    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/catalog.db")
    monkeypatch.setenv("QUICTRAIN_ENV", environment)
    monkeypatch.setenv("QUICTRAIN_SEED_EXAMPLE_DATASETS", str(examples).lower())
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    monkeypatch.setenv("QUICTRAIN_AUTH_MODE", "open")
    rebind_database()


def test_example_seed_setting_defaults_off(monkeypatch):
    monkeypatch.delenv("QUICTRAIN_SEED_EXAMPLE_DATASETS", raising=False)
    assert Settings(_env_file=None).seed_example_datasets is False


@pytest.mark.parametrize("environment", ["development", "production"])
def test_api_startup_keeps_real_dataset_catalog_empty(tmp_path, monkeypatch, environment):
    configure_database(tmp_path, monkeypatch, environment=environment)
    from quictrain_api.main import app

    with TestClient(app) as client:
        assert client.get("/api/v1/datasets").json()["items"] == []
        assert {item["id"] for item in client.get("/api/v1/models").json()["items"]} == {
            "act",
            "pi05",
        }
    with db.SessionLocal() as session:
        assert {row.id for row in session.scalars(select(db.ModelVersionRecord))} == {
            model.version_id for model in MODEL_REGISTRY.values()
        }
        assert session.scalars(select(db.ResourceProfileRecord)).first() is not None


@pytest.mark.parametrize("environment", ["production", "uat", "staging"])
def test_example_opt_in_cannot_seed_real_environment(tmp_path, monkeypatch, environment):
    configure_database(tmp_path, monkeypatch, environment=environment, examples=True)
    db.init_database()
    with db.SessionLocal() as session:
        seed_catalog(session)
        assert session.scalars(select(db.DatasetVersionRecord)).all() == []
        assert session.scalars(select(db.ModelVersionRecord)).first() is not None


@pytest.mark.parametrize("environment", ["development", "test"])
def test_explicit_example_seed_is_idempotent_and_disabled_startup_preserves_rows(
    tmp_path, monkeypatch, environment
):
    configure_database(tmp_path, monkeypatch, environment=environment, examples=True)
    db.init_database()
    with db.SessionLocal() as session:
        seed_catalog(session)
        seed_catalog(session)
        before = {row.id: row.manifest for row in session.scalars(select(db.DatasetVersionRecord))}
        assert set(before) == {dataset.id for dataset in SEED_DATASETS}
    configure_database(tmp_path, monkeypatch)
    with db.SessionLocal() as session:
        seed_catalog(session)
        assert {
            row.id: row.manifest for row in session.scalars(select(db.DatasetVersionRecord))
        } == before
