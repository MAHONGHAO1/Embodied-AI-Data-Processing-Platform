from __future__ import annotations

from pathlib import Path
import sys

import pytest

pytest.importorskip("mcap")
pytest.importorskip("mcap_ros2")

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = ROOT / "examples" / "ros2_mcap"
sys.path.insert(0, str(EXAMPLE_DIR))

from make_fixture import TOPIC, write_fixture
from sensor_slice import extract_scalar_series


def test_synthetic_ros2_mcap_decodes_single_topic_and_provenance(tmp_path):
    source = write_fixture(tmp_path / "sensor.mcap", count=5, interval_ns=50_000_000)
    result = extract_scalar_series(source, TOPIC)

    assert result["status"] == "passed"
    assert result["sample_count"] == 5
    assert result["message_type"] == "std_msgs/msg/Float64"
    assert result["timestamp_semantics"].startswith("MCAP log_time")
    assert [item["value"] for item in result["samples"]] == [0, 1, 2, 3, 4]
    assert result["summary"]["median_positive_interval_ns"] == 50_000_000
    assert len(result["source"]["sha256"]) == 64


def test_reversed_timestamp_is_reported_without_adapter_reordering(tmp_path):
    source = write_fixture(tmp_path / "reversed.mcap", count=5,
                           interval_ns=50_000_000, reverse_at=2)
    result = extract_scalar_series(source, TOPIC)

    assert result["status"] == "issues"
    assert result["summary"]["non_increasing_sample_indices"] == [2]
    assert [item["value"] for item in result["samples"]] == [0, 1, 2, 3, 4]
    assert sum(item["rule"] == "timestamp_strictly_increasing" for item in result["issues"]) == 1


def test_large_timestamp_interval_is_reported_as_gap(tmp_path):
    source = write_fixture(tmp_path / "gap.mcap", count=5,
                           interval_ns=50_000_000, gap_at=2)
    result = extract_scalar_series(source, TOPIC)

    assert result["status"] == "issues"
    gap = next(item for item in result["issues"] if item["rule"] == "timestamp_gap")
    assert gap["sample_indices"] == [2]
    assert result["summary"]["non_increasing_sample_indices"] == []


def test_non_finite_sensor_value_is_rejected(tmp_path):
    source = write_fixture(tmp_path / "non-finite.mcap", count=3, non_finite_at=1)
    with pytest.raises(ValueError, match="finite number"):
        extract_scalar_series(source, TOPIC)


def test_missing_topic_is_not_reported_as_empty_success(tmp_path):
    source = write_fixture(tmp_path / "sensor.mcap", count=2)
    with pytest.raises(ValueError, match="no decodable messages"):
        extract_scalar_series(source, "/missing/topic")


def test_relative_topic_is_rejected_before_reading(tmp_path):
    with pytest.raises(ValueError, match="absolute ROS topic"):
        extract_scalar_series(tmp_path / "does-not-exist.mcap", "sensor/value")
