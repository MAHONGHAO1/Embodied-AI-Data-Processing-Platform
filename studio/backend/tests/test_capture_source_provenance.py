"""Tests for fail-soft, workspace-scoped capture provenance projections."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from collector_fixtures import make_collector
from sqlalchemy import text

from data.database import CollectionDevice, Workspace
from data.services.capture_provenance import project_capture_provenance

_QRDF_SOURCE_GROUP_VECTORS = (
    Path(__file__).parent / "fixtures" / "contracts" / "qrdf_source_group_vectors.json"
)
_SOURCE_GROUP_VECTORS = json.loads(_QRDF_SOURCE_GROUP_VECTORS.read_text(encoding="utf-8"))


def _source_group_key(name: str) -> str:
    return f"sg1_{hashlib.sha256(name.encode('utf-8')).hexdigest()}"


def _workspace(db_session, *, name: str | None = None) -> Workspace:
    workspace = Workspace(name=name or f"capture provenance {uuid4().hex}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    return workspace


@pytest.mark.parametrize("vector", _SOURCE_GROUP_VECTORS, ids=lambda vector: vector["description"])
def test_projection_uses_the_qrdf_source_group_contract(db_session, vector):
    """The fail-soft platform projection must not drift from QRDF's writer contract."""
    workspace = _workspace(db_session)
    name = vector["name"]
    projection = project_capture_provenance(
        db_session,
        workspace_id=workspace.id,
        raw_metadata={
            "source_group": {
                "name": name,
                "key": vector.get("key", "sg1_invalid"),
            }
        },
    )

    if vector.get("valid") == "false":
        assert projection.source_group_key is None
        assert projection.source_group_name is None
        assert projection.source_group_status == "invalid"
        return

    assert projection.source_group_key == vector["key"]
    assert projection.source_group_name == vector["normalized_name"]
    assert projection.source_group_status == "valid"


def test_projection_accepts_verified_source_group_and_workspace_local_resources(db_session):
    workspace = _workspace(db_session)
    collector = make_collector(
        db_session,
        workspace_id=workspace.id,
        name="Collector",
        is_active=False,
    )
    device = CollectionDevice(
        workspace_id=workspace.id,
        name="Capture phone",
        device_type="phone",
        model="iPhone 15",
        serial_number="SN-42",
        is_active=False,
    )
    db_session.add_all((collector, device))
    db_session.flush()

    projection = project_capture_provenance(
        db_session,
        workspace_id=workspace.id,
        raw_metadata={
            "source_group": {
                "name": "  2026   Morning  ",
                "key": _source_group_key("2026 Morning"),
            },
            "operator": {"id": collector.profile_key},
            "devices": [{"serial_number": " sn-42 "}],
        },
    )

    assert projection.source_group_key == _source_group_key("2026 Morning")
    assert projection.source_group_name == "2026 Morning"
    assert projection.source_group_status == "valid"
    assert projection.collector_identifier == collector.profile_key
    assert projection.collector_profile_id == collector.id
    assert projection.collector_match_status == "matched"
    assert projection.device_serial == "sn-42"
    assert projection.collection_device_id == device.id
    assert projection.device_match_status == "matched"


def test_projection_downgrades_invalid_hints_without_rejecting_core_metadata(db_session):
    workspace = _workspace(db_session)

    projection = project_capture_provenance(
        db_session,
        workspace_id=workspace.id,
        raw_metadata={
            "source_group": {"name": "trusted", "key": _source_group_key("another")},
            "operator": {"id": "bad\nidentifier"},
            "devices": [{"serial_number": "\x1cSN-1"}],
        },
    )

    assert projection.source_group_key is None
    assert projection.source_group_name is None
    assert projection.source_group_status == "invalid"
    assert projection.collector_identifier is None
    assert projection.collector_profile_id is None
    assert projection.collector_match_status == "unmatched"
    assert projection.device_serial is None
    assert projection.collection_device_id is None
    assert projection.device_match_status == "unmatched"


def test_projection_never_matches_collector_or_device_from_another_workspace(db_session):
    workspace = _workspace(db_session)
    foreign_workspace = _workspace(db_session)
    foreign_collector = make_collector(
        db_session,
        workspace_id=foreign_workspace.id,
        name="Foreign collector",
    )
    foreign_device = CollectionDevice(
        workspace_id=foreign_workspace.id,
        name="Foreign phone",
        device_type="phone",
        serial_number="SN-42",
    )
    db_session.add_all((foreign_collector, foreign_device))
    db_session.flush()

    projection = project_capture_provenance(
        db_session,
        workspace_id=workspace.id,
        raw_metadata={
            "operator": {"id": foreign_collector.profile_key},
            "devices": [{"serial_number": "SN-42"}],
        },
    )

    assert projection.collector_identifier == foreign_collector.profile_key
    assert projection.collector_profile_id is None
    assert projection.collector_match_status == "unmatched"
    assert projection.device_serial == "SN-42"
    assert projection.collection_device_id is None
    assert projection.device_match_status == "unmatched"


def test_projection_uses_legacy_collection_task_as_bounded_source_group_fallback(db_session):
    workspace = _workspace(db_session)

    projection = project_capture_provenance(
        db_session,
        workspace_id=workspace.id,
        raw_metadata={"collection_task_id": "  Line A / Day 1  "},
    )

    assert projection.source_group_name == "Line A / Day 1"
    assert (
        projection.source_group_key == f"legacy_sg1_{hashlib.sha256(b'Line A / Day 1').hexdigest()}"
    )
    assert projection.source_group_status == "legacy"
    assert projection.collector_match_status == "unknown"
    assert projection.device_match_status == "unknown"


def test_projection_marks_an_invalid_legacy_collection_task_as_invalid(db_session):
    workspace = _workspace(db_session)

    projection = project_capture_provenance(
        db_session,
        workspace_id=workspace.id,
        raw_metadata={"collection_task_id": "\x1cnot-safe"},
    )

    assert projection.source_group_key is None
    assert projection.source_group_name is None
    assert projection.source_group_status == "invalid"


def test_projection_treats_absent_optional_hints_as_unknown(db_session):
    workspace = _workspace(db_session)

    projection = project_capture_provenance(db_session, workspace_id=workspace.id, raw_metadata={})

    assert projection.source_group_status == "missing"
    assert projection.collector_match_status == "unknown"
    assert projection.device_match_status == "unknown"


@pytest.mark.parametrize("unknown_value", ["unknown", "UNKNOWN"])
def test_projection_treats_unknown_collector_marker_as_unreported(db_session, unknown_value):
    workspace = _workspace(db_session)

    projection = project_capture_provenance(
        db_session,
        workspace_id=workspace.id,
        raw_metadata={"operator": {"id": unknown_value}},
    )

    assert projection.collector_reported_identifier is None
    assert projection.collector_identifier is None
    assert projection.collector_hint_status == "missing"
    assert projection.collector_match_status == "unknown"


@pytest.mark.skip(
    reason="Inherited rolling-deploy default check depends on quicdata revision 0037, obsolete after Task 2 squash to 0001_baseline"
)
def test_capture_provenance_columns_keep_database_defaults_during_rolling_deploy(db_session):
    """Older API/worker instances can omit additive columns until rollout completes."""
    rows = db_session.execute(
        text(
            """
            SELECT table_name, column_name, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND (
                (table_name = 'import_sessions' AND column_name = 'scan_cursor_json')
                OR (table_name = 'import_candidates' AND column_name = 'source_group_status')
                OR (table_name = 'episodes' AND column_name = 'reported_source_group_status')
                OR (table_name = 'episode_collector_attributions' AND column_name = 'match_status')
                OR (table_name = 'episode_device_attributions' AND column_name = 'match_status')
              )
            """
        )
    ).all()

    defaults = {(str(table), str(column)): default for table, column, default in rows}
    assert set(defaults) == {
        ("import_sessions", "scan_cursor_json"),
        ("import_candidates", "source_group_status"),
        ("episodes", "reported_source_group_status"),
        ("episode_collector_attributions", "match_status"),
        ("episode_device_attributions", "match_status"),
    }
    assert all(value is not None for value in defaults.values())
