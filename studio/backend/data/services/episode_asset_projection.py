"""Bounded, redacted read projections for the Episode asset directory."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.orm import Session, selectinload

from data.database import (
    Episode,
    EpisodeCollectorAttribution,
    EpisodeDeviceAttribution,
    JobRun,
    WorkItem,
)
from data.services.collector_profiles import collector_profile_brief

_ATTRIBUTION_PRIORITY = {
    "machine_reported": 2,
    "offline_declared": 1,
    "offline_curated": 3,
    "online_verified": 4,
}
_ACTIVE_PUBLICATION_STATUSES = frozenset({"queued", "running", "retry_pending"})


def publication_category(episode: Episode, job: JobRun | None) -> str:
    if episode.workflow_status == "published" or (job is not None and job.status == "succeeded"):
        return "published"
    if job is None or job.status == "cancelled":
        return "unpublished"
    if job.status == "failed":
        return "failed"
    if job.status in _ACTIVE_PUBLICATION_STATUSES:
        return "publishing"
    return "unpublished"


def episode_asset_states(
    db: Session,
    *,
    episodes: Iterable[Episode],
) -> dict[int, dict[str, object]]:
    episode_list = list(episodes)
    if not episode_list:
        return {}
    episode_ids = [episode.id for episode in episode_list]
    roots = _root_sources(db, episode_list)
    work_items = _latest_work_items(db, episode_ids)
    publications = _latest_publications(db, episode_ids)
    collector_attributions, device_attributions = _latest_attributions(
        db,
        {root.id for root in roots.values() if root is not None},
    )

    states: dict[int, dict[str, object]] = {}
    for episode in episode_list:
        root = roots.get(episode.id)
        expected_kind = "cut" if episode.kind == "source" else "annotation"
        primary = work_items.get((episode.id, expected_kind))
        review = work_items.get((episode.id, f"review:{expected_kind}"))
        job = publications.get(episode.id)
        states[episode.id] = {
            "human_work": {
                "primary": _work_item_brief(primary),
                "review": _work_item_brief(review),
            },
            "publication": {
                "status": job.status if job is not None else "waiting",
                "category": publication_category(episode, job),
                "job_id": job.id if job is not None else None,
                "retry_allowed": bool(job is not None and job.status == "failed"),
            },
            "attribution": _attribution_projection(
                root,
                collector_attributions.get(root.id) if root is not None else None,
                device_attributions.get(root.id) if root is not None else None,
            ),
        }
    return states


def _root_sources(db: Session, episodes: list[Episode]) -> dict[int, Episode | None]:
    by_id = {episode.id: episode for episode in episodes}
    missing_parent_ids = {
        episode.parent_episode_id
        for episode in episodes
        if episode.parent_episode_id is not None and episode.parent_episode_id not in by_id
    }
    for _depth in range(32):
        if not missing_parent_ids:
            break
        parents = db.query(Episode).filter(Episode.id.in_(missing_parent_ids)).all()
        if not parents:
            break
        by_id.update((parent.id, parent) for parent in parents)
        missing_parent_ids = {
            parent.parent_episode_id
            for parent in parents
            if parent.parent_episode_id is not None and parent.parent_episode_id not in by_id
        }

    result: dict[int, Episode | None] = {}
    for episode in episodes:
        current = episode
        seen: set[int] = set()
        root: Episode | None = None
        for _depth in range(32):
            if current.id in seen:
                break
            seen.add(current.id)
            if current.kind == "source":
                root = current
                break
            if current.parent_episode_id is None:
                break
            parent = by_id.get(current.parent_episode_id)
            if parent is None:
                break
            if (
                parent.workspace_id != episode.workspace_id
                or parent.task_set_id != episode.task_set_id
            ):
                break
            current = parent
        result[episode.id] = root
    return result


def _latest_work_items(db: Session, episode_ids: list[int]) -> dict[tuple[int, str], WorkItem]:
    rows = (
        db.query(WorkItem)
        .filter(WorkItem.episode_id.in_(episode_ids))
        .order_by(WorkItem.created_at.asc(), WorkItem.id.asc())
        .all()
    )
    latest: dict[tuple[int, str], WorkItem] = {}
    for item in rows:
        if item.episode_id is None:
            continue
        if item.kind in {"cut", "annotation"}:
            latest[(item.episode_id, item.kind)] = item
        elif item.kind == "review" and item.review_target_kind in {"cut", "annotation"}:
            latest[(item.episode_id, f"review:{item.review_target_kind}")] = item
    return latest


def _latest_publications(db: Session, episode_ids: list[int]) -> dict[int, JobRun]:
    resource_ids = [str(episode_id) for episode_id in episode_ids]
    rows = (
        db.query(JobRun)
        .filter(
            JobRun.kind == "episode_publish",
            JobRun.resource_type == "episode",
            JobRun.resource_id.in_(resource_ids),
        )
        .order_by(JobRun.created_at.asc(), JobRun.id.asc())
        .all()
    )
    latest: dict[int, JobRun] = {}
    for job in rows:
        try:
            episode_id = int(job.resource_id)
        except (TypeError, ValueError):
            continue
        latest[episode_id] = job
    return latest


def _latest_attributions(
    db: Session,
    root_ids: set[int],
) -> tuple[dict[int, EpisodeCollectorAttribution], dict[int, EpisodeDeviceAttribution]]:
    if not root_ids:
        return {}, {}
    collector_rows = (
        db.query(EpisodeCollectorAttribution)
        .options(selectinload(EpisodeCollectorAttribution.collector_profile))
        .filter(EpisodeCollectorAttribution.root_source_episode_id.in_(root_ids))
        .all()
    )
    device_rows = (
        db.query(EpisodeDeviceAttribution)
        .options(selectinload(EpisodeDeviceAttribution.collection_device))
        .filter(EpisodeDeviceAttribution.root_source_episode_id.in_(root_ids))
        .all()
    )
    return _preferred_attributions(collector_rows), _preferred_attributions(device_rows)


def _preferred_attributions(rows):
    result = {}
    ranking = {}
    for row in rows:
        key = row.root_source_episode_id
        rank = (_ATTRIBUTION_PRIORITY.get(row.source, 0), row.id)
        if key not in ranking or rank > ranking[key]:
            ranking[key] = rank
            result[key] = row
    return result


def _work_item_brief(item: WorkItem | None) -> dict[str, object] | None:
    if item is None:
        return None
    return {"id": item.id, "kind": item.kind, "status": item.status}


def _attribution_projection(
    root: Episode | None,
    collector: EpisodeCollectorAttribution | None,
    device: EpisodeDeviceAttribution | None,
) -> dict[str, object]:
    collector_profile = collector.collector_profile if collector is not None else None
    collection_device = device.collection_device if device is not None else None
    return {
        "collector": {
            "source_episode_id": root.id if root is not None else None,
            "collector": collector_profile_brief(collector_profile)
            if collector_profile is not None
            else None,
            "effective_source": collector.source if collector is not None else "unknown",
            "editable": bool(collector is None or collector.source != "online_verified"),
        },
        "device": {
            "source_episode_id": root.id if root is not None else None,
            "device": (
                {
                    "id": collection_device.id,
                    "name": collection_device.name,
                    "device_type": collection_device.device_type,
                    "model": collection_device.model,
                    "serial_number": collection_device.serial_number,
                }
                if collection_device is not None
                else None
            ),
            "effective_source": device.source if device is not None else "unknown",
            "editable": bool(device is None or device.source != "online_verified"),
        },
    }
