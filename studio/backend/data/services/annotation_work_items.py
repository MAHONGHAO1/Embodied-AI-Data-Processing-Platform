"""Annotation work items: load-balanced assignment, drafting, submission, and reassignment."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from data.database import Episode, EpisodeAnnotation, User, Workspace
from data.models.annotation_work import (
    AnnotationSubmission,
    AnnotationSubmissionReview,
    AnnotationWorkItem,
    ReviewWorkItem,
)
from data.models.data_batch import DataBatch, DataBatchEpisode
from data.models.data_package import DataPackage
from data.security.audit import add_transaction_audit, emit_audit_event

_OPEN_ANNOTATION_STATUSES = frozenset({"assigned", "in_progress", "submitted", "returned"})

_WRITABLE_ANNOTATION_STATUSES = frozenset({"assigned", "in_progress", "returned"})
_SUBMITTABLE_STATUSES = frozenset({"assigned", "in_progress", "returned"})


class AnnotationWorkError(ValueError):
    """Invalid annotation work-item transition."""


class AnnotationWorkConflict(AnnotationWorkError):
    """Conflict such as double-submit or completed reassign."""


class AnnotationWorkForbidden(PermissionError):
    """Caller is not the current assignee."""


def _packages_with_valid_episodes(
    db: Session, *, batch_id: int
) -> list[tuple[DataPackage, list[DataBatchEpisode]]]:
    members = (
        db.query(DataBatchEpisode)
        .filter(DataBatchEpisode.data_batch_id == batch_id)
        .order_by(DataBatchEpisode.episode_id.asc())
        .all()
    )
    if not members:
        return []
    valid_members_by_package: dict[int, list[DataBatchEpisode]] = {}
    for member in members:
        if member.episode is not None and member.episode.validity_status == "valid":
            valid_members_by_package.setdefault(member.data_package_id, []).append(member)
    valid_package_ids = set(valid_members_by_package)
    if not valid_package_ids:
        return []
    packages = (
        db.query(DataPackage)
        .filter(DataPackage.id.in_(sorted(valid_package_ids)))
        .order_by(DataPackage.id.asc())
        .all()
    )
    return [(package, valid_members_by_package[package.id]) for package in packages]


def _pick_assignee(annotator_ids: list[int], loads: dict[int, Decimal]) -> int:
    return min(annotator_ids, key=lambda user_id: (loads[user_id], user_id))


def assign_work_items_for_batch(db: Session, *, batch_id: int) -> list[AnnotationWorkItem]:
    """Greedy duration load-balance; create paired single-review items."""
    batch = db.query(DataBatch).filter(DataBatch.id == batch_id).with_for_update().one_or_none()
    if batch is None:
        raise LookupError("data batch does not exist")
    if not batch.annotation_enabled:
        return []

    existing = (
        db.query(AnnotationWorkItem).filter(AnnotationWorkItem.data_batch_id == batch_id).count()
    )
    if existing:
        return (
            db.query(AnnotationWorkItem)
            .filter(AnnotationWorkItem.data_batch_id == batch_id)
            .order_by(AnnotationWorkItem.id.asc())
            .all()
        )

    annotator_ids = [int(uid) for uid in (batch.annotator_user_ids_json or [])]
    if not annotator_ids:
        raise AnnotationWorkError("batch has no annotators")
    if batch.reviewer_user_id is None:
        raise AnnotationWorkError("batch has no reviewer")

    packages = _packages_with_valid_episodes(db, batch_id=batch_id)
    loads = {user_id: Decimal("0.00") for user_id in annotator_ids}
    assignments: list[dict[str, Any]] = []
    created: list[AnnotationWorkItem] = []

    for package, members in packages:
        episode_members = [
            {
                "episode_id": member.episode_id,
                "admission_attempt": member.admission_attempt,
                "duration_hours": f"{member.duration_hours:.2f}",
            }
            for member in members
        ]
        duration = sum((Decimal(str(member.duration_hours)) for member in members), Decimal("0.00"))
        assignee_id = _pick_assignee(annotator_ids, loads)
        loads[assignee_id] += duration
        item = AnnotationWorkItem(
            data_batch_id=batch.id,
            data_package_id=package.id,
            workspace_id=batch.workspace_id,
            assignee_user_id=assignee_id,
            status="assigned",
            draft_json={},
            episode_members_json=episode_members,
            materialization_provenance_state="known",
            draft_version=0,
            generation=1,
        )
        db.add(item)
        db.flush()
        review = ReviewWorkItem(
            annotation_work_item_id=item.id,
            data_batch_id=batch.id,
            workspace_id=batch.workspace_id,
            assignee_user_id=int(batch.reviewer_user_id),
            status="assigned",
            generation=1,
        )
        db.add(review)
        created.append(item)
        assignments.append(
            {
                "data_package_id": package.id,
                "episode_ids": [member.episode_id for member in members],
                "episode_members": episode_members,
                "assignee_user_id": assignee_id,
                "duration_hours": f"{duration:.2f}",
                "annotation_work_item_id": item.id,
            }
        )

    batch.assignment_snapshot_json = {
        "reviewer_user_id": int(batch.reviewer_user_id),
        "assignments": assignments,
    }
    db.flush()
    emit_audit_event(
        "annotation.assign",
        actor=None,
        resource=f"data_batch:{batch.id}",
        detail={
            "workspace_id": batch.workspace_id,
            "data_batch_id": batch.id,
            "assignment_count": len(assignments),
        },
    )
    return created


def _annotation_list_query(db: Session, *, workspace_id: int | None, actor: User):
    """Admins see every item and others see their own; the workspace is only a filter."""
    query = db.query(AnnotationWorkItem).options(
        joinedload(AnnotationWorkItem.review_item),
        joinedload(AnnotationWorkItem.batch).load_only(DataBatch.name),
        joinedload(AnnotationWorkItem.package).load_only(DataPackage.package_uid),
        joinedload(AnnotationWorkItem.assignee).load_only(User.email),
        joinedload(AnnotationWorkItem.workspace).load_only(Workspace.name),
    )
    if workspace_id is not None:
        query = query.filter(AnnotationWorkItem.workspace_id == workspace_id)
    if actor.role != "admin":
        query = query.filter(AnnotationWorkItem.assignee_user_id == actor.id)
    return query.order_by(AnnotationWorkItem.id.asc())


def list_annotation_work_items(
    db: Session, *, workspace_id: int | None, actor: User
) -> list[AnnotationWorkItem]:
    return _annotation_list_query(db, workspace_id=workspace_id, actor=actor).all()


def list_annotation_work_items_page(
    db: Session, *, workspace_id: int | None, actor: User, limit: int | None, offset: int | None
) -> dict:
    from data.services.list_pagination import paginate_list

    return paginate_list(
        _annotation_list_query(db, workspace_id=workspace_id, actor=actor),
        limit=limit,
        offset=offset,
    )


def _get_item(
    db: Session,
    *,
    workspace_id: int,
    item_id: int,
    for_update: bool = False,
) -> AnnotationWorkItem:
    query = db.query(AnnotationWorkItem).filter(
        AnnotationWorkItem.id == item_id,
        AnnotationWorkItem.workspace_id == workspace_id,
    )
    if for_update:
        probe = query.one_or_none()
        if probe is not None:
            db.query(DataBatch).filter(DataBatch.id == probe.data_batch_id).with_for_update().one()
        query = query.populate_existing().with_for_update()
    item = query.one_or_none()
    if item is None:
        raise LookupError("annotation work item does not exist")
    return item


def _require_assignee(item: AnnotationWorkItem, actor: User) -> None:
    if int(item.assignee_user_id) != int(actor.id):
        raise AnnotationWorkForbidden("only the current assignee can modify this item")


def save_annotation_draft(
    db: Session,
    *,
    workspace_id: int,
    item_id: int,
    actor: User,
    draft_json: dict[str, Any],
    base_version: int | None = None,
    expected_generation: int | None = None,
) -> AnnotationWorkItem:
    item = _get_item(db, workspace_id=workspace_id, item_id=item_id, for_update=True)
    _require_assignee(item, actor)
    if item.status not in _WRITABLE_ANNOTATION_STATUSES:
        raise AnnotationWorkConflict("annotation item is not writable")
    from data.services.package_annotation_workbench import (
        DRAFT_SCHEMA,
        frozen_members,
        normalize_draft,
    )

    strict = (
        draft_json.get("schema") == DRAFT_SCHEMA
        or (item.draft_json or {}).get("schema") == DRAFT_SCHEMA
    )
    _check_expected_versions(
        item, base_version=base_version, expected_generation=expected_generation, legacy=not strict
    )
    if strict:
        members = frozen_members(db, item)
        draft_json = normalize_draft(draft_json, member_ids={episode.id for episode, _ in members})
    item.draft_json = draft_json
    item.draft_version = int(item.draft_version or 0) + 1
    if item.status in {"assigned", "returned"}:
        item.status = "in_progress"
    db.flush()
    return item


def _check_expected_versions(
    item: AnnotationWorkItem,
    *,
    base_version: int | None,
    expected_generation: int | None,
    legacy: bool = False,
) -> None:
    """Central locked compatibility adapter for versionless historical callers."""
    if base_version is None and expected_generation is None and legacy:
        return
    if base_version is None or expected_generation is None:
        raise AnnotationWorkError("annotation_expected_versions_required")
    if int(item.draft_version) != base_version:
        raise AnnotationWorkConflict("annotation_draft_version_conflict")
    if int(item.generation) != expected_generation:
        raise AnnotationWorkConflict("annotation_generation_conflict")


def _materialize_submission(
    db: Session, *, item: AnnotationWorkItem, actor: User, require_mapping: bool, contract: str
) -> AnnotationSubmission:
    from data.services.package_annotation_workbench import validated_submission_entries

    normalized, entries = validated_submission_entries(db, item, require_mapping=require_mapping)
    # Protect the version allocator against other producers of EpisodeAnnotation.
    db.query(Episode).filter(Episode.id.in_([entry["episode_id"] for entry in entries])).order_by(
        Episode.id
    ).with_for_update().all()
    created = []
    for entry in entries:
        payload = entry.pop("annotation_payload")
        entry["annotation_revision_id"] = None
        entry["annotation_version"] = None
        if payload is None:
            continue
        next_version = (
            int(
                db.query(func.coalesce(func.max(EpisodeAnnotation.version), 0))
                .filter(EpisodeAnnotation.episode_id == entry["episode_id"])
                .scalar()
                or 0
            )
            + 1
        )
        annotation = EpisodeAnnotation(
            episode_id=entry["episode_id"],
            version=next_version,
            created_by_user_id=actor.id,
            payload_json=payload,
        )
        db.add(annotation)
        db.flush()
        entry["annotation_revision_id"] = annotation.id
        entry["annotation_version"] = annotation.version
        # This immutable copy prevents a later revision producer from changing
        # which description or range a historical review/asset displays.
        entry["annotation_payload"] = payload
        created.append(
            {
                "episode_id": annotation.episode_id,
                "annotation_id": annotation.id,
                "version": annotation.version,
            }
        )
    submission = AnnotationSubmission(
        annotation_work_item_id=item.id,
        workspace_id=item.workspace_id,
        submitted_by_user_id=actor.id,
        generation=item.generation,
        draft_version=item.draft_version,
        episodes_json=entries,
        draft_json=normalized,
        source_json={
            "contract": contract,
            "source": (item.draft_json or {}).get("source", {}),
            "review_required": (item.draft_json or {}).get("review_required", True),
        },
    )
    db.add(submission)
    db.flush()
    item.current_submission_id = submission.id
    item.materialized_annotation_versions_json = (
        list(item.materialized_annotation_versions_json or []) + created
    )
    item.materialization_provenance_state = "known"
    return submission


def submit_annotation_work_item(
    db: Session,
    *,
    workspace_id: int,
    item_id: int,
    actor: User,
    base_version: int | None = None,
    expected_generation: int | None = None,
    _algorithm_submission: bool = False,
) -> AnnotationWorkItem:
    from data.services.package_annotation_workbench import DRAFT_SCHEMA

    item = _get_item(db, workspace_id=workspace_id, item_id=item_id, for_update=True)
    _require_assignee(item, actor)
    strict = (item.draft_json or {}).get("schema") == DRAFT_SCHEMA
    if item.status == "submitted" and base_version is not None and expected_generation is not None:
        submitted = db.get(AnnotationSubmission, item.current_submission_id)
        if (
            submitted is not None
            and submitted.draft_version == base_version
            and submitted.generation == expected_generation
            and submitted.submitted_by_user_id == actor.id
        ):
            return item
    if item.status not in _SUBMITTABLE_STATUSES:
        raise AnnotationWorkConflict("annotation cannot be submitted")
    _check_expected_versions(
        item, base_version=base_version, expected_generation=expected_generation, legacy=not strict
    )
    contract = "algorithm" if _algorithm_submission else "workbench" if strict else "legacy"
    submission = _materialize_submission(
        db,
        item=item,
        actor=actor,
        require_mapping=strict and not _algorithm_submission,
        contract=contract,
    )
    item.status = "submitted"
    item.generation += 1
    review = (
        db.query(ReviewWorkItem)
        .filter(ReviewWorkItem.annotation_work_item_id == item.id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if review is None:
        raise AnnotationWorkError("annotation_review_item_missing")
    review.status = "assigned"
    review.reason = ""
    review.submission_id = submission.id
    review.generation += 1
    batch = db.get(DataBatch, item.data_batch_id)
    if batch is not None and batch.status == "annotating":
        batch.status = "reviewing"
    db.flush()
    add_transaction_audit(
        db,
        "annotation.submit",
        actor_id=actor.id,
        workspace_id=workspace_id,
        resource_type="annotation_work_item",
        resource_id=item.id,
        detail={
            "data_batch_id": item.data_batch_id,
            "generation": item.generation,
            "submission_id": submission.id,
            "draft_version": submission.draft_version,
        },
    )
    return item


def reassign_annotation_work_item(
    db: Session,
    *,
    workspace_id: int,
    item_id: int,
    to_user_id: int,
    reason: str,
    actor: User,
) -> AnnotationWorkItem:
    if actor.role != "admin":
        raise AnnotationWorkForbidden("only an admin can reassign annotation items")
    if not reason or not reason.strip():
        raise AnnotationWorkError("reassign reason is required")
    item = _get_item(db, workspace_id=workspace_id, item_id=item_id, for_update=True)
    if item.status not in _OPEN_ANNOTATION_STATUSES:
        raise AnnotationWorkConflict("completed annotation cannot be reassigned")
    if int(to_user_id) == int(item.assignee_user_id):
        raise AnnotationWorkError("to_user_id matches current assignee")

    target = db.get(User, to_user_id)
    if target is None or not target.is_active:
        raise LookupError("target user does not exist")
    if target.role != "annotator":
        raise AnnotationWorkError("target user must be an annotator")

    previous_id = int(item.assignee_user_id)
    item.assignee_user_id = int(to_user_id)
    item.generation = int(item.generation or 1) + 1
    item.reassign_reason = reason.strip()
    if item.status == "submitted":
        item.status = "in_progress"
        review = (
            db.query(ReviewWorkItem)
            .filter_by(annotation_work_item_id=item.id)
            .with_for_update()
            .one_or_none()
        )
        if review is not None:
            review.generation += 1
    db.flush()
    emit_audit_event(
        "annotation.reassign",
        actor=actor.email,
        resource=f"annotation_work_item:{item.id}",
        detail={
            "workspace_id": workspace_id,
            "from_user_id": previous_id,
            "to_user_id": to_user_id,
            "reason": reason.strip(),
            "generation": item.generation,
        },
    )
    return item


def _can_skip_review(actor: User) -> bool:
    """Only reviewers may ask for an algorithm result to skip human review."""

    from data.utils.helpers import get_role_permissions

    permissions = get_role_permissions(actor.role)
    return "*" in permissions or "annotation:approve" in permissions


def batch_submit_annotation_items(
    db: Session,
    *,
    workspace_id: int,
    actor: User,
    client_request_id: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Submit many annotation results at once; repeated calls are idempotent.

    Items are dispatched by an administrator: the caller may only submit work
    items whose ``assignee_user_id`` is their own account.
    """

    from data.database import JobRun
    from data.security.audit import add_transaction_audit
    from data.services.job_runs import create_or_get_job_in_transaction

    idempotency_key = f"annotation-batch:{workspace_id}:{actor.id}:{client_request_id}"
    normalized = [
        {
            "work_item_id": int(entry["work_item_id"]),
            "payload": entry.get("payload") or {},
            "source": entry.get("source") or {},
            "review_required": entry.get("review_required", True),
        }
        for entry in items
    ]
    item_ids = [entry["work_item_id"] for entry in normalized]
    if len(set(item_ids)) != len(item_ids):
        raise AnnotationWorkError("duplicate_work_item_id")
    digest = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    # PostgreSQL transaction lock serializes this durable key before any side effect.
    lock_key = int.from_bytes(
        hashlib.sha256(idempotency_key.encode()).digest()[:8], "big", signed=True
    )
    db.execute(select(func.pg_advisory_xact_lock(lock_key)))
    unlocked = [_get_item(db, workspace_id=workspace_id, item_id=i) for i in sorted(item_ids)]
    for item in unlocked:
        _require_assignee(item, actor)
    existing = db.query(JobRun).filter(JobRun.idempotency_key == idempotency_key).one_or_none()
    if existing is not None:
        if (existing.detail_json or {}).get("request_digest") != digest:
            raise AnnotationWorkConflict("idempotency_content_conflict")
        cached = (existing.detail_json or {}).get("result")
        if isinstance(cached, dict):
            return cached
    # All submit/review writers use batch -> annotation -> review lock order.
    db.query(DataBatch).filter(
        DataBatch.id.in_(sorted({i.data_batch_id for i in unlocked}))
    ).order_by(DataBatch.id).with_for_update().all()
    results: list[dict[str, Any]] = []
    for entry in items:
        item = _get_item(
            db, workspace_id=workspace_id, item_id=int(entry["work_item_id"]), for_update=True
        )
        _require_assignee(item, actor)
        payload = dict(entry.get("payload") or {})
        if not payload.get("episodes"):
            raise AnnotationWorkError("annotation_episodes_required")
        source = dict(entry.get("source") or {})
        review_required = bool(entry.get("review_required", True))
        if review_required is False and not _can_skip_review(actor):
            review_required = True
            add_transaction_audit(
                db,
                "annotation.review_skip_downgraded",
                actor_id=actor.id,
                workspace_id=workspace_id,
                resource_type="annotation_work_item",
                resource_id=item.id,
            )
        from data.services.package_annotation_workbench import adapt_legacy_qrdf_draft

        item = save_annotation_draft(
            db,
            workspace_id=workspace_id,
            item_id=item.id,
            actor=actor,
            draft_json=adapt_legacy_qrdf_draft(
                {**payload, "source": source, "review_required": review_required}
            ),
            base_version=item.draft_version,
            expected_generation=item.generation,
        )
        submit_annotation_work_item(
            db,
            workspace_id=workspace_id,
            item_id=item.id,
            actor=actor,
            base_version=item.draft_version,
            expected_generation=item.generation,
            _algorithm_submission=True,
        )
        if not review_required:
            from data.services.review_work_items import _maybe_publish_batch

            review = (
                db.query(ReviewWorkItem)
                .filter(ReviewWorkItem.annotation_work_item_id == item.id)
                .with_for_update()
                .one_or_none()
            )
            if review is not None:
                review.status = "approved"
                review.reason = "authorized_review_skip"
                db.add(
                    AnnotationSubmissionReview(
                        submission_id=item.current_submission_id,
                        review_work_item_id=review.id,
                        reviewer_user_id=actor.id,
                        generation=review.generation,
                        decision="approved",
                        reason="authorized_review_skip",
                    )
                )
                review.generation += 1
            item.status = "done"
            item.generation += 1
            batch = db.get(DataBatch, item.data_batch_id)
            db.flush()
            add_transaction_audit(
                db,
                "annotation.review_skipped",
                actor_id=actor.id,
                workspace_id=workspace_id,
                resource_type="annotation_work_item",
                resource_id=item.id,
                detail={"source": source, "permission": "annotation:approve"},
            )
            if _maybe_publish_batch(db, batch):
                from data.services.data_assets import publish_data_asset

                publish_data_asset(db, batch_id=batch.id)
        results.append(
            {
                "work_item_id": item.id,
                "status": item.status,
                "submission_id": item.current_submission_id,
                "review_required": review_required,
                "source": source,
            }
        )

    result = {"items": results}
    job = create_or_get_job_in_transaction(
        db,
        kind="annotation_batch_submit",
        resource_type="platform",
        resource_id=client_request_id,
        idempotency_key=idempotency_key,
        queue="control",
        actor_id=actor.id,
        workspace_id=workspace_id,
        detail={"result": result, "request_digest": digest},
    )
    job.status = "succeeded"
    db.flush()
    return result
