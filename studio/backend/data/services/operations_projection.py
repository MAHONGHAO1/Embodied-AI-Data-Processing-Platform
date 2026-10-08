"""Read-only, workspace-safe aggregates for the operations dashboard."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import String, and_, cast, or_, select
from sqlalchemy.orm import Session, aliased

from data.database import (
    EGO_KIND_DERIVED,
    EGO_KIND_SOURCE,
    AnnotationRevision,
    CollectionSource,
    CutPlanRevision,
    DatasetRevision,
    EgoEpisode,
    ExportJob,
    JobRun,
    Project,
    ReviewDecision,
    ReviewFinding,
    Task,
    UploadSession,
    WorkflowEvent,
    WorkItem,
)
from data.utils.formatting import format_api_datetime

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_SAFE_WORKFLOW_PAYLOAD_KEYS = frozenset(
    {
        "kind",
        "status",
        "generation",
        "dataset_revision_id",
        "annotation_revision_id",
        "export_job_id",
    }
)
_SAFE_WORKFLOW_TEXT = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_COUNTED_ANNOTATION_STATUSES = ("submitted", "accepted", "rejected")


def build_operations_summary(db: Session, *, workspace_id: int) -> dict[str, object]:
    """Build aggregate facts without changing the caller's transaction."""
    return {
        "jobs": _job_summary(db, workspace_id=workspace_id),
        "work_items": _work_item_summary(db, workspace_id=workspace_id),
        "quality": _quality_summary(db, workspace_id=workspace_id),
        "annotations": _annotation_summary(db, workspace_id=workspace_id),
        "reviews": _review_summary(db, workspace_id=workspace_id),
        "workflow": _workflow_summary(db, workspace_id=workspace_id),
    }


def _task_ids_for_workspace(workspace_id: int):
    return (
        select(Task.id)
        .outerjoin(Project, Project.id == Task.project_id)
        .where(
            or_(
                Task.workspace_id == workspace_id,
                and_(Task.workspace_id.is_(None), Project.workspace_id == workspace_id),
            )
        )
    )


def _episode_filter(episode, workspace_id: int):
    project = aliased(Project)
    return (
        project,
        or_(
            episode.workspace_id == workspace_id,
            and_(episode.workspace_id.is_(None), project.workspace_id == workspace_id),
        ),
    )


def _scoped_jobs(db: Session, *, workspace_id: int) -> list[JobRun]:
    task_ids = _task_ids_for_workspace(workspace_id)
    task_resource_ids = select(cast(Task.id, String)).where(Task.id.in_(task_ids))

    episode_project, episode_workspace = _episode_filter(EgoEpisode, workspace_id)
    episode_resource_ids = (
        select(cast(EgoEpisode.id, String))
        .outerjoin(episode_project, episode_project.id == EgoEpisode.project_id)
        .where(episode_workspace)
    )
    source_asset_ids = (
        select(EgoEpisode.asset_id)
        .outerjoin(episode_project, episode_project.id == EgoEpisode.project_id)
        .where(episode_workspace, EgoEpisode.kind == EGO_KIND_SOURCE)
    )
    derived_asset_ids = (
        select(EgoEpisode.asset_id)
        .outerjoin(episode_project, episode_project.id == EgoEpisode.project_id)
        .where(episode_workspace, EgoEpisode.kind == EGO_KIND_DERIVED)
    )

    upload_task = aliased(Task)
    upload_project = aliased(Project)
    upload_ids = (
        select(UploadSession.id)
        .outerjoin(upload_task, upload_task.id == UploadSession.task_id)
        .outerjoin(upload_project, upload_project.id == upload_task.project_id)
        .where(
            UploadSession.workspace_id == workspace_id,
            or_(
                UploadSession.task_id.is_(None),
                upload_task.workspace_id == workspace_id,
                and_(
                    upload_task.workspace_id.is_(None), upload_project.workspace_id == workspace_id
                ),
            ),
        )
    )
    revision_resource_ids = select(cast(DatasetRevision.id, String)).where(
        DatasetRevision.workspace_id == workspace_id
    )
    export_resource_ids = (
        select(cast(ExportJob.id, String))
        .join(Project, Project.id == ExportJob.project_id)
        .where(Project.workspace_id == workspace_id)
    )

    return list(
        db.scalars(
            select(JobRun).where(
                or_(
                    and_(JobRun.resource_type == "task", JobRun.resource_id.in_(task_resource_ids)),
                    and_(
                        JobRun.resource_type == "ego_episode",
                        JobRun.resource_id.in_(episode_resource_ids),
                    ),
                    and_(
                        JobRun.resource_type == "whole_source",
                        JobRun.resource_id.in_(source_asset_ids),
                    ),
                    and_(
                        JobRun.resource_type == "derived_asset",
                        JobRun.resource_id.in_(derived_asset_ids),
                    ),
                    and_(
                        JobRun.resource_type == "upload_session", JobRun.resource_id.in_(upload_ids)
                    ),
                    and_(
                        JobRun.resource_type == "dataset_revision",
                        JobRun.resource_id.in_(revision_resource_ids),
                    ),
                    and_(
                        JobRun.resource_type == "export_job",
                        JobRun.resource_id.in_(export_resource_ids),
                    ),
                )
            )
        )
    )


def _job_summary(db: Session, *, workspace_id: int) -> dict[str, object]:
    jobs = _scoped_jobs(db, workspace_id=workspace_id)
    grouped: dict[tuple[str, str], list[JobRun]] = defaultdict(list)
    for job in jobs:
        grouped[(_safe_label(job.queue), _safe_label(job.status))].append(job)
    now = datetime.utcnow()
    return {
        "groups": [
            {
                "queue": queue,
                "status": status,
                "count": len(rows),
                "attempt_count": sum(max(0, int(job.attempt_count or 0)) for job in rows),
            }
            for (queue, status), rows in sorted(grouped.items())
        ],
        "attempt_count": sum(max(0, int(job.attempt_count or 0)) for job in jobs),
        "stalled_count": sum(
            job.status == "running"
            and job.lease_expires_at is not None
            and job.lease_expires_at < now
            for job in jobs
        ),
    }


def _work_item_summary(db: Session, *, workspace_id: int) -> dict[str, object]:
    rows = list(db.scalars(select(WorkItem).where(WorkItem.workspace_id == workspace_id)))
    counts = Counter((_safe_label(item.kind), _safe_label(item.status)) for item in rows)
    return {
        "groups": [
            {"kind": kind, "status": status, "count": count}
            for (kind, status), count in sorted(counts.items())
        ],
        "assignee_ids": sorted(
            {item.assignee_user_id for item in rows if item.assignee_user_id is not None}
        ),
        # WorkItem intentionally has no deadline field. Do not infer an overdue
        # state from creation/update timestamps because that would be misleading.
        "overdue_count": 0,
    }


def _workspace_source_episodes(db: Session, *, workspace_id: int) -> list[EgoEpisode]:
    project, filter_clause = _episode_filter(EgoEpisode, workspace_id)
    return list(
        db.scalars(
            select(EgoEpisode)
            .outerjoin(project, project.id == EgoEpisode.project_id)
            .where(filter_clause, EgoEpisode.kind == EGO_KIND_SOURCE)
        )
    )


def _quality_summary(db: Session, *, workspace_id: int) -> dict[str, object]:
    episodes = _workspace_source_episodes(db, workspace_id=workspace_id)
    statuses = Counter(_safe_label(episode.quality_status) for episode in episodes)
    sources = Counter(
        episode.collection_source_id
        for episode in episodes
        if episode.collection_source_id is not None
    )
    return {
        "status_counts": [
            {"status": status, "count": count} for status, count in sorted(statuses.items())
        ],
        "source_counts": _source_count_items(db, sources),
    }


def _annotation_summary(db: Session, *, workspace_id: int) -> dict[str, object]:
    episode_project, episode_workspace = _episode_filter(EgoEpisode, workspace_id)
    rows = db.execute(
        select(AnnotationRevision.submitted_by_user_id, AnnotationRevision.status)
        .join(EgoEpisode, EgoEpisode.id == AnnotationRevision.ego_episode_id)
        .outerjoin(episode_project, episode_project.id == EgoEpisode.project_id)
        .where(
            episode_workspace,
            AnnotationRevision.submitted_by_user_id.is_not(None),
            AnnotationRevision.status.in_(_COUNTED_ANNOTATION_STATUSES),
        )
    )
    counts = Counter((int(annotator_id), _safe_label(status)) for annotator_id, status in rows)
    return {
        "groups": [
            {"annotator_id": annotator_id, "status": status, "count": count}
            for (annotator_id, status), count in sorted(counts.items())
        ]
    }


def _scoped_review_decisions(db: Session, *, workspace_id: int) -> list[ReviewDecision]:
    annotation = aliased(AnnotationRevision)
    cut_plan = aliased(CutPlanRevision)
    annotation_episode = aliased(EgoEpisode)
    cut_episode = aliased(EgoEpisode)
    annotation_project = aliased(Project)
    cut_project = aliased(Project)
    annotation_workspace = or_(
        annotation_episode.workspace_id == workspace_id,
        and_(
            annotation_episode.workspace_id.is_(None),
            annotation_project.workspace_id == workspace_id,
        ),
    )
    cut_workspace = or_(
        cut_episode.workspace_id == workspace_id,
        and_(cut_episode.workspace_id.is_(None), cut_project.workspace_id == workspace_id),
    )
    return list(
        db.scalars(
            select(ReviewDecision)
            .outerjoin(annotation, annotation.id == ReviewDecision.annotation_revision_id)
            .outerjoin(cut_plan, cut_plan.id == ReviewDecision.cut_plan_revision_id)
            .outerjoin(annotation_episode, annotation_episode.id == annotation.ego_episode_id)
            .outerjoin(cut_episode, cut_episode.id == cut_plan.source_episode_id)
            .outerjoin(annotation_project, annotation_project.id == annotation_episode.project_id)
            .outerjoin(cut_project, cut_project.id == cut_episode.project_id)
            .where(or_(annotation_workspace, cut_workspace))
        )
    )


def _review_summary(db: Session, *, workspace_id: int) -> dict[str, object]:
    decisions = _scoped_review_decisions(db, workspace_id=workspace_id)
    decision_counts = Counter(
        (decision.auditor_id, _safe_label(decision.decision)) for decision in decisions
    )
    decision_ids = [decision.id for decision in decisions]
    findings = (
        list(
            db.scalars(
                select(ReviewFinding).where(ReviewFinding.review_decision_id.in_(decision_ids))
            )
        )
        if decision_ids
        else []
    )
    finding_counts = Counter(
        (_safe_label(item.domain), _safe_label(item.code), _safe_label(item.attribution))
        for item in findings
    )
    decision_source_ids = _review_decision_source_ids(db, decision_ids)
    finding_sources = Counter(
        decision_source_ids[item.review_decision_id]
        for item in findings
        if item.review_decision_id in decision_source_ids
    )
    return {
        "decisions": [
            {"auditor_id": auditor_id, "decision": decision, "count": count}
            for (auditor_id, decision), count in sorted(decision_counts.items())
        ],
        "findings": [
            {"domain": domain, "code": code, "attribution": attribution, "count": count}
            for (domain, code, attribution), count in sorted(finding_counts.items())
        ],
        "finding_source_counts": _source_count_items(db, finding_sources),
    }


def _source_count_items(db: Session, counts: Counter[int]) -> list[dict[str, object]]:
    source_ids = sorted(counts)
    if not source_ids:
        return []
    sources = {
        source.id: source
        for source in db.scalars(
            select(CollectionSource).where(CollectionSource.id.in_(source_ids))
        )
    }
    return [
        {
            "collection_source_id": source_id,
            "label": sources[source_id].label,
            "kind": sources[source_id].kind,
            "count": count,
        }
        for source_id, count in sorted(counts.items())
        if source_id in sources
    ]


def _review_decision_source_ids(db: Session, decision_ids: list[int]) -> dict[int, int]:
    if not decision_ids:
        return {}
    annotation = aliased(AnnotationRevision)
    cut_plan = aliased(CutPlanRevision)
    annotation_episode = aliased(EgoEpisode)
    cut_episode = aliased(EgoEpisode)
    rows = db.execute(
        select(
            ReviewDecision.id,
            annotation_episode.collection_source_id,
            cut_episode.collection_source_id,
        )
        .outerjoin(annotation, annotation.id == ReviewDecision.annotation_revision_id)
        .outerjoin(cut_plan, cut_plan.id == ReviewDecision.cut_plan_revision_id)
        .outerjoin(annotation_episode, annotation_episode.id == annotation.ego_episode_id)
        .outerjoin(cut_episode, cut_episode.id == cut_plan.source_episode_id)
        .where(ReviewDecision.id.in_(decision_ids))
    )
    return {
        decision_id: annotation_source_id if annotation_source_id is not None else cut_source_id
        for decision_id, annotation_source_id, cut_source_id in rows
        if annotation_source_id is not None or cut_source_id is not None
    }


def _safe_identifier(value: object) -> str | None:
    text = str(value or "")
    return text if _SAFE_IDENTIFIER.fullmatch(text) else None


def _safe_label(value: object) -> str:
    """Keep aggregate dimensions useful without returning untrusted text."""
    return _safe_identifier(value) or "redacted"


def _safe_workflow_payload(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, object] = {}
    for key in _SAFE_WORKFLOW_PAYLOAD_KEYS:
        item = value.get(key)
        if isinstance(item, bool):
            result[key] = item
        elif isinstance(item, int) and not isinstance(item, bool):
            result[key] = item
        elif isinstance(item, str) and _SAFE_WORKFLOW_TEXT.fullmatch(item):
            result[key] = item
    return result


def _workflow_summary(db: Session, *, workspace_id: int) -> dict[str, object]:
    rows = list(
        db.scalars(
            select(WorkflowEvent)
            .where(WorkflowEvent.workspace_id == workspace_id)
            .order_by(WorkflowEvent.occurred_at.desc(), WorkflowEvent.id.desc())
            .limit(50)
        )
    )
    events: list[dict[str, Any]] = []
    for event in rows:
        resource_type = _safe_identifier(event.subject_type)
        resource_id = _safe_identifier(event.subject_id)
        status = _safe_identifier(event.to_status)
        if not resource_type or not resource_id or not status:
            continue
        events.append(
            {
                "id": event.id,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "status": status,
                "actor_id": event.actor_id,
                "assignee_id": event.to_assignee_user_id,
                "payload": _safe_workflow_payload(event.payload_json),
                "occurred_at": format_api_datetime(event.occurred_at),
            }
        )
    return {"events": events}
