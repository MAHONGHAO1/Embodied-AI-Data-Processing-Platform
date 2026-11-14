"""Generate an explicitly synthetic ROS 2 MCAP fixture for the example/tests."""
from __future__ import annotations

import argparse
import struct
from pathlib import Path

from mcap.writer import Writer

SCHEMA = b"float64 data\n"
TOPIC = "/sensor/value"


def write_fixture(path: Path, *, count: int = 5, interval_ns: int = 50_000_000,
                  duplicate_at: int | None = None, reverse_at: int | None = None,
                  gap_at: int | None = None, non_finite_at: int | None = None) -> Path:
    if count < 1 or interval_ns < 1:
        raise ValueError("count and interval_ns must be positive")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        writer = Writer(stream)
        writer.start(profile="ros2", library="robodata-synthetic-fixture/1")
        schema_id = writer.register_schema("std_msgs/msg/Float64", "ros2msg", SCHEMA)
        channel_id = writer.register_channel(TOPIC, "cdr", schema_id)
        for index in range(count):
            timestamp_ns = index * interval_ns
            if duplicate_at == index:
                timestamp_ns -= interval_ns
            if reverse_at == index:
                timestamp_ns = max(0, timestamp_ns - interval_ns * 2)
            if gap_at is not None and index >= gap_at:
                timestamp_ns += interval_ns
            value = float(index)
            if non_finite_at == index:
                value = float("nan")
            cdr = b"\x00\x01\x00\x00" + struct.pack("<d", value)
            writer.add_message(channel_id, timestamp_ns, cdr, timestamp_ns, sequence=index)
        writer.finish()
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--interval-ns", type=int, default=50_000_000)
    args = parser.parse_args()
    print(write_fixture(args.output, count=args.count, interval_ns=args.interval_ns))


if __name__ == "__main__":
    main()
