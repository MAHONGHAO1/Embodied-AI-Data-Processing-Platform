"""MCAP adapter is inspection-only until a real mapping/sample exists."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

from robodata.adapters import REGISTRY, UnsupportedSource, get_adapter

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/ros2_mcap"
sys.path.insert(0, str(EXAMPLE))


def test_mcap_without_optional_dependency_reports_install_hint(tmp_path):
    if importlib.util.find_spec("mcap") is not None:
        pytest.skip("mcap optional dependency is installed")
    path = tmp_path / "sensor.mcap"
    path.write_bytes(b"not a real mcap")
    result = get_adapter("mcap").detect(path)
    assert result.supported is False
    assert "mcap" in result.unsupported_reason


def test_mcap_topics_and_profile_validation(tmp_path):
    pytest.importorskip("mcap")
    from make_fixture import TOPIC, write_fixture
    path = write_fixture(tmp_path / "sensor.mcap", count=5)
    adapter = get_adapter("mcap")
    result = adapter.detect(path)
    assert result.supported is True
    assert result.detected_version == "mcap-inspection-v0"
    assert any(item["topic"] == TOPIC for item in result.evidence["topics"])
    profile = tmp_path / "sensor.robodata.json"
    profile.write_text('{"schema_version": 1, "profile": "demo", '
                       '"state": {"topic": "/missing/state"}, '
                       '"action": {"topic": "/missing/action"}, "cameras": []}', encoding="utf-8")
    preflight = adapter.preflight(path)
    assert preflight.supported is True
    assert preflight.summary["profile_missing_topics"]


def test_mcap_is_skipped_by_auto_import_and_rejects_prepare(tmp_path):
    pytest.importorskip("mcap")
    from make_fixture import write_fixture
    path = write_fixture(tmp_path / "sensor.mcap", count=2)
    with pytest.raises(UnsupportedSource, match="仅检查"):
        REGISTRY.best(path)
    with pytest.raises(ValueError, match="只支持检查"):
        get_adapter("mcap").prepare({"input_path": str(path)}, tmp_path / "raw", tmp_path / "managed")
