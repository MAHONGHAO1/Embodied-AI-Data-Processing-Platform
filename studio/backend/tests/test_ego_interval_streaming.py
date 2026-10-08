from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import mcap.reader as raw_mcap_reader
from mcap.reader import make_reader
from qrdf.mcap.reader import McapEpisodeReader, McapMessage
from qrdf.mcap.writer import McapEpisodeWriter
from qrdf.schema.qrdf.v0 import common_pb2

from data.integrations.qrdf.mcap_ops import open_indexed_mcap_reader


def _message(topic: str, timestamp_ns: int, *, event_type: str = "") -> McapMessage:
    payload = common_pb2.EpisodeEvent(
        header=common_pb2.Header(timestamp_ns=timestamp_ns),
        event_type=event_type,
    )
    return McapMessage(
        topic=topic,
        schema_name="qrdf.v0.EpisodeEvent",
        canonical_schema_name="qrdf.v0.EpisodeEvent",
        descriptor_format="file_descriptor_set",
        is_legacy_schema=False,
        log_time=timestamp_ns,
        publish_time=timestamp_ns,
        message=payload,
    )


def test_interval_writer_uses_one_streaming_pass_and_preserves_interval_semantics(
    tmp_path, monkeypatch
):
    from data.integrations.qrdf import ego

    source = tmp_path / "source"
    source.mkdir()
    (source / "metadata.json").write_text("{}", encoding="utf-8")
    (source / "data.mcap").write_bytes(b"source")
    destination = tmp_path / "episode_000001"
    messages = [
        _message("/camera/info", 5),
        _message("/camera/rgb", 9),
        _message("/camera/rgb", 10),
        _message(ego.EPISODE_EVENT, 11, event_type="start"),
        _message("/camera/info", 12),
        _message(ego.EPISODE_EVENT, 12, event_type="segment"),
        _message("/camera/rgb", 19),
        _message(ego.EPISODE_EVENT, 19, event_type="stop"),
        _message("/camera/rgb", 20),
    ]

    class FakeMetadata:
        data_file = "data.mcap"

    class FakeOutputMetadata:
        data_file = "data.mcap"

        def save(self, path: Path) -> None:
            path.write_text("{}", encoding="utf-8")

    class FakeReader:
        instance = None

        def __init__(self, _path):
            self.streaming_calls = 0
            self.eager_calls = 0
            FakeReader.instance = self

        def iter_all_messages_streaming(self):
            self.streaming_calls += 1
            yield from messages

        def list_topics(self):
            self.eager_calls += 1
            raise AssertionError("list_topics eagerly loads the complete MCAP")

        def iter_messages(self, _topic):
            self.eager_calls += 1
            raise AssertionError("iter_messages eagerly loads the complete MCAP")

        def get_schema_name(self, _topic):
            self.eager_calls += 1
            raise AssertionError("schema getters eagerly load the complete MCAP")

    class FakeWriter:
        instance = None

        def __init__(self, _path):
            self.registered: dict[str, str] = {}
            self.writes: list[tuple[str, int, str]] = []
            FakeWriter.instance = self

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def register_topic(self, topic: str, schema_name: str) -> None:
            self.registered[topic] = schema_name

        def write(self, topic: str, message) -> None:
            self.writes.append(
                (topic, int(message.header.timestamp_ns), str(getattr(message, "event_type", "")))
            )

    selected_topics: list[set[str]] = []

    def fake_build(_metadata, **kwargs):
        selected_topics.append(set(kwargs["selected_topics"]))
        return FakeOutputMetadata()

    monkeypatch.setattr(ego.EpisodeMetadata, "load", lambda _path: FakeMetadata())
    monkeypatch.setattr(
        ego, "resolve_qrdf_episode_data_file", lambda _metadata, root: Path(root) / "data.mcap"
    )
    monkeypatch.setattr(ego, "McapEpisodeReader", FakeReader)
    monkeypatch.setattr(ego, "McapEpisodeWriter", FakeWriter)
    monkeypatch.setattr(ego, "_calibration_topics", lambda _metadata: {"/camera/info"})
    monkeypatch.setattr(ego, "_prune_optional_unaligned_topics", lambda *_args: None)
    monkeypatch.setattr(ego, "_build_interval_metadata", fake_build)

    ego.write_ego_interval_episode(
        source,
        destination,
        output_episode_id="episode_000001",
        start_ns=10,
        end_ns=20,
        task_language="pick",
    )

    assert FakeReader.instance.streaming_calls == 1
    assert FakeReader.instance.eager_calls == 0
    assert selected_topics == [{"/camera/info", "/camera/rgb"}]
    writes = FakeWriter.instance.writes
    assert [
        (topic, timestamp) for topic, timestamp, _event in writes if topic == "/camera/info"
    ] == [
        ("/camera/info", 5),
        ("/camera/info", 10),
    ]
    assert [
        (topic, timestamp) for topic, timestamp, _event in writes if topic == "/camera/rgb"
    ] == [
        ("/camera/rgb", 10),
        ("/camera/rgb", 19),
    ]
    assert [
        (timestamp, event) for topic, timestamp, event in writes if topic == ego.EPISODE_EVENT
    ] == [
        (10, "start"),
        (12, "segment"),
        (20, "stop"),
    ]


def test_indexed_reader_only_decompresses_chunks_overlapping_requested_window(
    tmp_path, monkeypatch
):
    mcap_path = tmp_path / "indexed.mcap"
    with McapEpisodeWriter(mcap_path) as writer:
        writer.register_topic("/events", "qrdf.v0.EpisodeEvent")
        for timestamp_ns in (1, 2, 3):
            writer.write(
                "/events",
                common_pb2.EpisodeEvent(
                    header=common_pb2.Header(timestamp_ns=timestamp_ns),
                    event_type="segment",
                    message="x" * 700_000,
                ),
            )

    with mcap_path.open("rb") as file_obj:
        summary = make_reader(file_obj).get_summary()
    assert summary is not None
    assert len(summary.chunk_indexes) >= 2

    decompressed_chunks = 0
    original_breakup_chunk = raw_mcap_reader.breakup_chunk

    def count_breakup_chunk(*args, **kwargs):
        nonlocal decompressed_chunks
        decompressed_chunks += 1
        return original_breakup_chunk(*args, **kwargs)

    monkeypatch.setattr(raw_mcap_reader, "breakup_chunk", count_breakup_chunk)
    with open_indexed_mcap_reader(McapEpisodeReader(mcap_path)) as indexed_reader:
        assert indexed_reader is not None
        messages = list(
            indexed_reader.iter_messages(
                topics=None,
                start_ns=3,
                end_ns=4,
            )
        )

    assert [message.log_time for message in messages] == [3]
    assert decompressed_chunks == 1
    assert decompressed_chunks < len(summary.chunk_indexes)


def test_interval_writer_prefers_indexed_window_and_preserves_calibration_context(
    tmp_path,
    monkeypatch,
):
    from data.integrations.qrdf import ego

    source = tmp_path / "source"
    source.mkdir()
    (source / "metadata.json").write_text("{}", encoding="utf-8")
    (source / "data.mcap").write_bytes(b"source")
    destination = tmp_path / "episode_000001"
    messages = [
        _message("/camera/info", 5),
        _message("/camera/rgb", 9),
        _message("/camera/rgb", 10),
        _message(ego.EPISODE_EVENT, 11, event_type="start"),
        _message("/camera/info", 12),
        _message(ego.EPISODE_EVENT, 12, event_type="segment"),
        _message("/camera/rgb", 19),
        _message(ego.EPISODE_EVENT, 19, event_type="stop"),
        _message("/camera/rgb", 20),
    ]

    class FakeMetadata:
        data_file = "data.mcap"

    class FakeOutputMetadata:
        data_file = "data.mcap"

        def save(self, path: Path) -> None:
            path.write_text("{}", encoding="utf-8")

    class FakeReader:
        def __init__(self, _path):
            pass

        def iter_all_messages_streaming(self):
            raise AssertionError("indexed MCAP must not fall back to a full streaming scan")

    class FakeIndexedReader:
        def __init__(self):
            self.calls: list[tuple[tuple[str, ...] | None, int | None, int | None, bool]] = []

        def iter_messages(self, *, topics, start_ns, end_ns, reverse=False):
            selected_topics = tuple(sorted(topics)) if topics is not None else None
            self.calls.append((selected_topics, start_ns, end_ns, reverse))
            candidates = [
                message
                for message in messages
                if (topics is None or message.topic in topics)
                and (start_ns is None or message.log_time >= start_ns)
                and (end_ns is None or message.log_time < end_ns)
            ]
            yield from sorted(candidates, key=lambda item: item.log_time, reverse=reverse)

    class FakeWriter:
        instance = None

        def __init__(self, _path):
            self.writes: list[tuple[str, int, str]] = []
            FakeWriter.instance = self

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def register_topic(self, _topic: str, _schema_name: str) -> None:
            return None

        def write(self, topic: str, message) -> None:
            self.writes.append(
                (topic, int(message.header.timestamp_ns), str(getattr(message, "event_type", "")))
            )

    indexed = FakeIndexedReader()

    @contextmanager
    def fake_open_indexed(_reader):
        yield indexed

    monkeypatch.setattr(ego.EpisodeMetadata, "load", lambda _path: FakeMetadata())
    monkeypatch.setattr(
        ego, "resolve_qrdf_episode_data_file", lambda _metadata, root: Path(root) / "data.mcap"
    )
    monkeypatch.setattr(ego, "McapEpisodeReader", FakeReader)
    monkeypatch.setattr(ego, "McapEpisodeWriter", FakeWriter)
    monkeypatch.setattr(ego, "open_indexed_mcap_reader", fake_open_indexed)
    monkeypatch.setattr(ego, "_calibration_topics", lambda _metadata: {"/camera/info"})
    monkeypatch.setattr(ego, "_prune_optional_unaligned_topics", lambda *_args: None)
    monkeypatch.setattr(
        ego, "_build_interval_metadata", lambda *_args, **_kwargs: FakeOutputMetadata()
    )

    ego.write_ego_interval_episode(
        source,
        destination,
        output_episode_id="episode_000001",
        start_ns=10,
        end_ns=20,
        task_language="pick",
    )

    assert indexed.calls == [
        (("/camera/info",), None, 11, True),
        (None, 10, 20, False),
    ]
    writes = FakeWriter.instance.writes
    assert [
        (topic, timestamp) for topic, timestamp, _event in writes if topic == "/camera/info"
    ] == [
        ("/camera/info", 5),
        ("/camera/info", 10),
    ]
    assert [
        (topic, timestamp) for topic, timestamp, _event in writes if topic == "/camera/rgb"
    ] == [
        ("/camera/rgb", 10),
        ("/camera/rgb", 19),
    ]
    assert [
        (timestamp, event) for topic, timestamp, event in writes if topic == ego.EPISODE_EVENT
    ] == [
        (10, "start"),
        (12, "segment"),
        (20, "stop"),
    ]
