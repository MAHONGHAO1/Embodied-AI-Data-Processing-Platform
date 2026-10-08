"""MCAP read/write helpers (without modifying vendor/qrdf)."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

from mcap.reader import McapReader, make_reader
from qrdf.exceptions import McapCorruptedError, McapSchemaError
from qrdf.mcap.reader import McapEpisodeReader, McapMessage
from qrdf.mcap.writer import McapEpisodeWriter


class IndexedMcapMessageReader:
    """Decode bounded MCAP ranges through a single cached summary/index."""

    def __init__(
        self,
        reader: McapReader,
        decode_record: Callable[[object, object, object], McapMessage],
    ) -> None:
        self._reader = reader
        self._decode_record = decode_record

    def iter_messages(
        self,
        *,
        topics: set[str] | None,
        start_ns: int | None,
        end_ns: int | None,
        reverse: bool = False,
    ) -> Iterator[McapMessage]:
        try:
            for schema, channel, record in self._reader.iter_messages(
                topics=topics,
                start_time=start_ns,
                end_time=end_ns,
                log_time_order=True,
                reverse=reverse,
            ):
                yield self._decode_record(schema, channel, record)
        except (McapCorruptedError, McapSchemaError):
            raise
        except Exception as exc:
            raise McapCorruptedError(f"Failed to read indexed MCAP range: {exc}") from exc


@contextmanager
def open_indexed_mcap_reader(
    episode_reader: McapEpisodeReader,
) -> Iterator[IndexedMcapMessageReader | None]:
    """Open an indexed range reader, or yield ``None`` for explicit fallback.

    The deployed QRDF reader intentionally exposes only full streaming passes.
    This adapter keeps QRDF's canonical schema decoder while using MCAP's
    seeking reader when a summary with chunk indexes is available. The file is
    kept open so calibration and interval queries reuse the same parsed summary.
    """

    path = getattr(episode_reader, "path", None)
    decode_record = getattr(episode_reader, "_decode_record", None)
    if path is None or not callable(decode_record):
        yield None
        return

    file_obj = None
    try:
        file_obj = Path(path).open("rb")
        raw_reader = make_reader(file_obj)
        summary = raw_reader.get_summary()
    except (McapCorruptedError, McapSchemaError):
        if file_obj is not None:
            file_obj.close()
        raise
    except Exception as exc:
        if file_obj is not None:
            file_obj.close()
        raise McapCorruptedError(f"Failed to inspect MCAP index: {exc}") from exc

    if summary is None or not summary.chunk_indexes:
        file_obj.close()
        yield None
        return

    try:
        yield IndexedMcapMessageReader(raw_reader, decode_record)
    finally:
        file_obj.close()


def collect_messages_by_topic(reader: McapEpisodeReader) -> dict[str, list[McapMessage]]:
    return {topic: reader.get_all_messages(topic) for topic in reader.list_topics()}


def clone_mcap_message(msg: McapMessage, log_time: int | None = None) -> McapMessage:
    ts = log_time if log_time is not None else msg.log_time
    cloned = McapMessage(
        topic=msg.topic,
        schema_name=msg.schema_name,
        canonical_schema_name=msg.canonical_schema_name,
        descriptor_format=msg.descriptor_format,
        is_legacy_schema=msg.is_legacy_schema,
        log_time=ts,
        publish_time=ts,
        message=deepcopy(msg.message),
    )
    if cloned.message.HasField("header"):
        cloned.message.header.timestamp_ns = ts
    return cloned


def rewrite_episode_mcap(
    episode_path: Path,
    data_file: str,
    messages_by_topic: dict[str, list[McapMessage]],
) -> dict[str, int]:
    """Atomically rewrite episode MCAP, returning message counts per topic."""
    mcap_path = episode_path / data_file
    backup_path = mcap_path.with_suffix(mcap_path.suffix + ".bak")
    temp_path = mcap_path.with_suffix(mcap_path.suffix + ".tmp")

    if mcap_path.is_file() and not backup_path.is_file():
        shutil.copy2(mcap_path, backup_path)

    counts: dict[str, int] = {}
    with McapEpisodeWriter(temp_path) as writer:
        for topic in sorted(messages_by_topic.keys()):
            messages = messages_by_topic[topic]
            if not messages:
                continue
            schema_name = messages[0].canonical_schema_name
            writer.register_topic(topic, schema_name)
            for msg in messages:
                writer.write(topic, msg.message)
            counts[topic] = len(messages)

    if temp_path.is_file():
        temp_path.replace(mcap_path)
    return counts


def remove_empty_files(root: Path) -> list[str]:
    """Delete 0-byte empty files, returning list of deleted paths."""
    removed: list[str] = []
    if not root.exists():
        return removed
    for path in root.rglob("*"):
        if path.is_file() and path.stat().st_size == 0:
            path.unlink(missing_ok=True)
            removed.append(str(path))
    return removed


def merge_stats(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    for key, value in source.items():
        if isinstance(value, int) and isinstance(target.get(key), int):
            target[key] = target[key] + value
        elif isinstance(value, list) and isinstance(target.get(key), list):
            target[key].extend(value)
        else:
            target[key] = value
    return target
