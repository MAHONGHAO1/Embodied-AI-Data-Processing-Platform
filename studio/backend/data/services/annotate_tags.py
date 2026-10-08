"""P0 hardcoded atomic action vocabulary (driven by behavior_tags table in P1)."""

from __future__ import annotations

from collections.abc import Mapping

ACTION_TAGS: list[dict[str, str]] = [
    {"action": "approach", "label": "接近", "color": "#8c8c8c"},
    {"action": "pick", "label": "抓取", "color": "#1677ff"},
    {"action": "place", "label": "放置", "color": "#fa8c16"},
    {"action": "hold", "label": "持握", "color": "#722ed1"},
    {"action": "push_pull", "label": "推拉", "color": "#722ed1"},
    {"action": "rotate", "label": "旋转", "color": "#13c2c2"},
    {"action": "press", "label": "按压", "color": "#eb2f96"},
    {"action": "wipe", "label": "擦拭", "color": "#a0d911"},
    {"action": "deliver", "label": "递送", "color": "#52c41a"},
    {"action": "open_door", "label": "开门", "color": "#2f54eb"},
    {"action": "fold", "label": "折叠", "color": "#faad14"},
    {"action": "other", "label": "其他", "color": "#8c8c8c"},
]

# Transition compatibility: key = action
BEHAVIOR_TAGS: list[dict[str, str]] = [
    {"key": t["action"], "label": t["label"], "color": t["color"], **t} for t in ACTION_TAGS
]

SUCCESS_OPTIONS = [
    {"value": 1, "label": "成功", "legacy_type": "success"},
    {"value": 0, "label": "失败", "legacy_type": "failure"},
    {"value": -1, "label": "无法判断", "legacy_type": "unknown"},
]

QUALITY_OPTIONS = [
    {"value": 1, "label": "质量良好"},
    {"value": 0, "label": "质量较差"},
]

# Legacy type dropdown (compatibility)
TYPE_OPTIONS = [
    {"value": "success", "label": "成功"},
    {"value": "failure", "label": "失败"},
    {"value": "unknown", "label": "未知"},
]

ACTION_BY_KEY = {t["action"]: t for t in ACTION_TAGS}
TAG_BY_KEY = ACTION_BY_KEY
LABEL_BY_KEY = {t["action"]: t["label"] for t in ACTION_TAGS}
LABEL_TO_KEY = {t["label"]: t["action"] for t in ACTION_TAGS}

TYPE_LABEL_BY_VALUE = {o["value"]: o["label"] for o in TYPE_OPTIONS}
TYPE_ALIASES = {"fail": "failure"}
SUCCESS_VALUES = {o["value"] for o in SUCCESS_OPTIONS}
QUALITY_VALUES = {o["value"] for o in QUALITY_OPTIONS}

DEFAULT_FEATURE_FLAGS = {
    "action_chart": True,
    "depth_view": True,
    "keyframes": False,
    "qwen_vl_auto_annotate": False,
    "sam_auto_bbox": False,
    "manual_bbox": False,
}


def normalize_type_value(type_value: str | None) -> str:
    """Normalize type enum (compatible with frontend fail -> failure)."""
    value = (type_value or "success").strip()
    value = TYPE_ALIASES.get(value, value)
    if value not in TYPE_LABEL_BY_VALUE:
        return "success"
    return value


def normalize_success_value(value: int | str | None) -> int:
    if value is None:
        return 1
    if isinstance(value, str):
        from data.services.annotate_schema import TYPE_TO_SUCCESS

        if value in TYPE_TO_SUCCESS:
            return TYPE_TO_SUCCESS[value]
        try:
            value = int(value)
        except (TypeError, ValueError):
            return 1
    if value in SUCCESS_VALUES:
        return int(value)
    return 1


def normalize_quality_value(value: int | str | None) -> int:
    if value is None:
        return 1
    try:
        v = int(value)
    except (TypeError, ValueError):
        return 1
    return v if v in QUALITY_VALUES else 1


def success_to_type(success: int) -> str:
    from data.services.annotate_schema import SUCCESS_TO_TYPE

    return SUCCESS_TO_TYPE.get(normalize_success_value(success), "unknown")


def type_label(type_value: str | None) -> str:
    return TYPE_LABEL_BY_VALUE.get(normalize_type_value(type_value), "成功")


def resolve_action_key(
    raw: str,
    *,
    action_by_key: Mapping[str, object] | None = None,
    label_by_key: Mapping[str, str] | None = None,
    label_to_key: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Parse skill key and display label from stored or submitted action string."""
    actions = ACTION_BY_KEY if action_by_key is None else action_by_key
    labels = LABEL_BY_KEY if label_by_key is None else label_by_key
    keys_by_label = LABEL_TO_KEY if label_to_key is None else label_to_key
    text = (raw or "").strip()
    if not text:
        return "other", action_label(
            "other",
            action_by_key=actions,
            label_by_key=labels,
        )
    if text in actions:
        return text, labels[text]
    if "|" in text:
        left, right = text.split("|", 1)
        left, right = left.strip(), right.strip()
        for key in actions:
            if labels[key] != left:
                continue
            abbrev = key[:2] if len(key) > 2 else key
            if right in (abbrev, key) or key.startswith(right):
                return key, labels[key]
        return "other", left
    if text in keys_by_label:
        key = keys_by_label[text]
        return key, labels[key]
    if text in actions:
        return text, labels[text]
    return "other", text


# Transition alias
resolve_behavior_tag = resolve_action_key


def action_label(
    action_key: str,
    *,
    action_by_key: Mapping[str, object] | None = None,
    label_by_key: Mapping[str, str] | None = None,
) -> str:
    actions = ACTION_BY_KEY if action_by_key is None else action_by_key
    labels = LABEL_BY_KEY if label_by_key is None else label_by_key
    if action_key in actions:
        return labels[action_key]
    if "|" in action_key:
        return action_key.split("|", 1)[0]
    return action_key


tag_label = action_label


def annotation_item_fields(
    seg: dict,
    *,
    action_by_key: Mapping[str, object] | None = None,
    label_by_key: Mapping[str, str] | None = None,
    label_to_key: Mapping[str, str] | None = None,
) -> dict:
    """Generate derived display fields (compatible with legacy frontend)."""
    uses_explicit_vocabulary = (
        action_by_key is not None and label_by_key is not None and label_to_key is not None
    )
    actions = ACTION_BY_KEY if action_by_key is None else action_by_key
    action_key, label = resolve_action_key(
        str(seg.get("action") or seg.get("tag") or ""),
        action_by_key=actions,
        label_by_key=label_by_key,
        label_to_key=label_to_key,
    )
    stored_label = (seg.get("tag_label") or "").strip()
    if stored_label and action_key in actions and not uses_explicit_vocabulary:
        label = stored_label
    success = normalize_success_value(seg.get("success"))
    if "type" in seg and "success" not in seg:
        from data.services.annotate_schema import TYPE_TO_SUCCESS

        success = TYPE_TO_SUCCESS.get(normalize_type_value(seg.get("type")), success)
    type_value = success_to_type(success)
    quality = normalize_quality_value(seg.get("quality", 1))
    return {
        "tag": action_key,
        "tag_label": label,
        "type": type_value,
        "action": action_key,
        "action_cn": label,
        "success": success,
        "quality": quality,
        "state": type_label(type_value),
        "subtask": str(seg.get("subtask") or seg.get("remark") or ""),
    }


def tag_display(action_key: str) -> str:
    entry = ACTION_BY_KEY.get(action_key)
    if not entry:
        return action_key
    abbrev = action_key[:2] if len(action_key) > 2 else action_key
    return f"{entry['label']}|{abbrev}"


def enrich_region_frames_from_segments(
    segments: dict[str, dict],
    fps: float,
    *,
    action_by_key: Mapping[str, object] | None = None,
    label_by_key: Mapping[str, str] | None = None,
    label_to_key: Mapping[str, str] | None = None,
) -> list[dict]:
    """Convert clip_descriptions.segments -> transition region_frames list (including timing fields)."""
    items: list[dict] = []
    for seg in segments.values():
        start = int(seg["start_frame"])
        end = int(seg["end_frame"])
        fields = annotation_item_fields(
            seg,
            action_by_key=action_by_key,
            label_by_key=label_by_key,
            label_to_key=label_to_key,
        )
        items.append(
            {
                "id": seg.get("id"),
                "start_frame": start,
                "end_frame": end,
                **fields,
                "remark": fields["subtask"],
                "start_time": round(start / fps, 2) if fps > 0 else 0.0,
                "end_time": round(end / fps, 2) if fps > 0 else 0.0,
            }
        )
    items.sort(key=lambda x: (x["start_frame"], x["end_frame"]))
    return items


def export_actions_jsonl_lines() -> list[str]:
    """Export meta/actions.jsonl line content."""
    import json

    return [
        json.dumps(
            {"action": t["action"], "label": t["label"], "color": t["color"]}, ensure_ascii=False
        )
        for t in ACTION_TAGS
    ]
