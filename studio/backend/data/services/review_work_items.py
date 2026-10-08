"""Review work items: approve, return, and reassign (single review for current phase)."""

from __future__ import annotations

from sqlalchemy.orm import Session, joinedload

from data.database import User, Workspace
from data.models.annotation_work import (
    AnnotationSubmission,
    AnnotationSubmissionReview,
    AnnotationWorkItem,
    ReviewWorkItem,
)
from data.models.data_batch import DataBatch
from data.models.data_package import DataPackage
from data.security.audit import emit_audit_event

_OPEN_REVIEW_STATUSES = frozenset({"assigned", "in_progress", "returned"})
_ACTIONABLE_REVIEW_STATUSES = frozenset({"assigned", "in_progress", "returned"})


class ReviewWorkError(ValueError):
    """Invalid review work-item transition."""


class ReviewWorkConflict(ReviewWorkError):
    """Conflict such as double-approve."""


class ReviewWorkForbidden(PermissionError):
    """Caller is not the current reviewer."""


def _review_list_query(db: Session, *, workspace_id: int | None, actor: User):
    """Admins see every item and others see their own; the workspace is only a filter."""
    query = db.query(ReviewWorkItem).options(
        joinedload(ReviewWorkItem.annotation_item)
        .load_only(
            AnnotationWorkItem.status,
            AnnotationWorkItem.data_package_id,
            AnnotationWorkItem.episode_members_json,
        )
        .joinedload(AnnotationWorkItem.package)
        .load_only(DataPackage.package_uid),
        joinedload(ReviewWorkItem.submission).load_only(AnnotationSubmission.source_json),
        joinedload(ReviewWorkItem.batch).load_only(DataBatch.name),
        joinedload(ReviewWorkItem.assignee).load_only(User.email),
        joinedload(ReviewWorkItem.workspace).load_only(Workspace.name),
    )
    if workspace_id is not None:
        query = query.filter(ReviewWorkItem.workspace_id == workspace_id)
    if actor.role != "admin":
        query = query.filter(ReviewWorkItem.assignee_user_id == actor.id)
    return query.order_by(ReviewWorkItem.id.asc())


def list_review_work_items(
    db: Session, *, workspace_id: int | None, actor: User
) -> list[ReviewWorkItem]:
    return _review_list_query(db, workspace_id=workspace_id, actor=actor).all()


def list_review_work_items_page(
    db: Session, *, workspace_id: int | None, actor: User, limit: int | None, offset: int | None
) -> dict:
    from data.services.list_pagination import paginate_list

    return paginate_list(
        _review_list_query(db, workspace_id=workspace_id, actor=actor), limit=limit, offset=offset
    )


def _get_item(
    db: Session,
    *,
    workspace_id: int,
    item_id: int,
    for_update: bool = False,
) -> ReviewWorkItem:
    query = db.query(ReviewWorkItem).filter(
        ReviewWorkItem.id == item_id,
        ReviewWorkItem.workspace_id == workspace_id,
    )
    if for_update:
        query = query.populate_existing().with_for_update()
    item = query.one_or_none()
    if item is None:
        raise LookupError("review work item does not exist")
    return item


def _locked_annotation(db: Session, annotation_id: int) -> AnnotationWorkItem:
    probe = db.get(AnnotationWorkItem, annotation_id)
    if probe is not None:
        _locked_batch(db, probe.data_batch_id)
    annotation = (
        db.query(AnnotationWorkItem)
        .filter(AnnotationWorkItem.id == annotation_id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if annotation is None:
        raise LookupError("annotation work item does not exist")
    return annotation


def _locked_batch(db: Session, batch_id: int) -> DataBatch:
    batch = db.query(DataBatch).filter(DataBatch.id == batch_id).with_for_update().one_or_none()
    if batch is None:
        raise LookupError("data batch does not exist")
    return batch


def _require_assignee(item: ReviewWorkItem, actor: User) -> None:
    if int(item.assignee_user_id) != int(actor.id):
        raise ReviewWorkForbidden("only the current reviewer can act on this item")


def _maybe_publish_batch(db: Session, batch: DataBatch) -> bool:
    """Transition batch to publishing under an existing row lock.

    Returns True when this caller is responsible for scheduling asset creation.
    """
    if batch.status in {"publishing", "published", "no_publishable_asset"}:
        return False
    pending = (
        db.query(ReviewWorkItem)
        .filter(
            ReviewWorkItem.data_batch_id == batch.id,
            ReviewWorkItem.status != "approved",
        )
        .count()
    )
    unfinished = (
        db.query(AnnotationWorkItem)
        .filter(AnnotationWorkItem.data_batch_id == batch.id, AnnotationWorkItem.status != "done")
        .count()
    )
    if pending or unfinished:
        return False
    batch.status = "publishing"
    db.flush()
    return True


def _check_submission(
    db: Session,
    item: ReviewWorkItem,
    annotation: AnnotationWorkItem,
    *,
    expected_submission_id: int | None,
    expected_generation: int | None,
) -> AnnotationSubmission:
    """Only the exact current immutable submission can receive a decision."""
    submission = db.get(AnnotationSubmission, item.submission_id) if item.submission_id else None
    if (
        submission is None
        or item.submission_id != annotation.current_submission_id
        or submission.annotation_work_item_id != annotation.id
        or submission.workspace_id != item.workspace_id
    ):
        raise ReviewWorkConflict("annotation_historical_result_uncertain")
    legacy = (submission.source_json or {}).get("contract") in {"algorithm", "legacy"}
    if expected_submission_id is None and expected_generation is None and legacy:
        return submission
    if expected_submission_id is None or expected_generation is None:
        raise ReviewWorkError("review_expected_versions_required")
    if expected_submission_id != submission.id:
        raise ReviewWorkConflict("review_submission_conflict")
    if expected_generation != item.generation:
        raise ReviewWorkConflict("review_generation_conflict")
    return submission


def _record_decision(
    db: Session, item: ReviewWorkItem, actor: User, decision: str, reason: str = ""
):
    db.add(
        AnnotationSubmissionReview(
            submission_id=item.submission_id,
            review_work_item_id=item.id,
            reviewer_user_id=actor.id,
            generation=item.generation,
            decision=decision,
            reason=reason,
        )
    )


def approve_review_work_item(
    db: Session,
    *,
    workspace_id: int,
    item_id: int,
    actor: User,
    expected_submission_id: int | None = None,
    expected_generation: int | None = None,
) -> tuple[ReviewWorkItem, bool]:
    # All writers use batch -> annotation -> review lock order.
    unlocked = _get_item(db, workspace_id=workspace_id, item_id=item_id)
    annotation = _locked_annotation(db, unlocked.annotation_work_item_id)
    item = _get_item(db, workspace_id=workspace_id, item_id=item_id, for_update=True)
    _require_assignee(item, actor)
    if item.status == "approved":
        raise ReviewWorkConflict("review already approved")
    if item.status == "done":
        raise ReviewWorkConflict("review already completed")
    if item.status not in _ACTIONABLE_REVIEW_STATUSES:
        raise ReviewWorkConflict("review cannot be approved")
    if annotation.status != "submitted":
        raise ReviewWorkConflict("annotation is not submitted")

    _check_submission(
        db,
        item,
        annotation,
        expected_submission_id=expected_submission_id,
        expected_generation=expected_generation,
    )
    _record_decision(db, item, actor, "approved")
    item.status = "approved"
    item.generation += 1
    annotation.generation += 1
    item.reason = ""
    annotation.status = "done"
    annotation.return_reason = ""
    batch = _locked_batch(db, item.data_batch_id)
    db.flush()
    should_schedule_asset = _maybe_publish_batch(db, batch)
    emit_audit_event(
        "review.approve",
        actor=actor.email,
        resource=f"review_work_item:{item.id}",
        detail={
            "workspace_id": workspace_id,
            "data_batch_id": item.data_batch_id,
            "annotation_work_item_id": item.annotation_work_item_id,
        },
    )
    return item, should_schedule_asset


def return_review_work_item(
    db: Session,
    *,
    workspace_id: int,
    item_id: int,
    actor: User,
    reason: str,
    expected_submission_id: int | None = None,
    expected_generation: int | None = None,
) -> ReviewWorkItem:
    if not reason or not str(reason).strip():
        raise ReviewWorkError("return reason is required")
    # Lock order: AnnotationWorkItem → ReviewWorkItem (never Rev→Ann).
    unlocked = _get_item(db, workspace_id=workspace_id, item_id=item_id)
    annotation = _locked_annotation(db, unlocked.annotation_work_item_id)
    item = _get_item(db, workspace_id=workspace_id, item_id=item_id, for_update=True)
    _require_assignee(item, actor)
    if item.status in {"approved", "done"}:
        raise ReviewWorkConflict("completed review cannot be returned")
    if item.status not in _ACTIONABLE_REVIEW_STATUSES:
        raise ReviewWorkConflict("review cannot be returned")
    if annotation.status != "submitted":
        raise ReviewWorkConflict("annotation is not submitted")

    _check_submission(
        db,
        item,
        annotation,
        expected_submission_id=expected_submission_id,
        expected_generation=expected_generation,
    )
    cleaned = str(reason).strip()
    _record_decision(db, item, actor, "returned", cleaned)
    item.generation += 1
    annotation.generation += 1
    item.status = "returned"
    item.reason = cleaned
    annotation.status = "returned"
    annotation.return_reason = cleaned
    db.flush()
    emit_audit_event(
        "review.return",
        actor=actor.email,
        resource=f"review_work_item:{item.id}",
        detail={
            "workspace_id": workspace_id,
            "data_batch_id": item.data_batch_id,
            "reason": cleaned,
        },
    )
    return item


def reassign_review_work_item(
    db: Session,
    *,
    workspace_id: int,
    item_id: int,
    to_user_id: int,
    reason: str,
    actor: User,
) -> ReviewWorkItem:
    if actor.role != "admin":
        raise ReviewWorkForbidden("only an admin can reassign review items")
    if not reason or not reason.strip():
        raise ReviewWorkError("reassign reason is required")
    item = _get_item(db, workspace_id=workspace_id, item_id=item_id, for_update=True)
    if item.status not in _OPEN_REVIEW_STATUSES | {"returned"}:
        if item.status in {"approved", "done"}:
            raise ReviewWorkConflict("completed review cannot be reassigned")
        raise ReviewWorkConflict("review cannot be reassigned")
    if int(to_user_id) == int(item.assignee_user_id):
        raise ReviewWorkError("to_user_id matches current assignee")

    target = db.get(User, to_user_id)
    if target is None or not target.is_active:
        raise LookupError("target user does not exist")
    if target.role != "auditor":
        raise ReviewWorkError("target user must be an auditor")

    previous_id = int(item.assignee_user_id)
    item.assignee_user_id = int(to_user_id)
    item.generation = int(item.generation or 1) + 1
    item.reassign_reason = reason.strip()
    db.flush()
    emit_audit_event(
        "review.reassign",
        actor=actor.email,
        resource=f"review_work_item:{item.id}",
        detail={
            "workspace_id": workspace_id,
            "from_user_id": previous_id,
            "to_user_id": to_user_id,
            "reason": reason.strip(),
            "generation": item.generation,
        },
    )
    return item
