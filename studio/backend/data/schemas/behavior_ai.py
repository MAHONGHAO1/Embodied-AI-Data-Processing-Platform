"""Contracts shared by behavior AI suggestion workflows."""

from pydantic import BaseModel, Field

BEHAVIOR_AI_JOB_KIND = "behavior_ai_suggestion"
BEHAVIOR_AI_QUEUE = "ai"
BEHAVIOR_AI_RESULT_SCHEMA = "quicdata.behavior-ai-suggestion.v1"


class BehaviorAiSuggestionCreateRequest(BaseModel):
    rgb_topic: str = Field(min_length=1, max_length=512)


class BehaviorAiSuggestionSource(BaseModel):
    job_id: str = Field(min_length=1, max_length=64)
    proposal_ids: list[str] = Field(min_length=1, max_length=256)
    input_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
