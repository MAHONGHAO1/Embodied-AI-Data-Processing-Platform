from __future__ import annotations

import secrets
from enum import StrEnum


class JobState(StrEnum):
    VALIDATING = "VALIDATING"
    QUEUED = "QUEUED"
    SUBMITTING = "SUBMITTING"
    ORPHANED = "ORPHANED"
    PROVISIONING = "PROVISIONING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"


class AttemptState(StrEnum):
    PENDING = "PENDING"
    SUBMITTING = "SUBMITTING"
    ORPHANED = "ORPHANED"
    PROVISIONING = "PROVISIONING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"


JOB_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.VALIDATING: {JobState.QUEUED, JobState.FAILED, JobState.CANCELLED},
    JobState.QUEUED: {JobState.SUBMITTING, JobState.CANCELLED},
    JobState.SUBMITTING: {
        JobState.ORPHANED,
        JobState.PROVISIONING,
        JobState.FAILED,
        JobState.CANCEL_REQUESTED,
    },
    JobState.ORPHANED: {
        JobState.PROVISIONING,
        JobState.SUBMITTING,
        JobState.FAILED,
        JobState.CANCEL_REQUESTED,
    },
    JobState.PROVISIONING: {
        JobState.RUNNING,
        JobState.ORPHANED,
        JobState.FAILED,
        JobState.CANCEL_REQUESTED,
    },
    JobState.RUNNING: {
        JobState.SUCCEEDED,
        JobState.ORPHANED,
        JobState.FAILED,
        JobState.CANCEL_REQUESTED,
    },
    JobState.CANCEL_REQUESTED: {JobState.CANCELLED, JobState.FAILED},
    JobState.SUCCEEDED: set(),
    JobState.FAILED: set(),
    JobState.CANCELLED: set(),
}


class StateTransitionError(ValueError):
    pass


def transition_allowed(old: JobState | str, new: JobState | str) -> bool:
    return JobState(new) in JOB_TRANSITIONS[JobState(old)]


def require_transition(old: JobState | str, new: JobState | str) -> None:
    if not transition_allowed(old, new):
        raise StateTransitionError(f"Illegal Job transition: {old} -> {new}")


def new_id(prefix: str) -> str:
    """Create an opaque, URL-safe identifier without leaking database sequences."""
    return f"{prefix}_{secrets.token_hex(10)}"
