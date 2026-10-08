"""Bounded projection of QuicEgo QR segment events for cut workbenches."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass

from google.protobuf.message import DecodeError
from qrdf.schema.qrdf.v0 import common_pb2

QR_EVENT_TOPIC = "/episode/event"
QR_EVENT_SCHEMA = "qrdf.v0.EpisodeEvent"
CUT_SUGGESTION_SCHEMA = "quicdata.cut-suggestions.v1"
MAX_CUT_SUGGESTIONS = 499
_MAX_EVENT_PAYLOAD_BYTES = 16 * 1024
_MAX_EVENT_MESSAGE_BYTES = 4 * 1024
_MAX_PROTOCOL_VERSION = 1_000_000
_MAX_SEGMENT_INDEX = 1_000_000


@dataclass(frozen=True)
class RawCutSuggestion:
    timestamp_ns: int
    segment_id_hint: str
    segment_index: int
    protocol_version: int


def empty_cut_suggestion_projection() -> dict[str, object]:
    return {"schema": CUT_SUGGESTION_SCHEMA, "items": []}


def decode_qr_segment_event(
    *, schema_name: str, topic: str, payload: bytes
) -> RawCutSuggestion | None:
    """Decode one explicitly supported QRDF event without exposing its raw message."""
    if schema_name != QR_EVENT_SCHEMA or topic != QR_EVENT_TOPIC:
        return None
    if not isinstance(payload, bytes) or not payload or len(payload) > _MAX_EVENT_PAYLOAD_BYTES:
        return None
    event = common_pb2.EpisodeEvent()
    try:
        event.ParseFromString(payload)
    except DecodeError:
        return None
    if event.event_type != "segment" or not event.HasField("message"):
        return None
    if len(event.message.encode("utf-8")) > _MAX_EVENT_MESSAGE_BYTES:
        return None
    try:
        message = json.loads(event.message)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(message, dict):
        return None

    timestamp_ns = event.header.timestamp_ns
    protocol_version = message.get("v")
    segment_id = message.get("segment_id")
    segment_index = message.get("segment_index")
    if type(timestamp_ns) is not int or timestamp_ns <= 0:
        return None
    if (
        type(protocol_version) is not int
        or not 1 <= protocol_version <= _MAX_PROTOCOL_VERSION
        or type(segment_index) is not int
        or not 0 <= segment_index <= _MAX_SEGMENT_INDEX
    ):
        return None
    if not isinstance(segment_id, str):
        return None
    segment_id = segment_id.strip()
    if not segment_id or len(segment_id) > 128 or "\x00" in segment_id:
        return None
    return RawCutSuggestion(
        timestamp_ns=timestamp_ns,
        segment_id_hint=segment_id,
        segment_index=segment_index,
        protocol_version=protocol_version,
    )


def project_qr_segment_events(
    *,
    events: Iterable[RawCutSuggestion],
    start_ns: int,
    end_ns: int,
    max_suggestions: int = MAX_CUT_SUGGESTIONS,
) -> dict[str, object]:
    """Return a bounded, ordered, identity-free projection for one source Episode."""
    if type(start_ns) is not int or type(end_ns) is not int or start_ns < 0 or start_ns >= end_ns:
        return empty_cut_suggestion_projection()
    if type(max_suggestions) is not int or max_suggestions <= 0:
        return empty_cut_suggestion_projection()
    limit = min(max_suggestions, MAX_CUT_SUGGESTIONS)
    unique: dict[int, RawCutSuggestion] = {}
    for event in events:
        if not isinstance(event, RawCutSuggestion):
            continue
        if (
            event.timestamp_ns <= start_ns
            or event.timestamp_ns >= end_ns
            or event.timestamp_ns in unique
        ):
            continue
        unique[event.timestamp_ns] = event
        if len(unique) >= limit:
            break
    items = [
        {
            "timestamp_ns": str(event.timestamp_ns),
            "segment_id_hint": event.segment_id_hint,
            "segment_index": event.segment_index,
            "protocol_version": event.protocol_version,
        }
        for event in sorted(unique.values(), key=lambda item: item.timestamp_ns)
    ]
    return {"schema": CUT_SUGGESTION_SCHEMA, "items": items}


def safe_cut_suggestion_items(
    *, projection: object, start_ns: int, end_ns: int
) -> list[dict[str, object]]:
    """Revalidate the stored worker projection before returning it from an API."""
    if not isinstance(projection, dict) or projection.get("schema") != CUT_SUGGESTION_SCHEMA:
        return []
    raw_items = projection.get("items")
    if not isinstance(raw_items, list):
        return []
    suggestions: list[RawCutSuggestion] = []
    for raw in raw_items[:MAX_CUT_SUGGESTIONS]:
        if not isinstance(raw, dict):
            continue
        timestamp_raw = raw.get("timestamp_ns")
        segment_id = raw.get("segment_id_hint")
        segment_index = raw.get("segment_index")
        protocol_version = raw.get("protocol_version")
        if (
            not isinstance(timestamp_raw, str)
            or not timestamp_raw.isdigit()
            or len(timestamp_raw) > 20
            or not isinstance(segment_id, str)
            or not segment_id.strip()
            or len(segment_id.strip()) > 128
            or "\x00" in segment_id
            or type(segment_index) is not int
            or not 0 <= segment_index <= _MAX_SEGMENT_INDEX
            or type(protocol_version) is not int
            or not 1 <= protocol_version <= _MAX_PROTOCOL_VERSION
        ):
            continue
        suggestions.append(
            RawCutSuggestion(
                timestamp_ns=int(timestamp_raw),
                segment_id_hint=segment_id.strip(),
                segment_index=segment_index,
                protocol_version=protocol_version,
            )
        )
    projected = project_qr_segment_events(
        events=suggestions,
        start_ns=start_ns,
        end_ns=end_ns,
    )
    items = projected["items"]
    return items if isinstance(items, list) else []
