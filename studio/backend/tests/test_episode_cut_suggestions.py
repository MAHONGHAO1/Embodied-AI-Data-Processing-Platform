import json

from qrdf.schema.qrdf.v0 import common_pb2

from data.services.episode_cut_suggestions import (
    MAX_CUT_SUGGESTIONS,
    RawCutSuggestion,
    decode_qr_segment_event,
    project_qr_segment_events,
)


def _event_payload(*, timestamp_ns: int, message: object, event_type: str = "segment") -> bytes:
    event = common_pb2.EpisodeEvent(
        header=common_pb2.Header(timestamp_ns=timestamp_ns),
        event_type=event_type,
        message=json.dumps(message) if not isinstance(message, str) else message,
    )
    return event.SerializeToString()


def test_qr_segment_event_decoder_accepts_only_the_declared_qrdf_contract():
    payload = _event_payload(
        timestamp_ns=5_000,
        message={
            "v": 1,
            "collector_id": "must-not-be-projected",
            "segment_id": "segment-2",
            "segment_index": 2,
        },
    )

    decoded = decode_qr_segment_event(
        schema_name="qrdf.v0.EpisodeEvent",
        topic="/episode/event",
        payload=payload,
    )

    assert decoded == RawCutSuggestion(
        timestamp_ns=5_000,
        segment_id_hint="segment-2",
        segment_index=2,
        protocol_version=1,
    )
    assert (
        decode_qr_segment_event(
            schema_name="test.Event",
            topic="/episode/event",
            payload=payload,
        )
        is None
    )
    assert (
        decode_qr_segment_event(
            schema_name="qrdf.v0.EpisodeEvent",
            topic="/other/event",
            payload=payload,
        )
        is None
    )
    assert (
        decode_qr_segment_event(
            schema_name="qrdf.v0.EpisodeEvent",
            topic="/episode/event",
            payload=_event_payload(timestamp_ns=5_000, message={}, event_type="start"),
        )
        is None
    )


def test_qr_segment_event_decoder_fails_closed_for_malformed_or_unbounded_fields():
    invalid_payloads = (
        b"not-protobuf",
        _event_payload(timestamp_ns=5_000, message="not-json"),
        _event_payload(timestamp_ns=0, message={"v": 1, "segment_id": "a", "segment_index": 1}),
        _event_payload(
            timestamp_ns=5_000, message={"v": True, "segment_id": "a", "segment_index": 1}
        ),
        _event_payload(timestamp_ns=5_000, message={"v": 1, "segment_id": "", "segment_index": 1}),
        _event_payload(
            timestamp_ns=5_000, message={"v": 1, "segment_id": "x" * 129, "segment_index": 1}
        ),
        _event_payload(
            timestamp_ns=5_000, message={"v": 1, "segment_id": "a", "segment_index": -1}
        ),
    )

    assert all(
        decode_qr_segment_event(
            schema_name="qrdf.v0.EpisodeEvent",
            topic="/episode/event",
            payload=payload,
        )
        is None
        for payload in invalid_payloads
    )


def test_qr_segment_projection_is_ordered_bounded_and_excludes_raw_identity():
    events = [
        RawCutSuggestion(8_000, "segment-8", 8, 1),
        RawCutSuggestion(5_000, "segment-5", 5, 1),
        RawCutSuggestion(5_000, "duplicate", 50, 1),
        RawCutSuggestion(1_000, "at-start", 1, 1),
        RawCutSuggestion(10_000, "at-end", 10, 1),
    ]

    projection = project_qr_segment_events(
        events=events,
        start_ns=1_000,
        end_ns=10_000,
        max_suggestions=2,
    )

    assert projection == {
        "schema": "quicdata.cut-suggestions.v1",
        "items": [
            {
                "timestamp_ns": "5000",
                "segment_id_hint": "segment-5",
                "segment_index": 5,
                "protocol_version": 1,
            },
            {
                "timestamp_ns": "8000",
                "segment_id_hint": "segment-8",
                "segment_index": 8,
                "protocol_version": 1,
            },
        ],
    }
    serialized = json.dumps(projection)
    assert "collector" not in serialized
    assert "message" not in serialized


def test_qr_segment_projection_rejects_invalid_bounds_and_limits():
    event = RawCutSuggestion(5_000, "segment-5", 5, 1)

    assert project_qr_segment_events(events=[event], start_ns=10, end_ns=10) == {
        "schema": "quicdata.cut-suggestions.v1",
        "items": [],
    }
    assert project_qr_segment_events(
        events=[event] * (MAX_CUT_SUGGESTIONS + 5),
        start_ns=1,
        end_ns=10_000,
    )["items"] == [
        {
            "timestamp_ns": "5000",
            "segment_id_hint": "segment-5",
            "segment_index": 5,
            "protocol_version": 1,
        }
    ]
