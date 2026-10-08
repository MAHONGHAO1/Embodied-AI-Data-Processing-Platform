"""Validate the human-use provenance attached to behavior AI suggestions."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from data.database import JOB_STATUS_SUCCEEDED, JobRun
from data.schemas.behavior_ai import (
    BEHAVIOR_AI_JOB_KIND,
    BEHAVIOR_AI_RESULT_SCHEMA,
    BehaviorAiSuggestionSource,
)


class BehaviorAiProvenanceError(ValueError):
    """The submitted AI provenance is not owned by this human annotation."""

    def __init__(self) -> None:
        super().__init__("ai_suggestion_source_invalid")


def validate_behavior_ai_provenance(
    db: Session,
    *,
    sources: Sequence[BehaviorAiSuggestionSource],
    resource_type: Literal["task", "ego_episode"],
    resource_id: str,
    workspace_id: int,
    actor_id: int,
) -> list[dict[str, object]]:
    """Return an allowlisted, deduplicated provenance summary or reject it.

    A browser can only claim that a human used suggestions which were produced
    for the same actor and annotation target. Provider payloads stay inside the
    JobRun; only stable identifiers and the immutable input fingerprint are
    persisted with the human annotation.
    """

    verified: list[dict[str, object]] = []
    seen: set[tuple[str, tuple[str, ...], str]] = set()
    for source in sources:
        proposal_ids = tuple(sorted(set(source.proposal_ids)))
        source_key = (source.job_id, proposal_ids, source.input_fingerprint)
        if source_key in seen:
            continue

        job = db.scalar(select(JobRun).where(JobRun.id == source.job_id).with_for_update())
        if not _matches_target(
            job,
            resource_type=resource_type,
            resource_id=resource_id,
            workspace_id=workspace_id,
            actor_id=actor_id,
            input_fingerprint=source.input_fingerprint,
        ):
            raise BehaviorAiProvenanceError()

        result = job.result_json if isinstance(job.result_json, dict) else {}
        proposal_set = _proposal_ids(result)
        if (
            result.get("schema") != BEHAVIOR_AI_RESULT_SCHEMA
            or result.get("input_fingerprint") != source.input_fingerprint
            or not proposal_ids
            or not set(proposal_ids).issubset(proposal_set)
        ):
            raise BehaviorAiProvenanceError()

        verified.append(
            {
                "job_id": source.job_id,
                "proposal_ids": list(proposal_ids),
                "input_fingerprint": source.input_fingerprint,
            }
        )
        seen.add(source_key)
    return verified


def _matches_target(
    job: JobRun | None,
    *,
    resource_type: str,
    resource_id: str,
    workspace_id: int,
    actor_id: int,
    input_fingerprint: str,
) -> bool:
    if (
        job is None
        or job.kind != BEHAVIOR_AI_JOB_KIND
        or job.status != JOB_STATUS_SUCCEEDED
        or job.resource_type != resource_type
        or job.resource_id != resource_id
        or job.actor_id != actor_id
    ):
        return False
    detail = job.detail_json if isinstance(job.detail_json, dict) else {}
    return (
        detail.get("workspace_id") == workspace_id
        and detail.get("input_fingerprint") == input_fingerprint
    )


def _proposal_ids(result: dict[str, object]) -> set[str]:
    segments = result.get("segments")
    if not isinstance(segments, list):
        return set()
    return {
        proposal_id
        for segment in segments
        if isinstance(segment, dict)
        and isinstance((proposal_id := segment.get("proposal_id")), str)
        and proposal_id
    }
