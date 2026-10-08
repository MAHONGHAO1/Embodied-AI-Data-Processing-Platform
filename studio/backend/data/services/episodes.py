"""Episode read-side queries and storage ownership checks."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, String, case, cast, func, literal, or_, select
from sqlalchemy.orm import Session, selectinload

from data.database import (
    Episode,
    EpisodeArtifact,
    EpisodeCollectorAttribution,
    EpisodeDeviceAttribution,
    JobRun,
    PublishedEpisode,
)
from data.models.data_package import DataPackage
from data.services.episode_visibility import exclude_import_placeholders

_ACTIVE_PUBLICATION_STATUSES = ("queued", "running", "retry_pending")
_ASSET_SORT_FIELDS = frozenset(
    {"published_at", "created_at", "updated_at", "episode_uid", "duration"}
)
_SORTABLE_NUMBER_PATTERN = r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$"


def list_episodes(
    db: Session,
    *,
    workspace_id: int,
    task_set_id: int | None = None,
    batch_id: int | None = None,
    collection_project_id: int | None = None,
    modality: str | None = None,
    embodiment_id: int | None = None,
    task_label_id: int | None = None,
    scene: str | None = None,
    workflow_status: str | None = None,
    kind: str | None = None,
    annotation_status: str | None = None,
    review_status: str | None = None,
    publication_status: str | None = None,
) -> list[Episode]:
    """Return an explicit unpaged internal result.

    Public APIs must use ``list_episode_page`` so a caller cannot accidentally
    turn this helper into an unbounded internet response.
    """
    query = _episode_query(
        db,
        workspace_id=workspace_id,
        task_set_id=task_set_id,
        batch_id=batch_id,
        collection_project_id=collection_project_id,
        modality=modality,
        embodiment_id=embodiment_id,
        task_label_id=task_label_id,
        scene=scene,
        workflow_status=workflow_status,
        kind=kind,
        annotation_status=annotation_status,
        review_status=review_status,
        publication_status=publication_status,
    )
    return (
        query.options(selectinload(Episode.task_label))
        .order_by(Episode.created_at.desc(), Episode.id.desc())
        .all()
    )


def list_episode_page(
    db: Session,
    *,
    workspace_id: int,
    limit: int,
    offset: int,
    task_set_id: int | None = None,
    batch_id: int | None = None,
    collection_project_id: int | None = None,
    modality: str | None = None,
    embodiment_id: int | None = None,
    task_label_id: int | None = None,
    scene: str | None = None,
    workflow_status: str | None = None,
    kind: str | None = None,
    annotation_status: str | None = None,
    review_status: str | None = None,
    publication_status: str | None = None,
) -> tuple[list[Episode], int]:
    """Return one stable public page and its tenant-filtered total."""
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError("invalid episode page")
    query = _episode_query(
        db,
        workspace_id=workspace_id,
        task_set_id=task_set_id,
        batch_id=batch_id,
        collection_project_id=collection_project_id,
        modality=modality,
        embodiment_id=embodiment_id,
        task_label_id=task_label_id,
        scene=scene,
        workflow_status=workflow_status,
        kind=kind,
        annotation_status=annotation_status,
        review_status=review_status,
        publication_status=publication_status,
    )
    total = query.count()
    items = (
        query.options(selectinload(Episode.task_label))
        .order_by(Episode.created_at.desc(), Episode.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return items, total


def list_episode_assets_page(
    db: Session,
    *,
    workspace_id: int,
    limit: int,
    offset: int,
    task_set_id: int | None = None,
    collection_project_id: int | None = None,
    modality: str | None = None,
    task_label_id: int | None = None,
    kind: str | None = None,
    keyword: str | None = None,
    collector_profile_id: int | None = None,
    collection_device_id: int | None = None,
    published_from: datetime | None = None,
    published_to: datetime | None = None,
    sort_by: str = "published_at",
    sort_order: str = "desc",
) -> tuple[list[tuple[Episode, PublishedEpisode]], int]:
    """Return one workspace-scoped page of usable, officially published assets."""
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError("invalid episode asset page")
    if sort_by not in _ASSET_SORT_FIELDS or sort_order not in {"asc", "desc"}:
        raise ValueError("invalid episode asset sort")

    query = (
        db.query(Episode, PublishedEpisode)
        .join(PublishedEpisode, PublishedEpisode.episode_id == Episode.id)
        .join(
            EpisodeArtifact,
            (EpisodeArtifact.id == PublishedEpisode.official_artifact_id)
            & (EpisodeArtifact.episode_id == Episode.id),
        )
        .filter(
            Episode.workspace_id == workspace_id,
            EpisodeArtifact.storage_role == "official",
            EpisodeArtifact.artifact_type == "official_qrdf",
            func.length(func.btrim(EpisodeArtifact.storage_uri)) > 0,
            EpisodeArtifact.checksum_sha256.op("~")("^[0-9a-fA-F]{64}$"),
        )
    )
    if task_set_id is not None:
        query = query.filter(Episode.task_set_id == task_set_id)
    if collection_project_id is not None:
        package_ids = select(DataPackage.id).where(
            DataPackage.workspace_id == workspace_id,
            DataPackage.collection_project_id == collection_project_id,
        )
        query = query.filter(Episode.data_package_id.in_(package_ids))
    if modality:
        query = query.filter(Episode.modality == modality)
    if task_label_id is not None:
        query = query.filter(Episode.task_label_id == task_label_id)
    if kind:
        query = query.filter(Episode.kind == kind)
    normalized_keyword = str(keyword or "").strip()
    if normalized_keyword:
        query = query.filter(
            Episode.episode_uid.ilike(f"%{_escape_like(normalized_keyword)}%", escape="\\")
        )
    if published_from is not None:
        query = query.filter(PublishedEpisode.published_at >= published_from)
    if published_to is not None:
        query = query.filter(PublishedEpisode.published_at <= published_to)
    if collector_profile_id is not None or collection_device_id is not None:
        roots = _asset_root_rows(workspace_id)
        query = query.join(roots, roots.c.episode_id == Episode.id)
        if collector_profile_id is not None:
            latest_collector = (
                select(EpisodeCollectorAttribution.collector_profile_id)
                .where(EpisodeCollectorAttribution.root_source_episode_id == roots.c.root_id)
                .order_by(
                    _attribution_priority(EpisodeCollectorAttribution.source).desc(),
                    EpisodeCollectorAttribution.id.desc(),
                )
                .limit(1)
                .correlate(roots)
                .scalar_subquery()
            )
            query = query.filter(latest_collector == collector_profile_id)
        if collection_device_id is not None:
            latest_device = (
                select(EpisodeDeviceAttribution.collection_device_id)
                .where(EpisodeDeviceAttribution.root_source_episode_id == roots.c.root_id)
                .order_by(
                    _attribution_priority(EpisodeDeviceAttribution.source).desc(),
                    EpisodeDeviceAttribution.id.desc(),
                )
                .limit(1)
                .correlate(roots)
                .scalar_subquery()
            )
            query = query.filter(latest_device == collection_device_id)

    total = query.count()
    duration_text = func.jsonb_extract_path_text(Episode.metadata_json, "metrics", "duration_s")
    duration = case(
        (duration_text.op("~")(_SORTABLE_NUMBER_PATTERN), cast(duration_text, Float)),
        else_=None,
    )
    sort_fields = {
        "published_at": PublishedEpisode.published_at,
        "created_at": Episode.created_at,
        "updated_at": Episode.updated_at,
        "episode_uid": Episode.episode_uid,
        "duration": duration,
    }
    direction = "asc" if sort_order == "asc" else "desc"
    primary = getattr(sort_fields[sort_by], direction)()
    stable = getattr(Episode.id, direction)()
    rows = (
        query.options(selectinload(Episode.task_label))
        .order_by(primary, stable)
        .offset(offset)
        .limit(limit)
        .all()
    )
    return rows, total


def _asset_root_rows(workspace_id: int):
    episodes = Episode.__table__
    lineage = (
        select(
            episodes.c.id.label("episode_id"),
            episodes.c.id.label("ancestor_id"),
            episodes.c.parent_episode_id.label("parent_id"),
            episodes.c.kind.label("kind"),
            literal(0).label("depth"),
        )
        .where(episodes.c.workspace_id == workspace_id)
        .cte("asset_lineage", recursive=True)
    )
    parent = episodes.alias("asset_lineage_parent")
    lineage = lineage.union_all(
        select(
            lineage.c.episode_id,
            parent.c.id,
            parent.c.parent_episode_id,
            parent.c.kind,
            lineage.c.depth + 1,
        )
        .join(parent, parent.c.id == lineage.c.parent_id)
        .where(parent.c.workspace_id == workspace_id, lineage.c.depth < 31)
    )
    return (
        select(lineage.c.episode_id, lineage.c.ancestor_id.label("root_id"))
        .where(lineage.c.kind == "source")
        .cte("asset_roots")
    )


def _attribution_priority(source_column):
    return case(
        (source_column == "online_verified", 4),
        (source_column == "offline_curated", 3),
        (source_column == "machine_reported", 2),
        (source_column == "offline_declared", 1),
        else_=0,
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _episode_query(
    db: Session,
    *,
    workspace_id: int,
    task_set_id: int | None,
    batch_id: int | None,
    collection_project_id: int | None,
    modality: str | None,
    embodiment_id: int | None,
    task_label_id: int | None,
    scene: str | None,
    workflow_status: str | None,
    kind: str | None,
    annotation_status: str | None,
    review_status: str | None,
    publication_status: str | None,
):
    query = db.query(Episode).filter(Episode.workspace_id == workspace_id)
    if task_set_id is not None:
        query = query.filter(Episode.task_set_id == task_set_id)
    if batch_id is not None:
        query = query.filter(Episode.batch_id == batch_id)
    if collection_project_id is not None:
        # Collection-scoped Episodes hang off a data package; the legacy
        # task-set/batch scope has no collection project.
        package_ids = select(DataPackage.id).where(
            DataPackage.workspace_id == workspace_id,
            DataPackage.collection_project_id == collection_project_id,
        )
        query = query.filter(Episode.data_package_id.in_(package_ids))
    if modality:
        query = query.filter(Episode.modality == modality)
    if embodiment_id is not None:
        query = query.filter(Episode.embodiment_id == embodiment_id)
    if task_label_id is not None:
        query = query.filter(Episode.task_label_id == task_label_id)
    if scene:
        query = query.filter(Episode.scene == scene)
    if workflow_status:
        query = query.filter(Episode.workflow_status == workflow_status)
    else:
        query = exclude_import_placeholders(query)
    if kind:
        query = query.filter(Episode.kind == kind)
    if annotation_status:
        query = query.filter(Episode.annotation_status == annotation_status)
    if review_status:
        query = query.filter(Episode.review_status == review_status)
    if publication_status:
        latest_status = (
            select(JobRun.status)
            .where(
                JobRun.kind == "episode_publish",
                JobRun.resource_type == "episode",
                JobRun.resource_id == cast(Episode.id, String),
            )
            .order_by(JobRun.created_at.desc(), JobRun.id.desc())
            .limit(1)
            .correlate(Episode)
            .scalar_subquery()
        )
        status = func.coalesce(latest_status, "")
        published = or_(Episode.workflow_status == "published", status == "succeeded")
        if publication_status == "published":
            query = query.filter(published)
        elif publication_status == "publishing":
            query = query.filter(~published, status.in_(_ACTIVE_PUBLICATION_STATUSES))
        elif publication_status == "failed":
            query = query.filter(~published, status == "failed")
        elif publication_status == "unpublished":
            query = query.filter(
                ~published,
                status.notin_((*_ACTIVE_PUBLICATION_STATUSES, "failed", "succeeded")),
            )
        else:
            raise ValueError("invalid publication status")
    return query


def artifact_for_episode(
    db: Session, *, episode_id: int, artifact_id: int
) -> EpisodeArtifact | None:
    """Read artifacts through their episode owner, never by an arbitrary URI."""
    return (
        db.query(EpisodeArtifact)
        .filter(EpisodeArtifact.id == artifact_id, EpisodeArtifact.episode_id == episode_id)
        .one_or_none()
    )
