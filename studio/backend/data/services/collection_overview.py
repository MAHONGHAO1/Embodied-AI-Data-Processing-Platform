"""Collection management overview aggregates."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from data.database import Episode
from data.models.collection_core import CollectionTask
from data.models.data_package import DataPackage, PackageIntakeReview
from data.services.collection_duration import (
    accepted_intake_episode_sql,
    episode_duration_sql,
    latest_intake_review_ids,
)
from data.services.episode_visibility import IMPORT_PLACEHOLDER_WORKFLOW_STATUSES


def get_collection_overview(
    db: Session,
    *,
    workspace_id: int,
    collection_project_id: int | None = None,
) -> dict[str, object]:
    """Aggregate task targets, intake-valid duration, and package statuses."""
    task_query = db.query(
        func.coalesce(
            func.sum(CollectionTask.target_duration_hours),
            Decimal("0.00"),
        )
    ).filter(CollectionTask.workspace_id == workspace_id)
    package_query = db.query(
        func.coalesce(
            func.sum(DataPackage.intake_valid_duration_hours),
            Decimal("0.00"),
        ),
        func.sum(case((DataPackage.status == "pending_assignment", 1), else_=0)),
        func.sum(case((DataPackage.status == "assigned", 1), else_=0)),
        func.sum(case((DataPackage.status == "voided", 1), else_=0)),
        func.sum(
            case(
                (
                    DataPackage.status.notin_(("pending_assignment", "assigned", "voided")),
                    1,
                ),
                else_=0,
            )
        ),
    ).filter(DataPackage.workspace_id == workspace_id)

    if collection_project_id is not None:
        task_query = task_query.filter(
            CollectionTask.collection_project_id == collection_project_id
        )
        package_query = package_query.filter(
            DataPackage.collection_project_id == collection_project_id
        )

    target_duration = task_query.scalar()
    intake_duration, pending, assigned, voided, other = package_query.one()
    latest_review = latest_intake_review_ids()
    duration = episode_duration_sql()
    accepted = accepted_intake_episode_sql()
    exact_query = (
        db.query(
            func.count(Episode.id),
            func.count(duration),
            func.sum(duration),
            func.sum(case((accepted, 1), else_=0)),
            func.count(case((accepted, duration))),
            func.sum(case((accepted, duration), else_=0)),
        )
        .select_from(Episode)
        .join(DataPackage, DataPackage.id == Episode.data_package_id)
        .outerjoin(latest_review, latest_review.c.data_package_id == DataPackage.id)
        .outerjoin(PackageIntakeReview, PackageIntakeReview.id == latest_review.c.review_id)
        .filter(
            DataPackage.workspace_id == workspace_id,
            Episode.kind == "source",
            Episode.workflow_status.notin_(IMPORT_PLACEHOLDER_WORKFLOW_STATUSES),
        )
    )
    if collection_project_id is not None:
        exact_query = exact_query.filter(DataPackage.collection_project_id == collection_project_id)
    count, known, captured_s, accepted_count, accepted_known, intake_s = exact_query.one()
    return {
        "workspace_id": workspace_id,
        "collection_project_id": collection_project_id,
        "target_duration_hours": Decimal(target_duration or 0),
        "intake_valid_duration_hours": Decimal(intake_duration or 0),
        "target_duration_s": float(Decimal(target_duration or 0) * 3600),
        "captured_duration_s": float(captured_s or 0) if count == known else None,
        "intake_valid_duration_s": float(intake_s or 0)
        if int(accepted_count or 0) == accepted_known
        else None,
        "package_counts": {
            "pending_assignment": int(pending or 0),
            "assigned": int(assigned or 0),
            "voided": int(voided or 0),
            "other": int(other or 0),
        },
        "duration_basis": "intake_valid_duration_s",
    }
