"""Fast Atom ROS2 MCAP reading (rosbags AnyReader + topic filtering, aligned with droid_dataset)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from data.integrations.qrdf.rosbags_mcap_patch import apply_mcap_schema_encoding_patch

_rosbags_ready = False


def _ensure_rosbags() -> None:
    global _rosbags_ready
    if not _rosbags_ready:
        apply_mcap_schema_encoding_patch()
        _rosbags_ready = True


def rosbags_available() -> bool:
    try:
        import rosbags  # noqa: F401

        return True
    except ImportError:
        return False


def iter_atom_mcap_messages(
    mcap_path: str | Path,
    topics: list[str],
) -> Iterator[tuple[str, int, Any]]:
    """Iterate deserialized messages for specified topics in chronological order.

    Yields:
        (topic, timestamp_ns, deserialized_msg)
    """
    _ensure_rosbags()
    from rosbags.highlevel import AnyReader

    mcap_path = Path(mcap_path)
    topic_set = set(topics)
    with AnyReader([mcap_path]) as reader:
        conns = [c for c in reader.connections if c.topic in topic_set]
        if not conns:
            raise RuntimeError(f"MCAP 中未找到 topic: {sorted(topic_set)}")
        for conn, ts, raw in reader.messages(connections=conns):
            yield conn.topic, int(ts), reader.deserialize(raw, conn.msgtype)
