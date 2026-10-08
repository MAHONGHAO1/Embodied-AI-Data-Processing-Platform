"""Shared visibility rules for normal Episode asset projections."""

from __future__ import annotations

from sqlalchemy.orm import Query

from data.database import Episode

IMPORT_PLACEHOLDER_WORKFLOW_STATUSES = frozenset({"importing", "import_failed"})


def exclude_import_placeholders(query: Query) -> Query:
    """Exclude durable ingest placeholders from normal asset projections."""
    return query.filter(~Episode.workflow_status.in_(tuple(IMPORT_PLACEHOLDER_WORKFLOW_STATUSES)))


def is_normal_episode_asset(episode: Episode) -> bool:
    """Return whether an Episode belongs in normal asset and aggregate views."""
    return episode.workflow_status not in IMPORT_PLACEHOLDER_WORKFLOW_STATUSES
