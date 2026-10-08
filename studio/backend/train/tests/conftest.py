"""Pytest configuration and isolation helpers."""

from __future__ import annotations

import os

os.environ.setdefault("QUICTRAIN_AUTH_MODE", "open")

import pytest
from tests.db_helpers import rebind_database


@pytest.fixture
def example_dataset_seed(monkeypatch):
    """Opt in only tests whose scenarios use the unverified example datasets."""
    monkeypatch.setenv("QUICTRAIN_ENV", "test")
    monkeypatch.setenv("QUICTRAIN_SEED_EXAMPLE_DATASETS", "true")
    rebind_database()


@pytest.fixture(autouse=True)
def _restore_default_database_after_test():
    """Prevent isolated-DB tests from poisoning later integration tests."""

    yield
    rebind_database()
