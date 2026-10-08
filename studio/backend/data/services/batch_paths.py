"""Canonical object-storage prefixes for the Batch / Episode domain."""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def _positive_id(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _token(value: str, field: str) -> str:
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        raise ValueError(f"{field} must be a server-generated token")
    return value


def _batch_scope(*, workspace_id: int, task_set_id: int, batch_id: int) -> str:
    workspace_id = _positive_id(workspace_id, "workspace_id")
    task_set_id = _positive_id(task_set_id, "task_set_id")
    batch_id = _positive_id(batch_id, "batch_id")
    return f"workspaces/{workspace_id}/task-sets/{task_set_id}/batches/{batch_id}"


def raw_import_original_prefix(
    *,
    workspace_id: int,
    task_set_id: int,
    batch_id: int,
    import_session_id: str,
    artifact_id: int,
) -> str:
    scope = _batch_scope(workspace_id=workspace_id, task_set_id=task_set_id, batch_id=batch_id)
    import_session_id = _token(import_session_id, "import_session_id")
    artifact_id = _positive_id(artifact_id, "artifact_id")
    return f"raw/v2/{scope}/imports/{import_session_id}/original/{artifact_id}"


def raw_episode_source_prefix(
    *,
    workspace_id: int,
    task_set_id: int,
    batch_id: int,
    episode_id: int,
    artifact_id: int,
) -> str:
    scope = _batch_scope(workspace_id=workspace_id, task_set_id=task_set_id, batch_id=batch_id)
    episode_id = _positive_id(episode_id, "episode_id")
    artifact_id = _positive_id(artifact_id, "artifact_id")
    return f"raw/v2/{scope}/episodes/{episode_id}/source/{artifact_id}"


def process_episode_run_prefix(
    *,
    workspace_id: int,
    task_set_id: int,
    batch_id: int,
    episode_id: int,
    job_id: str,
) -> str:
    scope = _batch_scope(workspace_id=workspace_id, task_set_id=task_set_id, batch_id=batch_id)
    episode_id = _positive_id(episode_id, "episode_id")
    job_id = _token(job_id, "job_id")
    return f"process/v2/{scope}/episodes/{episode_id}/runs/{job_id}"


def official_episode_publication_prefix(
    *,
    workspace_id: int,
    task_set_id: int,
    episode_id: int,
    publication_id: str,
) -> str:
    workspace_id = _positive_id(workspace_id, "workspace_id")
    task_set_id = _positive_id(task_set_id, "task_set_id")
    episode_id = _positive_id(episode_id, "episode_id")
    publication_id = _token(publication_id, "publication_id")
    return (
        f"official/v2/workspaces/{workspace_id}/task-sets/{task_set_id}/episodes/"
        f"{episode_id}/publications/{publication_id}"
    )


def export_revision_prefix(*, workspace_id: int, dataset_id: int, revision_id: int) -> str:
    workspace_id = _positive_id(workspace_id, "workspace_id")
    dataset_id = _positive_id(dataset_id, "dataset_id")
    revision_id = _positive_id(revision_id, "revision_id")
    return f"exports/v1/workspaces/{workspace_id}/datasets/{dataset_id}/revisions/{revision_id}"
