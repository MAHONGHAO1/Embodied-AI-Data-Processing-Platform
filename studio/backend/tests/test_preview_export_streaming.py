"""Bounded-memory regression tests for QRDF preview generation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from qrdf.registry.topics import CAMERA_FRONT_RGB
from qrdf.schema.qrdf.v0 import camera_pb2

from data.integrations.qrdf import preview_export
from data.integrations.qrdf.episode_metrics import infer_camera_fps_from_episode
from data.integrations.qrdf.preview_export import _pick_camera_topic, write_browser_mp4


def test_write_browser_mp4_consumes_frames_incrementally(tmp_path: Path, monkeypatch) -> None:
    import imageio.v2 as imageio

    state = {"yielded": 0, "written": 0}
    output_path = tmp_path / "preview.mp4"

    class FakeWriter:
        def __init__(self, path: str) -> None:
            self.path = Path(path)

        def __enter__(self):
            return self

        def append_data(self, frame: object) -> None:
            assert frame == state["written"]
            state["written"] += 1

        def __exit__(self, exc_type, exc, traceback) -> bool:
            if exc_type is None:
                self.path.write_bytes(b"mp4")
            return False

    def frame_factory():
        for index in range(3):
            # A materializing implementation asks for frame N+1 before frame N
            # reaches the encoder and fails this assertion.
            assert state["yielded"] == state["written"]
            state["yielded"] += 1
            yield index, 1_000_000_000 + index

    monkeypatch.setattr(imageio, "get_writer", lambda path, **_kwargs: FakeWriter(path))
    monkeypatch.setattr(
        "data.integrations.qrdf.gpu_utils.imageio_ffmpeg_nvenc_available",
        lambda: False,
    )

    result_path, timestamps = write_browser_mp4(frame_factory, output_path, fps=15.0)

    assert result_path == output_path
    assert output_path.read_bytes() == b"mp4"
    assert timestamps == (1_000_000_000, 1_000_000_001, 1_000_000_002)
    assert state == {"yielded": 3, "written": 3}


def test_pick_camera_topic_streams_when_metadata_is_missing() -> None:
    camera_frame = camera_pb2.CameraFrame(data=b"jpeg")
    mcap_reader = SimpleNamespace(
        iter_all_messages_streaming=lambda: iter(
            [SimpleNamespace(topic=CAMERA_FRONT_RGB, message=camera_frame)]
        )
    )
    episode = SimpleNamespace(metadata=None, mcap_reader=mcap_reader)

    assert _pick_camera_topic(episode) == CAMERA_FRONT_RGB


def test_infer_camera_fps_streams_without_populating_episode_cache(tmp_path: Path) -> None:
    timestamps = (1_000_000_000, 1_100_000_000, 1_200_000_000)

    class FakeEpisode:
        path = tmp_path
        metadata = None
        mcap_reader = SimpleNamespace(
            iter_messages_streaming=lambda _topic: iter(
                SimpleNamespace(log_time=timestamp_ns) for timestamp_ns in timestamps
            )
        )

        def iter_topic(self, _topic: str):
            raise AssertionError("preview FPS inference must not populate the episode cache")

    assert infer_camera_fps_from_episode(FakeEpisode(), CAMERA_FRONT_RGB) == 10.0


def test_export_preview_segments_decodes_parent_once_with_half_open_ranges(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import imageio.v2 as imageio

    state = {
        "reader_creations": 0,
        "yielded": 0,
        "reader_closed": 0,
        "active_writers": 0,
        "max_active_writers": 0,
    }
    frames_by_path: dict[Path, list[int]] = {}

    class FakeReader:
        def __iter__(self):
            for frame in range(6):
                state["yielded"] += 1
                yield frame

        def close(self) -> None:
            state["reader_closed"] += 1

    class FakeWriter:
        def __init__(self, path: str) -> None:
            self.path = Path(path)
            self.frames: list[int] = []
            state["active_writers"] += 1
            state["max_active_writers"] = max(state["max_active_writers"], state["active_writers"])

        def append_data(self, frame: object) -> None:
            self.frames.append(int(frame))

        def close(self) -> None:
            frames_by_path[self.path] = list(self.frames)
            self.path.write_text(",".join(str(frame) for frame in self.frames), encoding="ascii")
            state["active_writers"] -= 1

    def get_reader(_path: str) -> FakeReader:
        state["reader_creations"] += 1
        return FakeReader()

    monkeypatch.setattr(imageio, "get_reader", get_reader)
    monkeypatch.setattr(imageio, "get_writer", lambda path, **_kwargs: FakeWriter(path))
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"previous")
    second.write_bytes(b"previous")

    segments = (
        preview_export.PreviewSegmentSpec(
            episode_id=11, start_ns=100, end_ns=300, output_path=first
        ),
        preview_export.PreviewSegmentSpec(
            episode_id=12, start_ns=400, end_ns=700, output_path=second
        ),
    )

    facts = preview_export.export_preview_segments_from_mp4(
        tmp_path / "source.mp4",
        source_timestamps_ns=(100, 200, 300, 400, 500, 600),
        segments=segments,
        fps=15.0,
    )

    assert [(fact.episode_id, fact.frame_timestamps_ns) for fact in facts] == [
        (11, (100, 200)),
        (12, (400, 500, 600)),
    ]
    assert [fact.path for fact in facts] == [first, second]
    assert frames_by_path == {
        first.with_suffix(".tmp.mp4"): [0, 1],
        second.with_suffix(".tmp.mp4"): [3, 4, 5],
    }
    assert first.read_text(encoding="ascii") == "0,1"
    assert second.read_text(encoding="ascii") == "3,4,5"
    assert not first.with_suffix(".tmp.mp4").exists()
    assert not second.with_suffix(".tmp.mp4").exists()
    assert state == {
        "reader_creations": 1,
        "yielded": 6,
        "reader_closed": 1,
        "active_writers": 0,
        "max_active_writers": 1,
    }


@pytest.mark.parametrize(
    ("timestamps", "frames"),
    [
        ((100, 200), (0, 1, 2)),
        ((100, 200, 300), (0, 1)),
    ],
)
def test_export_preview_segments_rejects_frame_count_mismatch_and_cleans_temps(
    tmp_path: Path,
    monkeypatch,
    timestamps: tuple[int, ...],
    frames: tuple[int, ...],
) -> None:
    import imageio.v2 as imageio

    output = tmp_path / "preview.mp4"
    output.write_bytes(b"previous")
    temporary = output.with_suffix(".tmp.mp4")
    state = {"reader_closed": 0, "writer_closed": 0}

    class FakeReader:
        def __iter__(self):
            return iter(frames)

        def close(self) -> None:
            state["reader_closed"] += 1

    class FakeWriter:
        def __init__(self, path: str) -> None:
            self.path = Path(path)

        def append_data(self, _frame: object) -> None:
            return None

        def close(self) -> None:
            self.path.write_bytes(b"partial")
            state["writer_closed"] += 1

    monkeypatch.setattr(imageio, "get_reader", lambda _path: FakeReader())
    monkeypatch.setattr(imageio, "get_writer", lambda path, **_kwargs: FakeWriter(path))

    with pytest.raises(ValueError, match="frame count"):
        preview_export.export_preview_segments_from_mp4(
            tmp_path / "source.mp4",
            source_timestamps_ns=timestamps,
            segments=(
                preview_export.PreviewSegmentSpec(
                    episode_id=11,
                    start_ns=100,
                    end_ns=400,
                    output_path=output,
                ),
            ),
            fps=15.0,
        )

    assert output.read_bytes() == b"previous"
    assert not temporary.exists()
    assert state["reader_closed"] == 1
    assert state["writer_closed"] == 1


def test_export_preview_segments_rejects_invalid_or_empty_ranges(
    tmp_path: Path, monkeypatch
) -> None:
    import imageio.v2 as imageio

    output = tmp_path / "preview.mp4"
    reader_calls = 0

    def get_reader(_path: str):
        nonlocal reader_calls
        reader_calls += 1
        return iter((0,))

    monkeypatch.setattr(imageio, "get_reader", get_reader)
    monkeypatch.setattr(
        imageio,
        "get_writer",
        lambda _path, **_kwargs: (_ for _ in ()).throw(AssertionError("writer must not open")),
    )

    with pytest.raises(ValueError, match="ordered"):
        preview_export.export_preview_segments_from_mp4(
            tmp_path / "source.mp4",
            source_timestamps_ns=(100, 200),
            segments=(
                preview_export.PreviewSegmentSpec(11, 200, 400, output),
                preview_export.PreviewSegmentSpec(12, 300, 500, tmp_path / "second.mp4"),
            ),
            fps=15.0,
        )
    with pytest.raises(ValueError, match="unique"):
        preview_export.export_preview_segments_from_mp4(
            tmp_path / "source.mp4",
            source_timestamps_ns=(100, 200),
            segments=(
                preview_export.PreviewSegmentSpec(11, 100, 150, output),
                preview_export.PreviewSegmentSpec(
                    12,
                    150,
                    250,
                    tmp_path / "equivalent" / ".." / "preview.mp4",
                ),
            ),
            fps=15.0,
        )
    with pytest.raises(ValueError, match="frame"):
        preview_export.export_preview_segments_from_mp4(
            tmp_path / "source.mp4",
            source_timestamps_ns=(100,),
            segments=(preview_export.PreviewSegmentSpec(11, 200, 300, output),),
            fps=15.0,
        )

    assert reader_calls == 1
    assert not output.with_suffix(".tmp.mp4").exists()


def test_export_preview_segments_cleans_temp_after_encoder_error(
    tmp_path: Path, monkeypatch
) -> None:
    import imageio.v2 as imageio

    output = tmp_path / "preview.mp4"
    output.write_bytes(b"previous")
    temporary = output.with_suffix(".tmp.mp4")
    state = {"reader_closed": 0, "writer_closed": 0}

    class FakeReader:
        def __iter__(self):
            return iter((0,))

        def close(self) -> None:
            state["reader_closed"] += 1

    class FailingWriter:
        def __init__(self, path: str) -> None:
            self.path = Path(path)

        def append_data(self, _frame: object) -> None:
            raise RuntimeError("encoding failed")

        def close(self) -> None:
            self.path.write_bytes(b"partial")
            state["writer_closed"] += 1

    monkeypatch.setattr(imageio, "get_reader", lambda _path: FakeReader())
    monkeypatch.setattr(imageio, "get_writer", lambda path, **_kwargs: FailingWriter(path))

    with pytest.raises(RuntimeError, match="encoding failed"):
        preview_export.export_preview_segments_from_mp4(
            tmp_path / "source.mp4",
            source_timestamps_ns=(100,),
            segments=(preview_export.PreviewSegmentSpec(11, 100, 200, output),),
            fps=15.0,
        )

    assert output.read_bytes() == b"previous"
    assert not temporary.exists()
    assert state == {"reader_closed": 1, "writer_closed": 1}
