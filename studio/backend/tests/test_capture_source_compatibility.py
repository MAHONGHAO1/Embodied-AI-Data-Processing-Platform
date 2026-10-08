"""Pinned compatibility coverage for historical and V2 capture metadata."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from qrdf.models.episode import EpisodeMetadata

from data.services.capture_batch_import import (
    _capture_provenance_metadata,
    _capture_source_path,
    _core_capture_metadata_payload,
)
from data.services.historical_ego_import import _legacy_ego_path

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "capture_sources"


@pytest.mark.parametrize(
    ("fixture_name", "object_key", "reader", "expected_group"),
    (
        (
            "v1_legacy",
            "incoming/raw/v1/tasks/fixture-v1/devices/device-legacy/episodes/fixture-v1-legacy-episode/metadata.json",
            "legacy",
            None,
        ),
        (
            "ego_v2_legacy",
            "incoming/raw/v1/tasks/fixture-v2/devices/device-legacy/episodes/fixture-ego-v2-legacy-episode/metadata.json",
            "legacy",
            "Fixture Legacy Group",
        ),
        (
            "generic_v2_flat",
            "incoming/raw/v2/sources/fixture-generic-flat-episode/metadata.json",
            "generic",
            "Fixture Flat Group",
        ),
        (
            "generic_v2_partitioned",
            "incoming/raw/v2/sources/date=2026-08-25/hour=03/fixture-generic-partitioned-episode/metadata.json",
            "generic",
            "Fixture Partitioned Group",
        ),
    ),
)
def test_platform_readers_accept_pinned_historical_and_v2_metadata(
    fixture_name: str,
    object_key: str,
    reader: str,
    expected_group: str | None,
):
    payload = json.loads(
        (_FIXTURE_ROOT / fixture_name / "metadata.json").read_text(encoding="utf-8")
    )

    if reader == "legacy":
        path = _legacy_ego_path(object_key, require_known_file=True)
        core_payload = payload
    else:
        path = _capture_source_path(object_key, require_known_file=True)
        core_payload = _core_capture_metadata_payload(payload)

    assert path is not None
    metadata = EpisodeMetadata.model_validate(core_payload)
    metadata.check_version_compatible()
    assert metadata.episode_id == path.episode_id
    assert metadata.data_file == "data.mcap"

    provenance = _capture_provenance_metadata(payload)
    if expected_group is None:
        assert provenance == {}
    elif reader == "legacy":
        assert provenance == {"collection_task_id": expected_group}
    else:
        assert provenance["source_group"]["name"] == expected_group
        assert provenance["operator"]["id"].isdigit()
        assert provenance["devices"][0]["serial_number"].startswith("FIXTURE-SN-")
