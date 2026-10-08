"""Bidirectional conversion between clip_descriptions and region_frames, and segment validation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from data.services.annotate_tags import (
    action_label,
    normalize_quality_value,
    normalize_success_value,
    resolve_action_key,
    success_to_type,
)

SUCCESS_TO_TYPE = {1: "success", 0: "failure", -1: "unknown"}
TYPE_TO_SUCCESS = {v: k for k, v in SUCCESS_TO_TYPE.items()}


def segment_key(start_frame: int, end_frame: int) -> str:
    return f"{start_frame}_{end_frame}"


def region_frame_to_segment(
    rf: dict,
    *,
    action_by_key: Mapping[str, object] | None = None,
    label_by_key: Mapping[str, str] | None = None,
    label_to_key: Mapping[str, str] | None = None,
) -> tuple[str, dict]:
    """Legacy region_frames item -> (segment_key, segment_dict)."""
    start = int(rf.get("start_frame", rf.get("start", 0)))
    end = int(rf.get("end_frame", rf.get("end", 0)))
    key = segment_key(start, end)
    action_key, _ = resolve_action_key(
        str(rf.get("action") or rf.get("tag") or ""),
        action_by_key=action_by_key,
        label_by_key=label_by_key,
        label_to_key=label_to_key,
    )
    legacy_type = rf.get("type")
    if legacy_type is not None:
        success = TYPE_TO_SUCCESS.get(str(legacy_type), -1)
    else:
        success = normalize_success_value(rf.get("success", 1))
    quality = normalize_quality_value(rf.get("quality", 1))
    return key, {
        "id": rf.get("id", ""),
        "start_frame": start,
        "end_frame": end,
        "quality": quality,
        "success": success,
        "action": action_key,
        "subtask": str(rf.get("subtask") or rf.get("remark") or ""),
    }


def region_frames_to_clip_descriptions(
    region_frames: list[dict],
    *,
    action_by_key: Mapping[str, object] | None = None,
    label_by_key: Mapping[str, str] | None = None,
    label_to_key: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    segments: dict[str, dict] = {}
    for rf in region_frames:
        key, seg = region_frame_to_segment(
            rf,
            action_by_key=action_by_key,
            label_by_key=label_by_key,
            label_to_key=label_to_key,
        )
        segments[key] = seg
    return {"segments": segments}


def clip_descriptions_from_doc(
    doc: dict,
    *,
    action_by_key: Mapping[str, object] | None = None,
    label_by_key: Mapping[str, str] | None = None,
    label_to_key: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Parse clip_descriptions from SQL annotation document (compatible with region_frames)."""
    if doc.get("clip_descriptions") and doc["clip_descriptions"].get("segments"):
        return doc["clip_descriptions"]
    region_frames = doc.get("region_frames") or doc.get("items") or []
    if isinstance(region_frames, dict):
        region_frames = list(region_frames.values())
    if region_frames:
        return region_frames_to_clip_descriptions(
            list(region_frames),
            action_by_key=action_by_key,
            label_by_key=label_by_key,
            label_to_key=label_to_key,
        )
    return {"segments": {}}


def segment_to_annotation_item(
    seg: dict,
    *,
    action_by_key: Mapping[str, object] | None = None,
    label_by_key: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Convert segment -> API-derived annotations[] item."""
    action_key = str(seg.get("action") or "")
    success = normalize_success_value(seg.get("success", 1))
    quality = normalize_quality_value(seg.get("quality", 1))
    start = int(seg.get("start_frame", 0))
    end = int(seg.get("end_frame", 0))
    return {
        "id": seg.get("id", ""),
        "start": start,
        "end": end,
        "action": action_key,
        "action_cn": action_label(
            action_key,
            action_by_key=action_by_key,
            label_by_key=label_by_key,
        ),
        "success": success,
        "quality": quality,
        "type": success_to_type(success),
        "subtask": str(seg.get("subtask") or ""),
    }


def segments_to_annotations(
    segments: dict[str, dict],
    *,
    action_by_key: Mapping[str, object] | None = None,
    label_by_key: Mapping[str, str] | None = None,
) -> list[dict]:
    items = [
        segment_to_annotation_item(
            seg,
            action_by_key=action_by_key,
            label_by_key=label_by_key,
        )
        for seg in segments.values()
    ]
    items.sort(key=lambda x: (x["start"], x["end"]))
    return items


def validate_segment_overlap(segments: dict[str, dict]) -> None:
    """Segments within the same episode must not overlap."""
    ranges = []
    for seg in segments.values():
        start = int(seg["start_frame"])
        end = int(seg["end_frame"])
        ranges.append((start, end))
    ranges.sort()
    for i in range(1, len(ranges)):
        prev_start, prev_end = ranges[i - 1]
        cur_start, _ = ranges[i]
        if cur_start <= prev_end:
            raise ValueError("片段帧区间不得重叠")


def validate_segment_keys(segments: dict[str, dict]) -> None:
    for key, seg in segments.items():
        expected = segment_key(int(seg["start_frame"]), int(seg["end_frame"]))
        if key != expected:
            raise ValueError(f"segment 键 {key} 与帧区间 {expected} 不一致")


def normalize_submit_segment(
    raw: dict,
    *,
    total_frames: int,
    action_by_key: Mapping[str, object],
    label_to_key: Mapping[str, str],
) -> dict:
    """Normalize a single segment submission item."""
    start = int(raw.get("start_frame", raw.get("start", 0)))
    end = int(raw.get("end_frame", raw.get("end", 0)))
    if start > end:
        raise ValueError(f"片段 {raw.get('id')} 起止帧非法")
    if total_frames > 0 and (start < 0 or end >= total_frames):
        raise ValueError(f"片段 {raw.get('id')} 超出有效帧范围 [0, {total_frames - 1}]")
    raw_action = str(raw.get("action") or raw.get("tag") or "").strip()
    if raw_action in action_by_key:
        action_key = raw_action
    elif raw_action in label_to_key:
        action_key = label_to_key[raw_action]
    else:
        raise ValueError(f"action {raw_action!r} 不在词表中")
    success = raw.get("success")
    if success is None and raw.get("type") is not None:
        success = TYPE_TO_SUCCESS.get(str(raw.get("type")), 1)
    if success is not None:
        try:
            sv = int(success)
        except (TypeError, ValueError):
            raise ValueError(f"success 非法: {success}") from None
        if sv not in {1, 0, -1}:
            raise ValueError(f"success 非法: {success}")
    else:
        sv = 1
    quality = raw.get("quality", 1)
    try:
        qv = int(quality)
    except (TypeError, ValueError):
        raise ValueError(f"quality 非法: {quality}") from None
    if qv not in {0, 1}:
        raise ValueError(f"quality 非法: {quality}")
    success = sv
    quality = qv
    rf_id = str(raw.get("id") or "").strip()
    if not rf_id.startswith("rf_"):
        raise ValueError(f"片段 {rf_id} 须通过 POST /region-frames/id 预分配")
    key = segment_key(start, end)
    return key, {
        "id": rf_id,
        "start_frame": start,
        "end_frame": end,
        "quality": quality,
        "success": success,
        "action": action_key,
        "subtask": str(raw.get("subtask") or raw.get("remark") or ""),
    }
