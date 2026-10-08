"""Data annotation module P0 schemas (compatibility for clip_descriptions and region_frames)."""

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from data.schemas.behavior_ai import BehaviorAiSuggestionSource
from data.services.annotate_tags import (
    normalize_type_value,
)


class RegionFrameIdRequest(BaseModel):
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)


class RegionFrameSubmitItem(BaseModel):
    """Transition period region_frames input parameters."""

    id: str
    tag: str = ""
    action: str = ""
    type: str = "success"
    success: int | None = None
    quality: int | None = None
    start: int | None = None
    end: int | None = None
    start_frame: int | None = None
    end_frame: int | None = None
    remark: str = ""
    subtask: str = ""
    bbox: list[Any] = Field(default_factory=list)

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        normalized = normalize_type_value(v)
        allowed = {"success", "failure", "unknown"}
        if normalized not in allowed:
            raise ValueError(f"type 必须为 {allowed}")
        return normalized

    def resolved_start(self) -> int:
        if self.start_frame is not None:
            return self.start_frame
        if self.start is not None:
            return self.start
        raise ValueError("缺少 start/start_frame")

    def resolved_end(self) -> int:
        if self.end_frame is not None:
            return self.end_frame
        if self.end is not None:
            return self.end
        raise ValueError("缺少 end/end_frame")

    def to_segment_dict(self) -> dict[str, Any]:
        success = self.success
        if success is None:
            from data.services.annotate_schema import TYPE_TO_SUCCESS

            success = TYPE_TO_SUCCESS.get(self.type, 1)
        return {
            "id": self.id,
            "start_frame": self.resolved_start(),
            "end_frame": self.resolved_end(),
            "action": self.action or self.tag,
            "tag": self.tag or self.action,
            "type": self.type,
            "success": success,
            "quality": self.quality if self.quality is not None else 1,
            "subtask": self.subtask or self.remark,
            "remark": self.remark or self.subtask,
            "bbox": self.bbox,
        }


class SegmentSubmitItem(BaseModel):
    """MVP clip_descriptions.segments value."""

    id: str
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)
    action: str
    success: int = 1
    quality: int = 1
    subtask: str = ""

    @field_validator("success")
    @classmethod
    def validate_success(cls, v: int) -> int:
        if v not in {1, 0, -1}:
            raise ValueError(f"success 非法: {v}")
        return v

    @field_validator("quality")
    @classmethod
    def validate_quality(cls, v: int) -> int:
        if v not in {0, 1}:
            raise ValueError(f"quality 非法: {v}")
        return v


class ClipDescriptionsSubmit(BaseModel):
    segments: dict[str, SegmentSubmitItem | dict[str, Any]]


class EpisodeSummarySubmit(BaseModel):
    success: int = 1
    quality: int = 1
    language: str = ""


class AnnotateSubmitRequest(BaseModel):
    task_id: int | None = None
    episode_id: str | None = None
    action: str | None = None
    task: str | None = None
    clip_descriptions: ClipDescriptionsSubmit | None = None
    episode_summary: EpisodeSummarySubmit | dict[str, Any] | None = None
    region_frames: list[RegionFrameSubmitItem] | None = None
    keyframes: list[dict[str, Any]] | None = None  # Reserved for P1, ignored in P0
    ai_suggestion_sources: list[BehaviorAiSuggestionSource] = Field(
        default_factory=list, max_length=256
    )

    @model_validator(mode="after")
    def require_segments(self) -> "AnnotateSubmitRequest":
        if not self.clip_descriptions and not self.region_frames:
            raise ValueError("clip_descriptions 或 region_frames 至少提供一项")
        return self

    def resolved_segment_items(self) -> list[dict[str, Any]]:
        """Normalize to a list of segment dicts (including start_frame/end_frame)."""
        if self.clip_descriptions and self.clip_descriptions.segments:
            items: list[dict[str, Any]] = []
            for key, raw in self.clip_descriptions.segments.items():
                if isinstance(raw, SegmentSubmitItem):
                    seg = raw.model_dump()
                else:
                    seg = dict(raw)
                if "start_frame" not in seg and "_" in key:
                    parts = key.split("_", 1)
                    if len(parts) == 2:
                        seg.setdefault("start_frame", int(parts[0]))
                        seg.setdefault("end_frame", int(parts[1]))
                start = int(seg.get("start_frame", seg.get("start", 0)))
                end = int(seg.get("end_frame", seg.get("end", 0)))
                from data.services.annotate_schema import segment_key

                expected = segment_key(start, end)
                if key != expected:
                    raise ValueError(f"segment 键 {key} 与帧区间 {expected} 不一致")
                items.append(seg)
            return items
        if self.region_frames:
            return [rf.to_segment_dict() for rf in self.region_frames]
        return []


class BehaviorTagCreateRequest(BaseModel):
    tag_key: str = Field(min_length=1, max_length=64)
    tag_label: str = Field(min_length=1, max_length=128)
    color: str = Field(default="#8c8c8c", max_length=16)
    sort_order: int | None = None
    action: str = Field(default="", max_length=64)
    label: str = Field(default="", max_length=128)

    def resolved_key(self) -> str:
        return (self.tag_key or self.action or "").strip()

    def resolved_label(self) -> str:
        return (self.tag_label or self.label or "").strip()


class BehaviorTagUpdateRequest(BaseModel):
    tag_label: str | None = Field(default=None, max_length=128)
    label: str | None = Field(default=None, max_length=128)
    color: str | None = Field(default=None, max_length=16)
    sort_order: int | None = None
    is_active: bool | None = None

    def resolved_label(self) -> str | None:
        if self.tag_label is not None:
            return self.tag_label
        return self.label
