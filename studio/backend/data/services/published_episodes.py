"""Read-only, workspace-scoped projections of published QRDF Episodes."""

from __future__ import annotations

from sqlalchemy.orm import Session

from data.database import QrdfEpisode
from data.utils.formatting import format_api_datetime


def list_published_qrdf_episodes(
    db: Session,
    *,
    workspace_id: int,
    page: int,
    size: int,
) -> tuple[int, list[QrdfEpisode]]:
    """Return only immutable Episodes materialized by the publication workflow."""
    query = db.query(QrdfEpisode).filter(
        QrdfEpisode.workspace_id == workspace_id,
        QrdfEpisode.status == "published",
    )
    total = query.count()
    items = (
        query.order_by(QrdfEpisode.published_at.desc(), QrdfEpisode.id.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    return total, items


def serialize_published_qrdf_episode(episode: QrdfEpisode) -> dict[str, object]:
    """Expose only fields needed to browse a published Episode candidate."""
    return {
        "id": episode.id,
        "qrdf_id": episode.qrdf_id,
        "subject_type": episode.subject_type,
        "status": episode.status,
        "output_profile": episode.output_profile,
        "published_at": format_api_datetime(episode.published_at),
    }
