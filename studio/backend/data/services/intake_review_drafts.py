"""Versioned intake progress bound to the exact package source/admission set."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy.orm import Session

from data.models.data_package import DataPackage, PackageIntakeReview, PackageIntakeReviewDraft
from data.services.collection_intake_review import IntakeReviewConflictError
from data.services.episode_admission import package_episode_admission_rows
from data.utils.formatting import format_api_datetime


def locked_package(db: Session, workspace_id: int, package_id: int) -> DataPackage:
    package = (
        db.query(DataPackage)
        .filter(DataPackage.id == package_id, DataPackage.workspace_id == workspace_id)
        .with_for_update()
        .one_or_none()
    )
    if package is None:
        raise LookupError("data package does not exist in this workspace")
    return package


def source_snapshot(db: Session, package: DataPackage):
    rows = package_episode_admission_rows(
        db, workspace_id=package.workspace_id, data_package_id=package.id, for_update=True
    )
    sources = [
        {
            "episode_id": episode.id,
            "source_fingerprint": episode.source_fingerprint,
            "attempt": fact.attempt if fact else None,
            "fact_id": fact.id if fact else None,
            "eligible": eligible,
            "reason": reason,
        }
        for episode, fact, eligible, reason in rows
    ]
    fingerprint = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()
    return fingerprint, rows


def _draft(db: Session, package_id: int, reviewer_id: int):
    return (
        db.query(PackageIntakeReviewDraft)
        .filter_by(data_package_id=package_id, reviewer_user_id=reviewer_id)
        .one_or_none()
    )


def _editable(db: Session, package: DataPackage) -> bool:
    return (
        package.status == "pending_intake_review"
        and not db.query(PackageIntakeReview.id).filter_by(data_package_id=package.id).first()
    )


def get_draft(db: Session, *, workspace_id: int, package_id: int, reviewer_id: int):
    package = locked_package(db, workspace_id, package_id)
    fingerprint, _ = source_snapshot(db, package)
    row = _draft(db, package_id, reviewer_id)
    return {
        "data_package_id": package_id,
        "workspace_id": workspace_id,
        "draft_version": row.draft_version if row else 0,
        "source_fingerprint": fingerprint,
        "saved_source_fingerprint": row.source_fingerprint if row else fingerprint,
        "source_changed": bool(row and row.source_fingerprint != fingerprint),
        "draft": row.draft_json
        if row
        else {"viewed_episode_ids": [], "rejected_episodes": {}, "last_episode_id": None},
        "editable": _editable(db, package),
        "updated_at": format_api_datetime(row.updated_at) if row else None,
    }


def normalize_draft(draft: dict, episode_ids: set[int]) -> dict:
    if set(draft) - {"viewed_episode_ids", "rejected_episodes", "last_episode_id"}:
        raise ValueError("unknown intake draft fields")
    viewed = draft.get("viewed_episode_ids", [])
    rejected = draft.get("rejected_episodes", {})
    last = draft.get("last_episode_id")
    if not isinstance(viewed, list) or any(
        type(i) is not int or i not in episode_ids for i in viewed
    ):
        raise ValueError("viewed episodes must belong to this package")
    if last is not None and (type(last) is not int or last not in episode_ids):
        raise ValueError("last episode must belong to this package")
    if not isinstance(rejected, dict):
        raise ValueError("rejected_episodes must map Episode IDs to reasons")
    reasons = {}
    for key, reason in rejected.items():
        if (
            not isinstance(key, str)
            or not key.isdecimal()
            or str(int(key)) != key
            or int(key) not in episode_ids
        ):
            raise ValueError("rejected episodes must belong to this package")
        if not isinstance(reason, str) or len(reason) > 1000:
            raise ValueError("episode reason must be at most 1000 characters")
        # Incomplete reasons may be saved; final review requires a nonempty reason.
        reasons[key] = reason.strip()
    return {
        "viewed_episode_ids": sorted(set(viewed)),
        "rejected_episodes": reasons,
        "last_episode_id": last,
    }


def save_draft(
    db: Session,
    *,
    workspace_id: int,
    package_id: int,
    reviewer_id: int,
    base_version: int,
    source_fingerprint: str,
    draft: dict,
):
    package = locked_package(db, workspace_id, package_id)
    if not _editable(db, package):
        raise IntakeReviewConflictError("package_intake_finalized_or_unavailable")
    fingerprint, rows = source_snapshot(db, package)
    if fingerprint != source_fingerprint:
        raise IntakeReviewConflictError("intake_sources_changed")
    row = _draft(db, package_id, reviewer_id)
    if base_version != (row.draft_version if row else 0):
        raise IntakeReviewConflictError("intake_draft_version_conflict")
    normalized = normalize_draft(draft, {episode.id for episode, *_ in rows})
    if row is None:
        row = PackageIntakeReviewDraft(
            data_package_id=package_id, workspace_id=workspace_id, reviewer_user_id=reviewer_id
        )
        db.add(row)
    row.draft_json = normalized
    row.source_fingerprint = fingerprint
    row.draft_version = base_version + 1
    row.updated_at = datetime.utcnow()
    db.flush()
    return get_draft(db, workspace_id=workspace_id, package_id=package_id, reviewer_id=reviewer_id)


def validate_final_draft(
    db: Session,
    package: DataPackage,
    reviewer_id: int,
    *,
    base_version: int,
    source_fingerprint: str,
    rejected_episode_ids: list[int],
    episode_reasons: dict[str, str],
):
    fingerprint, _ = source_snapshot(db, package)
    if fingerprint != source_fingerprint:
        raise IntakeReviewConflictError("intake_sources_changed")
    row = _draft(db, package.id, reviewer_id)
    if row is None or row.draft_version != base_version:
        raise IntakeReviewConflictError("intake_draft_version_conflict")
    if row.source_fingerprint != fingerprint:
        raise IntakeReviewConflictError("intake_sources_changed")
    expected = {str(i) for i in rejected_episode_ids}
    if set(episode_reasons) != expected or any(
        not str(reason).strip() for reason in episode_reasons.values()
    ):
        raise ValueError("each rejected Episode requires its own reason")
    if row.draft_json.get("rejected_episodes", {}) != episode_reasons:
        raise IntakeReviewConflictError("intake_draft_decisions_changed")
