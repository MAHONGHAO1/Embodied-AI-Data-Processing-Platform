"""Data package intake review business logic service."""

from __future__ import annotations

import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy.orm import Session

from data.database import Episode
from data.models.data_package import DataPackage, PackageIntakeReview
from data.security.audit import add_transaction_audit
from data.services.episode_admission import package_episode_admission_rows
from data.services.package_dashboard_facts import (
    REJECTED_INTAKE_FACTS,
    apply_facts,
    capture_facts,
    intake_valid_facts,
)

_SECONDS_PER_HOUR = Decimal(3600)
_DURATION_QUANTUM = Decimal("0.01")


class IntakeReviewConflictError(ValueError):
    """Raised when data package intake review can no longer proceed (e.g., state conflict or already reviewed without fact change)."""


def _episode_duration_hours(episode: Episode) -> Decimal:
    metadata = episode.metadata_json if isinstance(episode.metadata_json, dict) else {}
    timing = metadata.get("timing")
    timing = timing if isinstance(timing, dict) else {}
    metrics = metadata.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    duration_seconds = timing.get("duration_s", metrics.get("duration_s"))
    duration_hours = timing.get("duration_hours", metadata.get("duration_hours"))
    try:
        duration = (
            Decimal(str(duration_seconds)) / _SECONDS_PER_HOUR
            if duration_seconds is not None
            else Decimal(str(duration_hours or 0))
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"episode {episode.id} has invalid duration metadata") from exc
    if not duration.is_finite() or duration < 0:
        raise ValueError(f"episode {episode.id} has invalid duration metadata")
    return duration


def review_data_package_intake(
    db: Session,
    *,
    workspace_id: int,
    data_package_id: int,
    reviewer_user_id: int | None,
    verdict: str,
    rejected_episode_ids: list[int],
    reason: str,
    is_bulk: bool = False,
    base_version: int | None = None,
    source_fingerprint: str | None = None,
    episode_reasons: dict[str, str] | None = None,
) -> tuple[DataPackage, PackageIntakeReview]:
    """Apply an immutable review conclusion (approved or rejected) to a pending intake package."""
    package = (
        db.query(DataPackage)
        .filter(
            DataPackage.id == data_package_id,
            DataPackage.workspace_id == workspace_id,
        )
        .with_for_update()
        .one_or_none()
    )
    if package is None:
        raise LookupError("data package does not exist in this workspace")
    reviews = (
        db.query(PackageIntakeReview)
        .filter(PackageIntakeReview.data_package_id == data_package_id)
        .order_by(PackageIntakeReview.id.asc())
        .all()
    )
    draft_binding = (
        {
            "base_version": base_version,
            "source_fingerprint": source_fingerprint,
            "episode_reasons": episode_reasons or {},
        }
        if base_version is not None
        else {}
    )
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "verdict": verdict,
                "rejected": sorted(set(rejected_episode_ids)),
                "reason": reason.strip(),
                "reviewer": reviewer_user_id,
                "is_bulk": is_bulk,
                **draft_binding,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if reviews:
        latest = reviews[-1]
        if latest.request_fingerprint == fingerprint:
            return package, latest
        raise IntakeReviewConflictError("already_reviewed")
    if package.status != "pending_intake_review":
        raise IntakeReviewConflictError(f"data package {package.id} must be pending_intake_review")

    if base_version is not None:
        from data.services.intake_review_drafts import validate_final_draft

        validate_final_draft(
            db,
            package,
            reviewer_user_id,
            base_version=base_version,
            source_fingerprint=source_fingerprint,
            rejected_episode_ids=rejected_episode_ids,
            episode_reasons=episode_reasons or {},
        )

    admission_rows = package_episode_admission_rows(
        db,
        workspace_id=workspace_id,
        data_package_id=data_package_id,
        for_update=True,
        require_content_checks=False,
    )
    episodes = [row[0] for row in admission_rows]
    package_episode_ids = {episode.id for episode in episodes}
    rejected_ids = set(rejected_episode_ids)
    if not rejected_ids.issubset(package_episode_ids):
        raise ValueError("rejected_episode_ids must belong to the reviewed package")

    current_attempts = {
        str(episode.id): fact.attempt
        for episode, fact, _eligible, _reason in admission_rows
        if fact is not None
    }
    accepted_ids: list[int] = []
    excluded: list[dict[str, object]] = []
    if verdict == "rejected":
        rejected_ids = package_episode_ids
        for episode in episodes:
            episode.validity_status = "intake_rejected"
        package.status = "voided"
        package.intake_valid_duration_hours = Decimal("0.00")
        apply_facts(package, capture_facts(db, package.id))
        apply_facts(package, REJECTED_INTAKE_FACTS)
    else:
        for episode, _fact, eligible, exclusion_reason in admission_rows:
            if episode.id in rejected_ids:
                episode.validity_status = "intake_rejected"
            elif eligible:
                episode.validity_status = "valid"
                accepted_ids.append(episode.id)
            else:
                excluded.append({"episode_id": episode.id, "reason": exclusion_reason})
        valid_duration = sum(
            (
                _episode_duration_hours(episode)
                for episode in episodes
                if episode.id in accepted_ids
            ),
            Decimal(0),
        )
        package.status = "intake_approved"
        package.intake_valid_duration_hours = valid_duration.quantize(
            _DURATION_QUANTUM,
            rounding=ROUND_HALF_UP,
        )
        apply_facts(package, capture_facts(db, package.id))
        apply_facts(
            package,
            intake_valid_facts(
                (episode, fact)
                for episode, fact, _eligible, _reason in admission_rows
                if episode.id in accepted_ids
            ),
        )

    review = PackageIntakeReview(
        data_package_id=package.id,
        reviewer_user_id=reviewer_user_id,
        verdict=verdict,
        is_bulk=is_bulk,
        accepted_episode_ids_json=sorted(accepted_ids),
        rejected_episode_ids_json=sorted(rejected_ids),
        excluded_episodes_json=excluded,
        fact_attempts_json=current_attempts,
        reason=reason.strip(),
        request_fingerprint=fingerprint,
        episode_reasons_json=episode_reasons or {},
        source_fingerprint=source_fingerprint,
    )
    add_transaction_audit(
        db,
        "collection.intake.review",
        actor_id=reviewer_user_id,
        workspace_id=workspace_id,
        resource_type="data_package",
        resource_id=package.id,
        detail={
            "verdict": verdict,
            "is_bulk": is_bulk,
            "accepted_episode_ids": accepted_ids,
            "rejected_episode_ids": sorted(rejected_ids),
        },
    )
    db.add(review)
    db.flush()
    return package, review


def bulk_approve_data_package_intake(
    db: Session,
    *,
    workspace_id: int,
    data_package_ids: list[int],
    reviewer_user_id: int | None,
) -> list[DataPackage]:
    """Atomically bulk approve pending intake packages after locking and validating all packages."""
    requested_ids = sorted(set(data_package_ids))
    packages = (
        db.query(DataPackage)
        .filter(
            DataPackage.workspace_id == workspace_id,
            DataPackage.id.in_(requested_ids),
        )
        .order_by(DataPackage.id.asc())
        .with_for_update()
        .all()
    )
    found_ids = {package.id for package in packages}
    missing_ids = sorted(set(requested_ids) - found_ids)
    if missing_ids:
        raise LookupError(f"data packages do not exist in this workspace: {missing_ids}")

    # Validate the complete set before creating any review rows.  The caller
    # gets one deterministic conflict and the transaction stays all-or-nothing
    # when a package was reviewed between list and submit.
    conflict_ids = sorted(
        package.id for package in packages if package.status != "pending_intake_review"
    )
    if conflict_ids:
        raise IntakeReviewConflictError(f"packages_not_pending_intake_review: {conflict_ids}")

    approved = []
    for package in packages:
        reviewed_package, _ = review_data_package_intake(
            db,
            workspace_id=workspace_id,
            data_package_id=package.id,
            reviewer_user_id=reviewer_user_id,
            verdict="approved",
            rejected_episode_ids=[],
            reason="",
            is_bulk=True,
        )
        approved.append(reviewed_package)
    return approved
