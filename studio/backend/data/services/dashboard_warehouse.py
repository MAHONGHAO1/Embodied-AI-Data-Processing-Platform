"""Episode-grain DWD → DWS(5m) → ADS dashboard warehouse ETL."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from math import isfinite
from typing import Any

from sqlalchemy import and_, case, delete, exists, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, selectinload

from data.database import (
    AdsDailyKpi,
    AdsDashboardSnapshot,
    CollectionDevice,
    DwdEpisodeFact,
    DwsEpisode5m,
    Episode,
    EpisodeDeviceAttribution,
    JobRun,
    PublishedEpisode,
)
from data.services.collection_duration import episode_duration_seconds
from data.services.dashboard_package_projection import (
    context_revision,
    episode_context_query,
    package_published_at,
    project_package_episode,
)
from data.services.dashboard_scope import DashboardScope
from data.services.episode_visibility import IMPORT_PLACEHOLDER_WORKFLOW_STATUSES
from data.services.job_access import job_has_consistent_resource_scope
from data.services.job_runs import create_or_get_job
from data.services.task_dispatcher import JobDispatchUnavailable, dispatch_media_job

DASHBOARD_ETL_JOB_KIND = "dashboard_etl"
DASHBOARD_ETL_QUEUE = "analytics"
PIPELINE_STAGES = ("intake", "collected", "separated", "annotated", "stored")
PIPELINE_STAGE_LABELS = {
    "intake": "数据接入",
    "collected": "采集数据",
    "separated": "分离数据",
    "annotated": "标注数据",
    "stored": "入库数据",
}
DURATION_BUCKETS = ("lt_30s", "bt_30_60s", "gt_60s", "unknown")
QUEUE_KEYS = PIPELINE_STAGES
QUEUE_LABELS = {
    "intake": "待采集",
    "collected": "待处理",
    "separated": "待标注",
    "annotated": "待标注审核",
    "stored": "待入库",
}
_QUALITY_PASSED = frozenset({"passed", "recovered", "profiled"})
_ATTRIBUTION_PRIORITY = {
    "machine_reported": 2,
    "offline_declared": 1,
    "offline_curated": 3,
    "online_verified": 4,
}
_DASHBOARD_ETL_ADVISORY_LOCK_ID = 719_403_202_608_13
_DASHBOARD_ETL_IN_FLIGHT = frozenset({"queued", "running", "retry_pending"})
_DWD_MAX_CHANGED_ROWS_PER_RUN = 20_000
_DWD_PROJECTION_VERSION = 2


def align_bucket_start(moment: datetime | None = None) -> datetime:
    """Return the UTC-naive 5-minute bucket start containing ``moment``."""
    current = moment or datetime.utcnow()
    minute = (current.minute // 5) * 5
    return current.replace(minute=minute, second=0, microsecond=0)


def parse_episode_duration_s(metadata: object) -> float | None:
    if not isinstance(metadata, dict):
        return None
    duration = episode_duration_seconds(metadata)
    if duration is not None:
        seconds = float(duration)
        return seconds if isfinite(seconds) and seconds > 0 else None
    value = metadata.get("duration_hours")
    if isinstance(value, bool):
        return None
    try:
        duration = float(value) * 3600
    except (TypeError, ValueError):
        return None
    return duration if isfinite(duration) and duration > 0 else None


def resolve_episode_duration_s(episode: Episode) -> float | None:
    if episode.kind == "derived":
        start_ns = episode.source_start_ns
        end_ns = episode.source_end_ns
        if (
            isinstance(start_ns, int)
            and not isinstance(start_ns, bool)
            and isinstance(end_ns, int)
            and not isinstance(end_ns, bool)
            and end_ns > start_ns
        ):
            return (end_ns - start_ns) / 1_000_000_000
    return parse_episode_duration_s(episode.metadata_json)


def duration_bucket_for(duration_s: float | None) -> str:
    if duration_s is None:
        return "unknown"
    if duration_s < 30:
        return "lt_30s"
    if duration_s <= 60:
        return "bt_30_60s"
    return "gt_60s"


def is_countable_episode(workflow_status: str) -> bool:
    return workflow_status not in IMPORT_PLACEHOLDER_WORKFLOW_STATUSES


def unique_countable_asset_facts(facts: list[Any]) -> list[Any]:
    """Return asset-library episodes, unique by ``episode_id``.

    The assets page lists every countable Episode (source and derived) after
    dropping import placeholders. KPI totals use this same grain so a source
    and its derived children are distinct entries, never duplicated rows.
    """
    unique: dict[int, Any] = {}
    for fact in facts:
        if not getattr(fact, "is_countable", False):
            continue
        episode_id = getattr(fact, "episode_id", None)
        if episode_id is None:
            continue
        unique[int(episode_id)] = fact
    return list(unique.values())


def asset_library_duration_s(facts: list[Any]) -> float:
    """Sum each asset-library episode's own duration, skipping unknown values."""
    total = 0.0
    for fact in unique_countable_asset_facts(facts):
        duration = getattr(fact, "duration_s", None)
        if duration is None:
            continue
        total += float(duration)
    return total


def is_annotation_eligible(*, kind: str, workflow_status: str) -> bool:
    return (
        kind == "derived"
        and is_countable_episode(workflow_status)
        and workflow_status != "excluded"
    )


def resolve_pipeline_stage(
    *,
    kind: str,
    workflow_status: str,
    quality_status: str,
    annotation_status: str,
    review_status: str,
    is_published: bool,
    has_derived_children: bool,
) -> str:
    """Map an Episode's current state to a mutually exclusive pipeline stage."""
    if is_published or workflow_status == "published":
        return "stored"
    if annotation_status == "accepted":
        return "annotated"
    if kind == "derived":
        return "separated"
    if review_status == "accepted" and not has_derived_children:
        return "separated"
    if (
        kind == "source"
        and quality_status in _QUALITY_PASSED
        and workflow_status not in IMPORT_PLACEHOLDER_WORKFLOW_STATUSES
        and workflow_status not in {"failed", "excluded"}
    ):
        return "collected"
    return "intake"


def resolve_today_queue_key(
    *,
    pipeline_stage: str,
    annotation_status: str = "",
) -> str:
    """Map one countable episode onto the same mutually exclusive stage used by the funnel."""
    if pipeline_stage in PIPELINE_STAGES:
        return pipeline_stage
    return "intake"


def queue_counts_from_asset_facts(facts: list[Any]) -> dict[str, int]:
    """Count pending work; terminal package facts remain in totals, not queues."""
    counts = dict.fromkeys(QUEUE_KEYS, 0)
    for fact in unique_countable_asset_facts(facts):
        if getattr(fact, "projection_version", 1) >= _DWD_PROJECTION_VERSION:
            key = fact.queue_key
            if key in counts:
                counts[key] += 1
            continue
        key = resolve_today_queue_key(
            pipeline_stage=str(getattr(fact, "pipeline_stage", "") or ""),
            annotation_status=str(getattr(fact, "annotation_status", "") or ""),
        )
        counts[key] = int(counts.get(key, 0)) + 1
    return counts


def _duration_basis() -> dict[str, str]:
    return {
        "total_duration_s": "all_countable_episode_durations",
        "source_duration_s": "captured_source_episode_durations",
        "intake_valid_duration_s": "frozen_intake_accepted_source_durations",
        "annotation_effective_duration_s": "approved_submission_effective_segments",
    }


def empty_dashboard_payload(*, computed_at: datetime | None = None) -> dict[str, Any]:
    moment = computed_at or datetime.utcnow()
    return {
        "kpis": {
            "total_episodes": 0,
            "total_duration_s": 0.0,
            "source_duration_s": 0.0,
            "intake_valid_duration_s": 0.0,
            "annotation_effective_duration_s": 0.0,
            "collected_today": 0,
            "annotation_completed": 0,
            "annotation_completion_rate": 0.0,
            "qrdf_baseline_count": 0,
            "deltas": {
                "total_episodes": 0.0,
                "total_duration_s": 0.0,
                "collected_today": 0.0,
                "annotation_completed": 0.0,
                "annotation_completion_rate": 0.0,
            },
        },
        "pipeline_funnel": {
            "avg_duration_s": 0.0,
            "stages": [
                {
                    "key": stage,
                    "label": PIPELINE_STAGE_LABELS[stage],
                    "buckets": dict.fromkeys(DURATION_BUCKETS, 0),
                }
                for stage in PIPELINE_STAGES
            ],
        },
        "today_queues": [
            {"key": key, "label": QUEUE_LABELS[key], "count": 0, "share": 0.0, "pending": 0}
            for key in QUEUE_KEYS
        ],
        "collect_trend_7d": [],
        "device_distribution": {"total": 0, "items": []},
        "computed_at": moment.isoformat() + "Z",
        "time_zone": "UTC",
        "duration_basis": _duration_basis(),
    }


def enqueue_dashboard_etl(
    db: Session,
    *,
    scope: DashboardScope,
    actor_id: int | None = None,
    bucket_start: datetime | None = None,
) -> JobRun:
    """Create or reuse a dashboard ETL JobRun and dispatch in-flight work to Celery.

    The same 5-minute idempotency key is reused for in-flight jobs. A terminal
    (succeeded/failed/cancelled) row is also returned as-is; callers that need a
    fresh snapshot must rebuild in-process rather than treating that row as a
    completed refresh.
    """
    bucket = align_bucket_start(bucket_start)
    idempotency_key = f"dashboard-etl:{scope.key}:{bucket.isoformat()}"
    existing = db.scalar(select(JobRun).where(JobRun.idempotency_key == idempotency_key))
    if existing is not None:
        if existing.status in {"queued", "retry_pending"}:
            try:
                dispatch_media_job(existing)
            except JobDispatchUnavailable:
                pass
        return existing
    job = create_or_get_job(
        db,
        kind=DASHBOARD_ETL_JOB_KIND,
        resource_type="platform",
        resource_id=scope.key,
        idempotency_key=idempotency_key,
        queue=DASHBOARD_ETL_QUEUE,
        actor_id=actor_id,
        workspace_id=scope.workspace_id,
        task_set_id=scope.task_set_id,
        detail={
            "bucket_start": bucket.isoformat(),
            "scope_key": scope.key,
            "workspace_id": scope.workspace_id,
            "task_set_id": scope.task_set_id,
        },
    )
    if job.status in {"queued", "retry_pending"}:
        try:
            dispatch_media_job(job)
        except JobDispatchUnavailable:
            # Beat/API can leave the durable queued row for a later worker.
            pass
    return job


def _execute_dashboard_warehouse(
    db: Session,
    *,
    scope: DashboardScope,
    bucket_start: datetime,
    etl_job_id: str,
) -> dict[str, Any]:
    """Rebuild DWD → DWS → ADS for one scope/bucket. Caller owns the transaction."""
    etl_at = datetime.utcnow()
    db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": _DASHBOARD_ETL_ADVISORY_LOCK_ID},
    )
    build_dwd(db, etl_at=etl_at)
    roll_dws(db, scope=scope, bucket_start=bucket_start, etl_at=etl_at)
    return refresh_ads(
        db,
        scope=scope,
        bucket_start=bucket_start,
        etl_at=etl_at,
        etl_job_id=etl_job_id,
    )


def run_dashboard_etl(db: Session, job: JobRun) -> dict[str, Any]:
    """Execute one full warehouse rebuild for the JobRun's bucket."""
    if job.kind != DASHBOARD_ETL_JOB_KIND or job.resource_type != "platform":
        raise ValueError("unsupported dashboard etl job")
    if not job_has_consistent_resource_scope(db, job=job):
        raise ValueError("dashboard job resource scope is inconsistent")
    detail = job.detail_json if isinstance(job.detail_json, dict) else {}
    scope = DashboardScope.from_job_detail(detail)
    if job.resource_id != scope.key:
        raise ValueError("dashboard job resource scope is inconsistent")
    raw_bucket = detail.get("bucket_start")
    if isinstance(raw_bucket, str) and raw_bucket:
        try:
            bucket_start = datetime.fromisoformat(raw_bucket.replace("Z", ""))
        except ValueError:
            bucket_start = align_bucket_start()
    else:
        bucket_start = align_bucket_start()
    bucket_start = align_bucket_start(bucket_start)
    payload = _execute_dashboard_warehouse(
        db,
        scope=scope,
        bucket_start=bucket_start,
        etl_job_id=job.id,
    )
    db.commit()
    return {
        "bucket_start": bucket_start.isoformat() + "Z",
        "computed_at": payload["computed_at"],
        "scope_key": scope.key,
        "total_episodes": payload["kpis"]["total_episodes"],
        "qrdf_baseline_count": payload["kpis"]["qrdf_baseline_count"],
    }


def request_dashboard_refresh(
    db: Session,
    *,
    scope: DashboardScope,
    actor_id: int | None = None,
    bucket_start: datetime | None = None,
) -> tuple[JobRun, dict[str, Any]]:
    """Enqueue warehouse ETL and return a live overview assembled from current OLTP.

    In-flight jobs stay idempotent. Terminal jobs in the same 5-minute bucket are
    not treated as a finished refresh: the snapshot is rebuilt in-process so a
    local API without Celery, or a second click after success, still matches
    Data Assets / Work Queue.
    """
    job = enqueue_dashboard_etl(db, scope=scope, actor_id=actor_id, bucket_start=bucket_start)
    bucket = align_bucket_start(bucket_start)
    if job.status not in _DASHBOARD_ETL_IN_FLIGHT:
        payload = _execute_dashboard_warehouse(
            db,
            scope=scope,
            bucket_start=bucket,
            etl_job_id=job.id,
        )
        db.commit()
    else:
        payload = build_live_overview_payload(db, scope)
    payload = dict(payload)
    payload["etl_job_id"] = job.id
    payload["scope"] = scope.as_dict()
    payload["source"] = "live"
    payload["status"] = "ready"
    return job, payload


def collect_episode_fact_mappings(
    db: Session,
    *,
    etl_at: datetime,
    scope: DashboardScope | None = None,
    episode_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Project current OLTP Episode rows into DWD-shaped fact mappings."""
    if episode_ids is not None and not episode_ids:
        return []
    # Episode is the warehouse grain.  Collection-package Episodes deliberately
    # carry nullable legacy TaskSet/Batch IDs, so scope them by their own
    # workspace/task-set columns and never require a legacy Batch join.
    query = db.query(Episode).options(
        selectinload(Episode.device_attributions).selectinload(
            EpisodeDeviceAttribution.collection_device
        ),
        selectinload(Episode.derived_episodes),
        selectinload(Episode.parent),
    )
    if scope is not None:
        query = query.filter(
            _scope_filter(
                scope,
                workspace_column=Episode.workspace_id,
                task_set_column=Episode.task_set_id,
            )
        )
    if episode_ids is not None:
        query = query.filter(Episode.id.in_(episode_ids))
    episodes = query.all()
    published_query = select(PublishedEpisode.episode_id, PublishedEpisode.published_at)
    if episode_ids is not None:
        published_query = published_query.where(PublishedEpisode.episode_id.in_(episode_ids))
    elif scope is not None:
        published_query = published_query.join(
            Episode, Episode.id == PublishedEpisode.episode_id
        ).where(
            _scope_filter(
                scope,
                workspace_column=Episode.workspace_id,
                task_set_column=Episode.task_set_id,
            )
        )
    published_rows = db.execute(published_query).all()
    published_at_by_id = {
        int(episode_id): published_at for episode_id, published_at in published_rows
    }
    published_ids = set(published_at_by_id)
    package_ids = [episode.id for episode in episodes if episode.data_package_id is not None]
    contexts = (
        {
            row.Episode.id: row
            for row in episode_context_query(db).filter(Episode.id.in_(package_ids)).all()
        }
        if package_ids
        else {}
    )

    rows: list[dict[str, Any]] = []
    for episode in episodes:
        root = _root_source(episode)
        device_id, device_name = _effective_device(root)
        duration_s = resolve_episode_duration_s(episode)
        is_published = episode.id in published_ids
        countable = is_countable_episode(episode.workflow_status)
        stage = resolve_pipeline_stage(
            kind=episode.kind,
            workflow_status=episode.workflow_status,
            quality_status=episode.quality_status,
            annotation_status=episode.annotation_status,
            review_status=episode.review_status,
            is_published=is_published,
            has_derived_children=bool(episode.derived_episodes),
        )
        rows.append(
            {
                "episode_id": episode.id,
                "episode_uid": episode.episode_uid,
                "kind": episode.kind,
                "workspace_id": episode.workspace_id,
                "task_set_id": episode.task_set_id,
                "batch_id": episode.batch_id,
                "scene": episode.scene or "",
                "modality": episode.modality,
                "embodiment_id": episode.embodiment_id,
                "task_label_id": episode.task_label_id,
                "device_id": device_id,
                "device_name": device_name,
                "workflow_status": episode.workflow_status,
                "quality_status": episode.quality_status,
                "annotation_status": episode.annotation_status,
                "review_status": episode.review_status,
                "pipeline_stage": stage,
                "annotation_eligible": is_annotation_eligible(
                    kind=episode.kind, workflow_status=episode.workflow_status
                ),
                "queue_key": stage,
                "intake_valid_duration_s": None,
                "annotation_effective_duration_ns": None,
                "projection_version": _DWD_PROJECTION_VERSION,
                "source_revision": None,
                "duration_s": duration_s,
                "duration_bucket": duration_bucket_for(duration_s),
                "is_countable": countable,
                "is_qrdf_baseline": is_published or episode.workflow_status == "published",
                "collected_on": episode.created_at.date() if episode.created_at else None,
                "created_at": episode.created_at,
                "published_at": published_at_by_id.get(episode.id),
                "source_updated_at": episode.updated_at,
                "etl_at": etl_at,
            }
        )
        if episode.id in contexts:
            rows[-1]["source_updated_at"] = contexts[episode.id].context_updated_at
            rows[-1]["source_revision"] = contexts[episode.id].source_revision
            rows[-1].update(project_package_episode(contexts[episode.id], duration_s=duration_s))
    return rows


def build_dwd(db: Session, *, etl_at: datetime) -> int:
    """Incrementally upsert changed Episode facts and reconcile deletions."""
    changed_ids = list(
        db.scalars(
            episode_context_query(db)
            .with_entities(Episode.id)
            .outerjoin(DwdEpisodeFact, DwdEpisodeFact.episode_id == Episode.id)
            .outerjoin(PublishedEpisode, PublishedEpisode.episode_id == Episode.id)
            .where(
                or_(
                    DwdEpisodeFact.episode_id.is_(None),
                    DwdEpisodeFact.projection_version < _DWD_PROJECTION_VERSION,
                    and_(
                        Episode.data_package_id.is_(None),
                        Episode.updated_at.is_distinct_from(DwdEpisodeFact.source_updated_at),
                    ),
                    and_(
                        Episode.data_package_id.is_not(None),
                        context_revision().is_distinct_from(DwdEpisodeFact.source_revision),
                    ),
                    and_(
                        PublishedEpisode.published_at.is_not(None),
                        or_(
                            DwdEpisodeFact.published_at.is_(None),
                            PublishedEpisode.published_at != DwdEpisodeFact.published_at,
                        ),
                    ),
                    and_(
                        func.coalesce(PublishedEpisode.published_at, package_published_at()).is_(
                            None
                        ),
                        DwdEpisodeFact.published_at.is_not(None),
                    ),
                ),
            )
            .order_by(Episode.updated_at.asc(), Episode.id.asc())
            .limit(_DWD_MAX_CHANGED_ROWS_PER_RUN)
        )
    )
    if changed_ids:
        parent_ids = list(
            db.scalars(
                select(Episode.parent_episode_id)
                .where(
                    Episode.id.in_(changed_ids),
                    Episode.parent_episode_id.is_not(None),
                )
                .distinct()
            )
        )
        changed_ids = sorted(
            {*changed_ids, *(int(item) for item in parent_ids if item is not None)}
        )
    rows = collect_episode_fact_mappings(db, etl_at=etl_at, episode_ids=changed_ids)
    if rows:
        statement = pg_insert(DwdEpisodeFact).values(rows)
        statement = statement.on_conflict_do_update(
            index_elements=[DwdEpisodeFact.episode_id],
            set_={
                column.name: getattr(statement.excluded, column.name)
                for column in DwdEpisodeFact.__table__.columns
                if column.name != "episode_id"
            },
        )
        db.execute(statement)
    orphan_ids = list(
        db.scalars(
            select(DwdEpisodeFact.episode_id)
            .where(~exists(select(Episode.id).where(Episode.id == DwdEpisodeFact.episode_id)))
            .order_by(DwdEpisodeFact.episode_id.asc())
            .limit(_DWD_MAX_CHANGED_ROWS_PER_RUN)
        )
    )
    if orphan_ids:
        db.execute(delete(DwdEpisodeFact).where(DwdEpisodeFact.episode_id.in_(orphan_ids)))
    db.flush()
    return len(rows) + len(orphan_ids)


def _aggregate_dws_rows(
    db: Session,
    *,
    scope: DashboardScope,
    bucket_start: datetime,
    etl_at: datetime,
) -> list[dict[str, Any]]:
    """Return bounded SQL aggregates for one dashboard scope."""
    scope_filter = _scope_filter(
        scope,
        workspace_column=DwdEpisodeFact.workspace_id,
        task_set_column=DwdEpisodeFact.task_set_id,
    )
    countable_filter = and_(scope_filter, DwdEpisodeFact.is_countable.is_(True))
    today = bucket_start.date()
    global_values = db.execute(
        select(
            func.count(DwdEpisodeFact.episode_id),
            func.coalesce(func.sum(DwdEpisodeFact.duration_s), 0.0),
            func.coalesce(
                func.sum(case((DwdEpisodeFact.is_qrdf_baseline.is_(True), 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(case((DwdEpisodeFact.annotation_status == "accepted", 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (
                            DwdEpisodeFact.annotation_eligible.is_(True),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                DwdEpisodeFact.kind == "source",
                                DwdEpisodeFact.collected_on == today,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(case((DwdEpisodeFact.kind == "source", 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (DwdEpisodeFact.kind == "source", DwdEpisodeFact.duration_s),
                        else_=None,
                    )
                ),
                0.0,
            ),
            func.coalesce(func.sum(DwdEpisodeFact.intake_valid_duration_s), 0.0),
            func.coalesce(func.sum(DwdEpisodeFact.annotation_effective_duration_ns), 0),
        ).where(countable_filter)
    ).one()
    (
        total_episodes,
        total_duration,
        qrdf_count,
        annotated_count,
        eligible_count,
        collected_today,
        source_count,
        source_duration,
        intake_duration,
        annotation_duration_ns,
    ) = global_values

    aggregates = [
        _dws_row(
            scope,
            bucket_start,
            grain="global",
            dim_key="all",
            dim_value="all",
            episode_count=int(total_episodes or 0),
            duration_s_sum=float(total_duration or 0.0),
            qrdf_count=int(qrdf_count or 0),
            annotated_count=int(annotated_count or 0),
            eligible_annotation_count=int(eligible_count or 0),
            etl_at=etl_at,
        ),
        _dws_row(
            scope,
            bucket_start,
            grain="global",
            dim_key="collected_today",
            dim_value=today.isoformat(),
            episode_count=int(collected_today or 0),
            duration_s_sum=None,
            qrdf_count=None,
            annotated_count=None,
            eligible_annotation_count=None,
            etl_at=etl_at,
        ),
        _dws_row(
            scope,
            bucket_start,
            grain="global",
            dim_key="source_duration",
            dim_value="all",
            episode_count=int(source_count or 0),
            duration_s_sum=float(source_duration or 0.0),
            qrdf_count=None,
            annotated_count=None,
            eligible_annotation_count=None,
            etl_at=etl_at,
        ),
    ]
    for key, seconds in (
        ("intake_valid_duration", float(intake_duration)),
        ("annotation_effective_duration", int(annotation_duration_ns) / 1_000_000_000),
    ):
        aggregates.append(
            _dws_row(
                scope,
                bucket_start,
                grain="global",
                dim_key=key,
                dim_value="all",
                episode_count=0,
                duration_s_sum=seconds,
                qrdf_count=None,
                annotated_count=None,
                eligible_annotation_count=None,
                etl_at=etl_at,
            )
        )

    pipeline_counts = {
        (str(stage), str(duration_bucket)): int(count)
        for stage, duration_bucket, count in db.execute(
            select(
                DwdEpisodeFact.pipeline_stage,
                DwdEpisodeFact.duration_bucket,
                func.count(DwdEpisodeFact.episode_id),
            )
            .where(countable_filter)
            .group_by(DwdEpisodeFact.pipeline_stage, DwdEpisodeFact.duration_bucket)
        ).all()
    }
    queue_counts = dict(
        db.execute(
            select(DwdEpisodeFact.queue_key, func.count(DwdEpisodeFact.episode_id))
            .where(countable_filter, DwdEpisodeFact.queue_key.is_not(None))
            .group_by(DwdEpisodeFact.queue_key)
        ).all()
    )
    for stage in PIPELINE_STAGES:
        for duration_bucket in DURATION_BUCKETS:
            count = pipeline_counts.get((stage, duration_bucket), 0)
            aggregates.append(
                _dws_row(
                    scope,
                    bucket_start,
                    grain="pipeline_duration",
                    dim_key=stage,
                    dim_value=duration_bucket,
                    episode_count=count,
                    duration_s_sum=None,
                    qrdf_count=None,
                    annotated_count=None,
                    eligible_annotation_count=None,
                    etl_at=etl_at,
                )
            )
    for key in QUEUE_KEYS:
        aggregates.append(
            _dws_row(
                scope,
                bucket_start,
                grain="queue",
                dim_key=key,
                dim_value=QUEUE_LABELS[key],
                episode_count=queue_counts.get(key, 0),
                duration_s_sum=None,
                qrdf_count=None,
                annotated_count=None,
                eligible_annotation_count=None,
                etl_at=etl_at,
            )
        )

    device_rows = db.execute(
        select(
            DwdEpisodeFact.device_id,
            DwdEpisodeFact.device_name,
            func.count(DwdEpisodeFact.episode_id).label("episode_count"),
        )
        .where(countable_filter, DwdEpisodeFact.kind == "source")
        .group_by(DwdEpisodeFact.device_id, DwdEpisodeFact.device_name)
        .order_by(text("episode_count DESC"), DwdEpisodeFact.device_name.asc())
        .limit(20)
    ).all()
    for device_id, device_name, count in device_rows:
        aggregates.append(
            _dws_row(
                scope,
                bucket_start,
                grain="device",
                dim_key=str(device_id or 0),
                dim_value=str(device_name or "未登记设备")[:128],
                episode_count=int(count),
                duration_s_sum=None,
                qrdf_count=None,
                annotated_count=None,
                eligible_annotation_count=None,
                etl_at=etl_at,
            )
        )

    first_trend_day = today - timedelta(days=6)
    daily_rows = db.execute(
        select(DwdEpisodeFact.collected_on, func.count(DwdEpisodeFact.episode_id))
        .where(
            countable_filter,
            DwdEpisodeFact.kind == "source",
            DwdEpisodeFact.collected_on >= first_trend_day,
            DwdEpisodeFact.collected_on <= today,
        )
        .group_by(DwdEpisodeFact.collected_on)
        .order_by(DwdEpisodeFact.collected_on.asc())
    ).all()
    for day, count in daily_rows:
        aggregates.append(
            _dws_row(
                scope,
                bucket_start,
                grain="collect_daily",
                dim_key="day",
                dim_value=day.isoformat(),
                episode_count=int(count),
                duration_s_sum=None,
                qrdf_count=None,
                annotated_count=None,
                eligible_annotation_count=None,
                etl_at=etl_at,
            )
        )
    return aggregates


def roll_dws(
    db: Session,
    *,
    scope: DashboardScope,
    bucket_start: datetime,
    etl_at: datetime,
) -> int:
    """Aggregate the current DWD with bounded SQL group results."""
    aggregates = _aggregate_dws_rows(
        db,
        scope=scope,
        bucket_start=bucket_start,
        etl_at=etl_at,
    )

    db.execute(
        delete(DwsEpisode5m).where(
            DwsEpisode5m.scope_key == scope.key,
            DwsEpisode5m.bucket_start == bucket_start,
        )
    )
    if aggregates:
        # Strip helper-only keys before insert.
        insert_rows = []
        for row in aggregates:
            clean = {key: value for key, value in row.items() if key != "extra"}
            insert_rows.append(clean)
        db.bulk_insert_mappings(DwsEpisode5m, insert_rows)
    db.flush()
    return len(aggregates)


def refresh_ads(
    db: Session,
    *,
    scope: DashboardScope,
    bucket_start: datetime,
    etl_at: datetime,
    etl_job_id: str,
) -> dict[str, Any]:
    """Build the dashboard payload from the latest DWS/DWD and persist ADS rows."""
    payload = build_overview_payload(
        db,
        scope=scope,
        bucket_start=bucket_start,
        computed_at=etl_at,
    )
    snapshot = (
        db.query(AdsDashboardSnapshot)
        .filter(
            AdsDashboardSnapshot.scope_key == scope.key,
            AdsDashboardSnapshot.source_bucket_start == bucket_start,
        )
        .one_or_none()
    )
    if snapshot is None:
        snapshot = AdsDashboardSnapshot(
            scope_key=scope.key,
            workspace_id=scope.workspace_id,
            task_set_id=scope.task_set_id,
            payload_json=payload,
            computed_at=etl_at,
            source_bucket_start=bucket_start,
            etl_job_id=etl_job_id,
        )
        db.add(snapshot)
    else:
        snapshot.payload_json = payload
        snapshot.computed_at = etl_at
        snapshot.source_bucket_start = bucket_start
        snapshot.etl_job_id = etl_job_id

    kpis = payload["kpis"]
    daily = (
        db.query(AdsDailyKpi)
        .filter(
            AdsDailyKpi.scope_key == scope.key,
            AdsDailyKpi.stat_date == bucket_start.date(),
        )
        .one_or_none()
    )
    if daily is None:
        daily = AdsDailyKpi(
            scope_key=scope.key,
            workspace_id=scope.workspace_id,
            task_set_id=scope.task_set_id,
            stat_date=bucket_start.date(),
        )
        db.add(daily)
    daily.total_episodes = int(kpis["total_episodes"])
    daily.total_duration_s = float(kpis["total_duration_s"])
    daily.collected_today = int(kpis["collected_today"])
    daily.annotation_completed = int(kpis["annotation_completed"])
    daily.annotation_completion_rate = float(kpis["annotation_completion_rate"])
    daily.qrdf_baseline_count = int(kpis["qrdf_baseline_count"])
    daily.computed_at = etl_at
    db.flush()
    return payload


def assemble_overview_from_facts(
    facts: list[Any],
    *,
    scope: DashboardScope,
    bucket_start: datetime,
    computed_at: datetime,
    yesterday: AdsDailyKpi | None,
) -> dict[str, Any]:
    """Build the overview payload from the same unique countable Episode grain."""
    payload = empty_dashboard_payload(computed_at=computed_at)
    countable = unique_countable_asset_facts(facts)
    total_episodes = len(countable)
    total_duration = asset_library_duration_s(facts)
    annotated_count = sum(1 for fact in countable if fact.annotation_status == "accepted")
    eligible_count = sum(
        1
        for fact in countable
        if (
            fact.annotation_eligible
            if getattr(fact, "projection_version", 1) >= _DWD_PROJECTION_VERSION
            else is_annotation_eligible(kind=fact.kind, workflow_status=fact.workflow_status)
        )
    )
    qrdf_count = sum(1 for fact in countable if fact.is_qrdf_baseline)
    today = bucket_start.date()
    collected_today = sum(
        1 for fact in countable if fact.kind == "source" and fact.collected_on == today
    )
    rate = (annotated_count / eligible_count) if eligible_count > 0 else 0.0
    deltas = {
        "total_episodes": _delta_ratio(
            total_episodes, yesterday.total_episodes if yesterday else None
        ),
        "total_duration_s": _delta_ratio(
            total_duration, yesterday.total_duration_s if yesterday else None
        ),
        "collected_today": _delta_ratio(
            collected_today, yesterday.collected_today if yesterday else None
        ),
        "annotation_completed": _delta_ratio(
            annotated_count, yesterday.annotation_completed if yesterday else None
        ),
        "annotation_completion_rate": _delta_ratio(
            rate, yesterday.annotation_completion_rate if yesterday else None
        ),
    }
    payload["kpis"] = {
        "total_episodes": total_episodes,
        "total_duration_s": round(total_duration, 3),
        "source_duration_s": sum(float(f.duration_s or 0) for f in countable if f.kind == "source"),
        "intake_valid_duration_s": sum(
            float(getattr(f, "intake_valid_duration_s", 0) or 0) for f in countable
        ),
        "annotation_effective_duration_s": sum(
            int(getattr(f, "annotation_effective_duration_ns", 0) or 0) for f in countable
        )
        / 1_000_000_000,
        "collected_today": collected_today,
        "annotation_completed": annotated_count,
        "annotation_completion_rate": round(rate, 4),
        "qrdf_baseline_count": qrdf_count,
        "deltas": deltas,
    }

    stage_map = {stage: dict.fromkeys(DURATION_BUCKETS, 0) for stage in PIPELINE_STAGES}
    queue_counts = queue_counts_from_asset_facts(countable)
    duration_values: list[float] = []
    for fact in countable:
        stage = fact.pipeline_stage if fact.pipeline_stage in stage_map else "intake"
        bucket = fact.duration_bucket if fact.duration_bucket in stage_map[stage] else "unknown"
        stage_map[stage][bucket] += 1
        if fact.kind == "source" and fact.duration_s is not None:
            duration_values.append(float(fact.duration_s))
    payload["pipeline_funnel"] = {
        "avg_duration_s": round(sum(duration_values) / len(duration_values), 3)
        if duration_values
        else 0.0,
        "stages": [
            {
                "key": stage,
                "label": PIPELINE_STAGE_LABELS[stage],
                "buckets": stage_map[stage],
            }
            for stage in PIPELINE_STAGES
        ],
    }

    payload["today_queues"] = [
        {
            "key": key,
            "label": QUEUE_LABELS[key],
            "count": queue_counts[key],
            "share": round((queue_counts[key] / total_episodes), 4) if total_episodes else 0.0,
            "pending": queue_counts[key],
        }
        for key in QUEUE_KEYS
    ]

    trend_days = [
        (bucket_start.date() - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)
    ]
    dwd_daily: dict[str, int] = defaultdict(int)
    for fact in countable:
        if fact.kind != "source" or fact.collected_on is None:
            continue
        day = fact.collected_on.isoformat()
        if day in set(trend_days):
            dwd_daily[day] += 1
    payload["collect_trend_7d"] = (
        [{"date": day, "count": int(dwd_daily.get(day, 0))} for day in trend_days]
        if countable
        else []
    )

    device_counts: dict[tuple[int | None, str], int] = defaultdict(int)
    for fact in countable:
        if fact.kind != "source":
            continue
        device_counts[(fact.device_id, fact.device_name or "未登记设备")] += 1
    device_items: list[dict[str, Any]] = []
    device_total = 0
    for (device_id, name), count in device_counts.items():
        device_total += count
        device_items.append({"device_id": device_id, "name": name, "count": count, "share": 0.0})
    for item in device_items:
        item["share"] = round((item["count"] / device_total), 4) if device_total else 0.0
    device_items.sort(key=lambda item: (-int(item["count"]), str(item["name"])))
    payload["device_distribution"] = {"total": device_total, "items": device_items[:20]}
    payload["computed_at"] = computed_at.isoformat() + "Z"
    payload["scope"] = scope.as_dict()
    payload["source"] = "live"
    payload["status"] = "ready"
    payload["time_zone"] = "UTC"
    payload["duration_basis"] = _duration_basis()
    return payload


def assemble_overview_from_dws_rows(
    rows: list[Any],
    *,
    scope: DashboardScope,
    bucket_start: datetime,
    computed_at: datetime,
    yesterday: AdsDailyKpi | None,
) -> dict[str, Any]:
    """Build a dashboard payload from the bounded DWS result set."""
    payload = empty_dashboard_payload(computed_at=computed_at)

    def value(row: Any, key: str, default: Any = None) -> Any:
        if isinstance(row, dict):
            return row.get(key, default)
        return getattr(row, key, default)

    by_dimension = {
        (
            str(value(row, "grain", "")),
            str(value(row, "dim_key", "")),
            str(value(row, "dim_value", "")),
        ): row
        for row in rows
    }
    global_row = by_dimension.get(("global", "all", "all"))
    collected_row = by_dimension.get(("global", "collected_today", bucket_start.date().isoformat()))
    source_duration_row = by_dimension.get(("global", "source_duration", "all"))
    total_episodes = int(value(global_row, "episode_count", 0) or 0)
    total_duration = float(value(global_row, "duration_s_sum", 0.0) or 0.0)
    collected_today = int(value(collected_row, "episode_count", 0) or 0)
    annotated_count = int(value(global_row, "annotated_count", 0) or 0)
    eligible_count = int(value(global_row, "eligible_annotation_count", 0) or 0)
    qrdf_count = int(value(global_row, "qrdf_count", 0) or 0)
    annotation_rate = annotated_count / eligible_count if eligible_count else 0.0
    payload["kpis"] = {
        "total_episodes": total_episodes,
        "total_duration_s": round(total_duration, 3),
        "source_duration_s": float(value(source_duration_row, "duration_s_sum", 0.0) or 0.0),
        "intake_valid_duration_s": float(
            value(
                by_dimension.get(("global", "intake_valid_duration", "all")), "duration_s_sum", 0.0
            )
            or 0.0
        ),
        "annotation_effective_duration_s": float(
            value(
                by_dimension.get(("global", "annotation_effective_duration", "all")),
                "duration_s_sum",
                0.0,
            )
            or 0.0
        ),
        "collected_today": collected_today,
        "annotation_completed": annotated_count,
        "annotation_completion_rate": round(annotation_rate, 4),
        "qrdf_baseline_count": qrdf_count,
        "deltas": {
            "total_episodes": _delta_ratio(
                total_episodes, yesterday.total_episodes if yesterday else None
            ),
            "total_duration_s": _delta_ratio(
                total_duration, yesterday.total_duration_s if yesterday else None
            ),
            "collected_today": _delta_ratio(
                collected_today, yesterday.collected_today if yesterday else None
            ),
            "annotation_completed": _delta_ratio(
                annotated_count, yesterday.annotation_completed if yesterday else None
            ),
            "annotation_completion_rate": _delta_ratio(
                annotation_rate,
                yesterday.annotation_completion_rate if yesterday else None,
            ),
        },
    }

    stage_map = {stage: dict.fromkeys(DURATION_BUCKETS, 0) for stage in PIPELINE_STAGES}
    for row in rows:
        if value(row, "grain") != "pipeline_duration":
            continue
        stage = str(value(row, "dim_key", ""))
        duration_bucket = str(value(row, "dim_value", ""))
        if stage in stage_map and duration_bucket in stage_map[stage]:
            stage_map[stage][duration_bucket] = int(value(row, "episode_count", 0) or 0)
    source_count = int(value(source_duration_row, "episode_count", 0) or 0)
    source_duration = float(value(source_duration_row, "duration_s_sum", 0.0) or 0.0)
    payload["pipeline_funnel"] = {
        "avg_duration_s": round(source_duration / source_count, 3) if source_count else 0.0,
        "stages": [
            {
                "key": stage,
                "label": PIPELINE_STAGE_LABELS[stage],
                "buckets": stage_map[stage],
            }
            for stage in PIPELINE_STAGES
        ],
    }

    queue_counts = dict.fromkeys(QUEUE_KEYS, 0)
    for row in rows:
        if value(row, "grain") != "queue":
            continue
        key = str(value(row, "dim_key", ""))
        if key in queue_counts:
            queue_counts[key] = int(value(row, "episode_count", 0) or 0)
    payload["today_queues"] = [
        {
            "key": key,
            "label": QUEUE_LABELS[key],
            "count": queue_counts[key],
            "share": round(queue_counts[key] / total_episodes, 4) if total_episodes else 0.0,
            "pending": queue_counts[key],
        }
        for key in QUEUE_KEYS
    ]

    trend_counts = {
        str(value(row, "dim_value")): int(value(row, "episode_count", 0) or 0)
        for row in rows
        if value(row, "grain") == "collect_daily"
    }
    trend_days = [
        (bucket_start.date() - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)
    ]
    payload["collect_trend_7d"] = (
        [{"date": day, "count": trend_counts.get(day, 0)} for day in trend_days]
        if source_count
        else []
    )

    device_items = []
    for row in rows:
        if value(row, "grain") != "device":
            continue
        raw_device_id = str(value(row, "dim_key", "0"))
        count = int(value(row, "episode_count", 0) or 0)
        device_items.append(
            {
                "device_id": int(raw_device_id) if raw_device_id != "0" else None,
                "name": str(value(row, "dim_value", "未登记设备")),
                "count": count,
                "share": round(count / source_count, 4) if source_count else 0.0,
            }
        )
    device_items.sort(key=lambda item: (-int(item["count"]), str(item["name"])))
    payload["device_distribution"] = {
        "total": source_count,
        "items": device_items[:20],
    }
    payload["computed_at"] = computed_at.isoformat() + "Z"
    payload["scope"] = scope.as_dict()
    payload["source"] = "live"
    payload["status"] = "ready"
    payload["time_zone"] = "UTC"
    payload["duration_basis"] = _duration_basis()
    return payload


def build_overview_payload(
    db: Session,
    *,
    scope: DashboardScope,
    bucket_start: datetime,
    computed_at: datetime,
) -> dict[str, Any]:
    dws_rows = (
        db.query(DwsEpisode5m)
        .filter(
            DwsEpisode5m.scope_key == scope.key,
            DwsEpisode5m.bucket_start == bucket_start,
        )
        .all()
    )
    if not dws_rows:
        roll_dws(db, scope=scope, bucket_start=bucket_start, etl_at=computed_at)
        dws_rows = (
            db.query(DwsEpisode5m)
            .filter(
                DwsEpisode5m.scope_key == scope.key,
                DwsEpisode5m.bucket_start == bucket_start,
            )
            .all()
        )
    yesterday = (
        db.query(AdsDailyKpi)
        .filter(
            AdsDailyKpi.scope_key == scope.key,
            AdsDailyKpi.stat_date == bucket_start.date() - timedelta(days=1),
        )
        .one_or_none()
    )
    return assemble_overview_from_dws_rows(
        dws_rows,
        scope=scope,
        bucket_start=bucket_start,
        computed_at=computed_at,
        yesterday=yesterday,
    )


def build_live_overview_payload(db: Session, scope: DashboardScope) -> dict[str, Any]:
    """Assemble live KPIs through incremental DWD and bounded SQL aggregates."""
    computed_at = datetime.utcnow()
    bucket_start = align_bucket_start(computed_at)
    build_dwd(db, etl_at=computed_at)
    dws_rows = _aggregate_dws_rows(
        db,
        scope=scope,
        bucket_start=bucket_start,
        etl_at=computed_at,
    )
    yesterday = (
        db.query(AdsDailyKpi)
        .filter(
            AdsDailyKpi.scope_key == scope.key,
            AdsDailyKpi.stat_date == bucket_start.date() - timedelta(days=1),
        )
        .one_or_none()
    )
    return assemble_overview_from_dws_rows(
        dws_rows,
        scope=scope,
        bucket_start=bucket_start,
        computed_at=computed_at,
        yesterday=yesterday,
    )


def read_dashboard_overview(db: Session, *, scope: DashboardScope) -> dict[str, Any]:
    """Return the live Episode-grain overview for the authorized scope.

    Yesterday deltas still come from ``ads_daily_kpi``. Warehouse ADS snapshots
    remain the periodic ETL product; the operator-facing GET must match
    Data Assets / Work Queue without waiting for Celery.
    """
    return build_live_overview_payload(db, scope)


def _dws_row(
    scope: DashboardScope,
    bucket_start: datetime,
    *,
    grain: str,
    dim_key: str,
    dim_value: str,
    episode_count: int,
    duration_s_sum: float | None,
    qrdf_count: int | None,
    annotated_count: int | None,
    eligible_annotation_count: int | None,
    etl_at: datetime,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "scope_key": scope.key,
        "workspace_id": scope.workspace_id,
        "task_set_id": scope.task_set_id,
        "bucket_start": bucket_start,
        "grain": grain,
        "dim_key": dim_key,
        "dim_value": dim_value,
        "episode_count": episode_count,
        "duration_s_sum": duration_s_sum,
        "qrdf_count": qrdf_count,
        "annotated_count": annotated_count,
        "eligible_annotation_count": eligible_annotation_count,
        "etl_at": etl_at,
        "extra": extra or {},
    }


def _scoped_dwd_query(db: Session, scope: DashboardScope):
    return db.query(DwdEpisodeFact).filter(
        _scope_filter(
            scope,
            workspace_column=DwdEpisodeFact.workspace_id,
            task_set_column=DwdEpisodeFact.task_set_id,
        )
    )


def _scope_filter(scope: DashboardScope, *, workspace_column, task_set_column):
    if scope.task_set_id is not None:
        return task_set_column == scope.task_set_id
    if scope.workspace_id is not None:
        return workspace_column == scope.workspace_id
    return workspace_column.is_not(None)


def _delta_ratio(current: float | int, previous: float | int | None) -> float:
    if previous is None:
        return 0.0
    prev = float(previous)
    if prev == 0:
        return 0.0 if float(current) == 0 else 1.0
    return round((float(current) - prev) / abs(prev), 4)


def _root_source(episode: Episode) -> Episode | None:
    current = episode
    seen: set[int] = set()
    for _ in range(32):
        if current.id in seen:
            return None
        seen.add(current.id)
        if current.kind == "source":
            return current
        parent = current.parent
        if parent is None:
            return None
        current = parent
    return None


def _effective_device(root: Episode | None) -> tuple[int | None, str]:
    if root is None:
        return None, ""
    attributions = list(root.device_attributions or [])
    if not attributions:
        return None, ""
    best = max(
        attributions,
        key=lambda row: (_ATTRIBUTION_PRIORITY.get(row.source, 0), row.id),
    )
    device: CollectionDevice | None = best.collection_device
    if device is None:
        return None, "未登记设备"
    return int(device.id), str(device.name or f"设备 {device.id}")
