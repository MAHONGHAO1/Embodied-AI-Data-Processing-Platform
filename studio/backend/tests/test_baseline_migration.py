"""Baseline migration must create a schema equivalent to source head."""

from sqlalchemy import inspect

from data.database import Base, engine


def test_baseline_creates_every_mapped_table():
    """All ORM-mapped tables must exist in the migrated database."""
    existing = set(inspect(engine).get_table_names())
    mapped = set(Base.metadata.tables)
    assert mapped - existing == set()


def test_functional_unique_indexes_exist():
    """autogenerate does not produce functional indexes, they must be manually completed."""
    inspector = inspect(engine)
    workspace_indexes = {index["name"] for index in inspector.get_indexes("workspaces")}
    assert "uq_workspaces_normalized_name" in workspace_indexes
    task_set_indexes = {index["name"] for index in inspector.get_indexes("task_sets")}
    assert "uq_task_sets_workspace_normalized_name" in task_set_indexes
    device_indexes = {index["name"] for index in inspector.get_indexes("collection_devices")}
    assert "uq_collection_devices_workspace_normalized_serial" in device_indexes
