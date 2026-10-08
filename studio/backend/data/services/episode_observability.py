"""Safe artifact projections for the Episode console.

These views deliberately model operational state rather than storage facts.
Object locations, checksums, job diagnostics, and artifact metadata remain
server-only evidence and are available only through the narrowly authorized
preview/download paths.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from data.database import Episode, EpisodeArtifact
from data.services.public_metadata import serialize_public_packet_metadata
from data.services.workspace_access import require_episode_actor
from data.utils.formatting import format_api_datetime


def episode_assets_projection(
    db: Session,
    *,
    episode: Episode,
    actor_id: int | None,
) -> dict[str, object]:
    """Return safe artifact metadata for an authorized Episode reader."""
    require_episode_actor(db, actor_id=actor_id, episode=episode)
    artifacts = (
        db.query(EpisodeArtifact)
        .filter(EpisodeArtifact.episode_id == episode.id)
        .order_by(EpisodeArtifact.created_at.asc(), EpisodeArtifact.id.asc())
        .all()
    )
    return {
        "items": [
            {
                "id": artifact.id,
                "artifact_type": artifact.artifact_type,
                "storage_role": artifact.storage_role,
                "size_bytes": max(0, int(artifact.size_bytes or 0)),
                "retention_policy": artifact.retention_policy,
                "retention_until": format_api_datetime(artifact.retention_until)
                if artifact.retention_until
                else None,
            }
            for artifact in artifacts
        ],
        "packet_metadata": serialize_public_packet_metadata(episode.metadata_json),
    }
