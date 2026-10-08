"""Safe, bounded candidates for building one immutable DatasetRevision."""

from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session, selectinload

from data.database import Episode, EpisodeArtifact, PublishedEpisode, TaskSet
from data.services.dataset_revisions import get_dataset
from data.services.episode_asset_projection import episode_asset_states
from data.services.workspace_access import require_workspace_actor

logger = logging.getLogger(__name__)

MAX_REVISION_CANDIDATES = 1000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CandidateLimitExceeded(ValueError):
    def __init__(self, limit: int = MAX_REVISION_CANDIDATES) -> None:
        super().__init__("dataset revision candidate limit exceeded")
        self.limit = limit

    def as_detail(self) -> dict[str, object]:
        return {"code": "candidate_limit_exceeded", "limit": self.limit}


def dataset_revision_candidates(
    db: Session,
    *,
    dataset_id: int,
    task_set_id: int | None = None,
    actor_id: int,
) -> dict[str, object]:
    dataset = get_dataset(db, dataset_id)
    require_workspace_actor(db, actor_id=actor_id, workspace_id=dataset.workspace_id)
    task_set = None
    if task_set_id is not None:
        task_set = (
            db.query(TaskSet)
            .filter(TaskSet.id == task_set_id, TaskSet.workspace_id == dataset.workspace_id)
            .one_or_none()
        )
        if task_set is None:
            raise ValueError("task set does not exist")

    row_query = (
        db.query(PublishedEpisode, Episode, EpisodeArtifact)
        .join(Episode, PublishedEpisode.episode_id == Episode.id)
        .join(EpisodeArtifact, PublishedEpisode.official_artifact_id == EpisodeArtifact.id)
        .options(selectinload(Episode.task_label))
        .filter(Episode.workspace_id == dataset.workspace_id)
    )
    episode_query = db.query(Episode).filter(Episode.workspace_id == dataset.workspace_id)
    if task_set is not None:
        row_query = row_query.filter(Episode.task_set_id == task_set.id)
        episode_query = episode_query.filter(Episode.task_set_id == task_set.id)
    rows = row_query.order_by(Episode.created_at.desc(), Episode.id.desc()).all()
    all_episodes = episode_query.all()
    episodes_by_id = {episode.id: episode for episode in all_episodes}

    eligible: list[tuple[Episode, Episode, EpisodeArtifact]] = []
    for _published, episode, artifact in rows:
        root = _root_source(episode, episodes_by_id)
        if root is None or not _official_artifact_valid(artifact):
            logger.warning(
                "dataset revision candidate rejected dataset_id=%s episode_id=%s reason=%s",
                dataset.id,
                episode.id,
                "lineage_invalid" if root is None else "official_artifact_invalid",
            )
            continue
        eligible.append((root, episode, artifact))

    if len(eligible) > MAX_REVISION_CANDIDATES:
        raise CandidateLimitExceeded()

    states = episode_asset_states(db, episodes=[episode for _root, episode, _artifact in eligible])
    previews = {
        int(episode_id)
        for (episode_id,) in db.query(EpisodeArtifact.episode_id)
        .filter(
            EpisodeArtifact.episode_id.in_([episode.id for _root, episode, _artifact in eligible]),
            EpisodeArtifact.artifact_type == "process_preview",
            EpisodeArtifact.storage_role == "process",
        )
        .all()
        if episode_id is not None
    }

    grouped: dict[int, dict[str, object]] = {}
    for root, episode, _artifact in eligible:
        group = grouped.setdefault(
            root.id,
            {
                "root": root,
                "source": _source_projection(root),
                "candidates": [],
            },
        )
        group["candidates"].append(
            _candidate_projection(
                episode,
                state=states.get(episode.id, {}),
                preview_available=episode.id in previews,
                root=root,
            )
        )

    ordered_groups = sorted(
        grouped.values(),
        key=lambda group: (group["root"].created_at, group["root"].id),
        reverse=True,
    )
    groups: list[dict[str, object]] = []
    for group in ordered_groups:
        candidates = sorted(
            group["candidates"],
            key=lambda item: (
                0 if item["candidate_kind"] == "full_source" else 1,
                _optional_int(item["source_start_ns"], default=-1),
                int(item["episode_id"]),
            ),
        )
        groups.append({"source": group["source"], "candidates": candidates})

    candidate_count = sum(len(group["candidates"]) for group in groups)
    duration_s = round(
        sum(
            float(candidate.get("duration_s") or 0.0)
            for group in groups
            for candidate in group["candidates"]
        ),
        3,
    )
    return {
        "dataset_id": int(dataset.id),
        "workspace_id": int(dataset.workspace_id),
        "task_set_id": int(task_set.id) if task_set is not None else None,
        "summary": {
            "candidate_count": candidate_count,
            "source_count": len(groups),
            "duration_s": duration_s,
        },
        "groups": groups,
    }


def _root_source(episode: Episode, episodes_by_id: dict[int, Episode]) -> Episode | None:
    current = episode
    seen: set[int] = set()
    for _depth in range(32):
        if current.id in seen:
            return None
        seen.add(current.id)
        if current.kind == "source":
            return current
        if current.parent_episode_id is None:
            return None
        parent = episodes_by_id.get(current.parent_episode_id)
        if (
            parent is None
            or parent.workspace_id != episode.workspace_id
            or parent.task_set_id != episode.task_set_id
        ):
            return None
        current = parent
    return None


def _official_artifact_valid(artifact: EpisodeArtifact) -> bool:
    return bool(
        artifact.storage_role == "official"
        and artifact.artifact_type == "official_qrdf"
        and artifact.storage_uri
        and _SHA256.fullmatch(str(artifact.checksum_sha256 or "").lower())
    )


def _source_projection(source: Episode) -> dict[str, object]:
    start_ns, end_ns = _timeline_bounds(source)
    return {
        "id": int(source.id),
        "task_set_id": int(source.task_set_id),
        "episode_uid": source.episode_uid,
        "modality": source.modality,
        "duration_s": _duration_s(source),
        "timeline_available": start_ns is not None and end_ns is not None,
        "timeline_start_ns": str(start_ns) if start_ns is not None else None,
        "timeline_end_ns": str(end_ns) if end_ns is not None else None,
        "created_at": source.created_at.isoformat() if source.created_at is not None else None,
    }


def _candidate_projection(
    episode: Episode,
    *,
    state: dict[str, object],
    preview_available: bool,
    root: Episode,
) -> dict[str, object]:
    attribution = state.get("attribution") if isinstance(state, dict) else None
    attribution = attribution if isinstance(attribution, dict) else {}
    collector_state = (
        attribution.get("collector") if isinstance(attribution.get("collector"), dict) else {}
    )
    device_state = attribution.get("device") if isinstance(attribution.get("device"), dict) else {}
    collector = (
        collector_state.get("collector")
        if isinstance(collector_state.get("collector"), dict)
        else None
    )
    device = device_state.get("device") if isinstance(device_state.get("device"), dict) else None
    task_label = episode.task_label
    source_start_ns = episode.source_start_ns
    source_end_ns = episode.source_end_ns
    if episode.kind == "source":
        source_start_ns, source_end_ns = _timeline_bounds(root)
    return {
        "episode_id": int(episode.id),
        "task_set_id": int(episode.task_set_id),
        "episode_uid": episode.episode_uid,
        "candidate_kind": "full_source" if episode.kind == "source" else "derived",
        "kind": episode.kind,
        "modality": episode.modality,
        "source_start_ns": str(source_start_ns) if source_start_ns is not None else None,
        "source_end_ns": str(source_end_ns) if source_end_ns is not None else None,
        "duration_s": _duration_s(episode),
        "task_label": (
            {"id": int(task_label.id), "name": task_label.name} if task_label is not None else None
        ),
        "collector": collector,
        "device": device,
        "preview_available": bool(preview_available),
        "created_at": episode.created_at.isoformat() if episode.created_at is not None else None,
    }


def _timeline_bounds(episode: Episode) -> tuple[int | None, int | None]:
    metadata = episode.metadata_json if isinstance(episode.metadata_json, dict) else {}
    timing = metadata.get("timing") if isinstance(metadata.get("timing"), dict) else {}
    start = _optional_int(timing.get("start_timestamp_ns"))
    end = _optional_int(timing.get("end_timestamp_ns"))
    if start is None or end is None or start < 0 or end <= start:
        return None, None
    return start, end


def _duration_s(episode: Episode) -> float:
    metadata = episode.metadata_json if isinstance(episode.metadata_json, dict) else {}
    metrics = metadata.get("metrics") if isinstance(metadata.get("metrics"), dict) else {}
    value = metrics.get("duration_s")
    try:
        duration = float(value)
    except (TypeError, ValueError):
        duration = 0.0
    if duration > 0:
        return round(duration, 3)
    start, end = _timeline_bounds(episode)
    return round((end - start) / 1e9, 3) if start is not None and end is not None else 0.0


def _optional_int(value: object, *, default: int | None = None) -> int | None:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
