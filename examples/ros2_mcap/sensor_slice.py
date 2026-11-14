"""Minimal ROS 2 MCAP scalar sensor slice; deliberately separate from Episode."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any

from mcap.reader import make_reader
from mcap_ros2.decoder import DecoderFactory


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_scalar_series(path: Path, topic: str) -> dict[str, Any]:
    """Decode one std_msgs/msg/Float64 ROS 2 topic in capture order.

    Timestamps use MCAP log_time in integer nanoseconds. Messages are read in
    physical file order (log_time_order=False); duplicate or reversed timestamps
    are reported rather than silently reordered by this adapter.
    """
    path = Path(path)
    if not topic.startswith("/"):
        raise ValueError("topic must be an absolute ROS topic name starting with '/'")
    if not path.is_file():
        raise FileNotFoundError(path)

    samples: list[dict[str, int | float]] = []
    schema_name: str | None = None
    with path.open("rb") as stream:
        reader = make_reader(stream, decoder_factories=[DecoderFactory()])
        for item in reader.iter_decoded_messages(topics=[topic], log_time_order=False):
            current_schema = item.schema.name if item.schema else None
            if current_schema != "std_msgs/msg/Float64":
                raise ValueError(
                    f"unsupported schema on {topic}: {current_schema!r}; expected std_msgs/msg/Float64"
                )
            if item.channel.message_encoding != "cdr":
                raise ValueError(f"unsupported message encoding: {item.channel.message_encoding!r}")
            schema_name = current_schema
            timestamp_ns = item.message.log_time
            value = item.decoded_message.data
            if isinstance(timestamp_ns, bool) or not isinstance(timestamp_ns, int) or timestamp_ns < 0:
                raise ValueError("MCAP log_time must be a non-negative integer nanosecond timestamp")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("decoded scalar must be a finite number")
            samples.append({"timestamp_ns": timestamp_ns, "value": float(value)})

    if not samples:
        raise ValueError(f"no decodable messages found for topic {topic!r}")

    deltas = [samples[index]["timestamp_ns"] - samples[index - 1]["timestamp_ns"]
              for index in range(1, len(samples))]
    non_increasing = [index for index, delta in enumerate(deltas, start=1) if delta <= 0]
    positive_deltas = [delta for delta in deltas if delta > 0]
    median_delta = statistics.median(positive_deltas) if positive_deltas else None
    gap_indices = ([index for index, delta in enumerate(deltas, start=1)
                    if median_delta is not None and delta > median_delta * 1.5]
                   if len(positive_deltas) >= 2 else [])
    issues = []
    if non_increasing:
        issues.append({"rule": "timestamp_strictly_increasing", "sample_indices": non_increasing,
                       "message": "Capture-order log_time must be strictly increasing."})
    if gap_indices:
        issues.append({"rule": "timestamp_gap", "sample_indices": gap_indices,
                       "message": "Timestamp interval exceeds 1.5x the median positive interval."})

    return {
        "schema_version": "ros2-scalar-slice-v1",
        "source": {"file": path.name, "sha256": sha256_file(path)},
        "topic": topic,
        "message_type": schema_name,
        "timestamp_semantics": "MCAP log_time, integer nanoseconds; not hardware synchronization proof",
        "sample_count": len(samples),
        "summary": {
            "first_timestamp_ns": samples[0]["timestamp_ns"],
            "last_timestamp_ns": samples[-1]["timestamp_ns"],
            "median_positive_interval_ns": median_delta,
            "non_increasing_sample_indices": non_increasing,
            "gap_sample_indices": gap_indices,
        },
        "status": "issues" if issues else "passed",
        "issues": issues,
        "samples": samples,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mcap", type=Path, help="Input ROS 2 MCAP file")
    parser.add_argument("--topic", required=True, help="Exact topic, e.g. /sensor/value")
    parser.add_argument("--output", type=Path, help="Write JSON report to this path; default is stdout")
    args = parser.parse_args()
    report = extract_scalar_series(args.mcap, args.topic)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    if report["status"] != "passed":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
