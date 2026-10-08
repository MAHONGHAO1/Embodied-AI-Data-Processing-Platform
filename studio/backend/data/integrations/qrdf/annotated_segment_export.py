"""Export approved logical ranges as independent QRDF Episodes.

Source objects are read only. A delivered Episode contains one effective
half-open interval, never the source preview or the unselected raw recording.
Long intervals are partitioned into bounded, adjacent output samples.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from qrdf.models.annotation import EpisodeAnnotation
from qrdf.models.episode import EpisodeMetadata

from data.integrations.qrdf.ego import write_ego_interval_episode

MAX_OUTPUT_INTERVAL_NS = 300 * 1_000_000_000
_NS = re.compile(r"^(?:0|[1-9][0-9]{0,19})$")


def _ns(value: object) -> int:
    if not isinstance(value, str) or not _NS.fullmatch(value):
        raise ValueError("effective range timestamps must be exact decimal strings")
    return int(value)


def approved_segments(episode: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate the immutable asset contract, without reading current DB rows."""
    segments = episode.get("effective_segments")
    revision = episode.get("annotation_revision")
    if (
        type(episode.get("annotation_submission_id")) is not int
        or episode["annotation_submission_id"] < 1
        or episode.get("annotation_conclusion") != "segments"
        or not isinstance(revision, Mapping)
        or type(revision.get("id")) is not int
        or revision["id"] < 1
        or not isinstance(segments, list)
        or not 1 <= len(segments) <= 1000
    ):
        raise ValueError("approved annotation ranges or revision are missing")
    annotation = EpisodeAnnotation.model_validate(revision.get("payload"))
    start, end = _ns(episode.get("source_start_ns")), _ns(episode.get("source_end_ns"))
    if start >= end:
        raise ValueError("approved source range is invalid")
    if annotation.source.episode_id != episode.get("qrdf_episode_id"):
        raise ValueError("approved annotation source Episode does not match")
    parsed = []
    ids = set()
    previous_end = start
    for segment in segments:
        if not isinstance(segment, Mapping):
            raise ValueError("approved effective segment is invalid")
        seg_start, seg_end = _ns(segment.get("start_ns")), _ns(segment.get("end_ns"))
        description, segment_id = segment.get("description"), segment.get("id")
        if (
            not isinstance(segment_id, str)
            or not segment_id
            or segment_id in ids
            or not isinstance(description, str)
            or not description.strip()
            or not start <= seg_start < seg_end <= end
            or seg_start < previous_end
        ):
            raise ValueError("approved effective segments overlap or are invalid")
        ids.add(segment_id)
        parsed.append(dict(segment))
        previous_end = seg_end
    actual = [
        (item.id, item.target.start_ns, item.target.end_ns, item.high_level_subtask)
        for track in annotation.tracks
        if track.name == "high_level_subtask"
        for item in track.items
    ]
    expected = [
        (segment["id"], segment["start_ns"], segment["end_ns"], segment["description"])
        for segment in parsed
    ]
    if sorted(actual) != sorted(expected):
        raise ValueError("effective ranges do not match the approved QRDF revision")
    if str(
        sum(_ns(segment["end_ns"]) - _ns(segment["start_ns"]) for segment in parsed)
    ) != episode.get("effective_duration_ns"):
        raise ValueError("approved effective duration does not match its ranges")
    return parsed


def output_intervals(segments: list[dict[str, Any]]):
    for segment in segments:
        cursor, end = _ns(segment["start_ns"]), _ns(segment["end_ns"])
        part = 0
        while cursor < end:
            next_end = min(end, cursor + MAX_OUTPUT_INTERVAL_NS)
            yield {**segment, "start_ns": str(cursor), "end_ns": str(next_end), "part": part}
            cursor = next_end
            part += 1


def verify_source_episode(source: Path, snapshot: Mapping[str, Any]) -> EpisodeMetadata:
    metadata = EpisodeMetadata.load(source / "metadata.json")
    payload = snapshot["annotation_revision"]["payload"]
    digest = hashlib.sha256()
    with metadata.resolve_data_file(source).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    if (
        payload["source"]["episode_id"] != metadata.episode_id
        or payload["source"]["data_sha256"] != digest.hexdigest()
        or int(metadata.timing.start_timestamp_ns) != _ns(snapshot["source_start_ns"])
        or int(metadata.timing.end_timestamp_ns) != _ns(snapshot["source_end_ns"])
    ):
        raise ValueError("materialized source differs from the approved annotation source")
    return metadata


def write_approved_interval(
    source: Path,
    destination: Path,
    *,
    episode_id: str,
    snapshot: Mapping[str, Any],
    interval: Mapping[str, Any],
) -> dict:
    """Use the shared QRDF interval writer, with no legacy publication effects."""
    start, end = _ns(interval["start_ns"]), _ns(interval["end_ns"])
    write_ego_interval_episode(
        source,
        destination,
        output_episode_id=episode_id,
        start_ns=start,
        end_ns=end,
        task_language=interval["description"],
    )
    provenance = {
        "source_episode_id": snapshot["episode_id"],
        "source_qrdf_episode_id": snapshot["qrdf_episode_id"],
        "source_fingerprint": snapshot["source_fingerprint"],
        "admission_attempt": snapshot["admission_attempt"],
        "annotation_submission_id": snapshot["annotation_submission_id"],
        "annotation_revision_id": snapshot["annotation_revision"]["id"],
        "segment_id": interval["id"],
        "segment_part": interval["part"],
        "start_ns": str(start),
        "end_ns": str(end),
        "description": interval["description"],
        "interval_message_policy": {
            "dynamic": "[start,end)",
            "calibration": "latest_at_or_before_start_with_alignment_anchor",
            "lifecycle": "synthesized",
        },
    }
    metadata = EpisodeMetadata.load(destination / "metadata.json")
    metadata.extensions["quicstudio_annotation_segment"] = deepcopy(provenance)
    metadata.save(destination / "metadata.json")
    payload = clipped_annotation(snapshot["annotation_revision"]["payload"], interval=interval)
    return {
        "metadata": metadata,
        "annotation_revision": {
            "id": snapshot["annotation_revision"]["id"],
            "version": snapshot["annotation_revision"].get("version"),
            "payload": payload,
        },
        "provenance": provenance,
    }


def clipped_annotation(payload: Mapping[str, Any], *, interval: Mapping[str, Any]) -> dict:
    """Keep the segment's semantic track and clip auxiliary tracks to this sample."""
    result = deepcopy(dict(payload))
    start, end = _ns(interval["start_ns"]), _ns(interval["end_ns"])
    tracks = []
    for track in result["tracks"]:
        items = []
        for item in track["items"]:
            if track["name"] == "high_level_subtask" and item["id"] != interval["id"]:
                continue
            if (
                track["name"] == "atomic_subtask"
                and item["parent_high_level_subtask_id"] != interval["id"]
            ):
                continue
            target = item["target"]
            if target["type"] == "time_point":
                if not start <= _ns(target["timestamp_ns"]) < end:
                    continue
            else:
                clipped_start = max(start, _ns(target["start_ns"]))
                clipped_end = min(end, _ns(target["end_ns"]))
                if clipped_start >= clipped_end:
                    continue
                target["start_ns"], target["end_ns"] = str(clipped_start), str(clipped_end)
            items.append(item)
        if items:
            tracks.append({"name": track["name"], "items": items})
    result["tracks"] = tracks
    return EpisodeAnnotation.model_validate(result).model_dump(mode="json", exclude_none=True)
