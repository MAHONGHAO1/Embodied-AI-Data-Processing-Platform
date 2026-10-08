"""Shared behavior-annotation option payloads for native workbench endpoints."""

from __future__ import annotations

from sqlalchemy.orm import Session

from data.services.annotate_tags import QUALITY_OPTIONS, SUCCESS_OPTIONS
from data.services.behavior_tags_service import (
    BehaviorVocabularySnapshot,
    get_active_vocabulary_snapshot,
)


def build_behavior_annotation_options(
    db: Session,
    *,
    vocabulary: BehaviorVocabularySnapshot | None = None,
) -> dict[str, list[dict[str, object]]]:
    """Return the common UI options without coupling EGO to annotate permissions."""
    vocabulary = vocabulary or get_active_vocabulary_snapshot(db)
    return {
        "action_options": vocabulary.action_options(),
        "action_kind_options": [
            {"value": "standard", "label": "标准动作"},
            {"value": "custom", "label": "自定义动作"},
        ],
        "success_options": [dict(item) for item in SUCCESS_OPTIONS],
        "quality_options": [dict(item) for item in QUALITY_OPTIONS],
    }
