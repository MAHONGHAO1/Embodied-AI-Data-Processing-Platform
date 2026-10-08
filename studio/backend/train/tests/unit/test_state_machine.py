import pytest
from quictrain_core import JobState, StateTransitionError, transition_allowed
from quictrain_core.domain import require_transition


def test_documented_transition_path_is_valid():
    path = [
        JobState.VALIDATING,
        JobState.QUEUED,
        JobState.SUBMITTING,
        JobState.PROVISIONING,
        JobState.RUNNING,
        JobState.SUCCEEDED,
    ]
    assert all(transition_allowed(old, new) for old, new in zip(path, path[1:], strict=False))


def test_terminal_state_cannot_mutate():
    with pytest.raises(StateTransitionError):
        require_transition(JobState.SUCCEEDED, JobState.RUNNING)


def test_cancel_path_is_explicit():
    assert transition_allowed(JobState.RUNNING, JobState.CANCEL_REQUESTED)
    assert transition_allowed(JobState.CANCEL_REQUESTED, JobState.CANCELLED)


def test_provider_loss_can_orphan_active_jobs():
    assert transition_allowed(JobState.PROVISIONING, JobState.ORPHANED)
    assert transition_allowed(JobState.RUNNING, JobState.ORPHANED)
