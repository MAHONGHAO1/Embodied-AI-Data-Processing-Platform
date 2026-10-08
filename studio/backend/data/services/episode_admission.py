"""Database-only Episode admission facts and eligibility gates."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from data.database import Episode
from data.models.data_package import DataPackage, PackageIntakeReview
from data.models.episode_admission import EpisodeAdmissionFact

REQUIRED_ADMISSION_POLICY_VERSION = "v1"
_READY_PREVIEW_STATES = frozenset({"ready", "not_applicable"})


class EpisodeAdmissionConflictError(ValueError):
    """Raised when an admission worker attempts to replace a newer attempt."""


def record_episode_admission_fact(
    db: Session,
    *,
    episode_id: int,
    attempt: int,
    source_fingerprint: str,
    validation_policy_version: str,
    integrity_status: str,
    preview_status: str,
    output_verification_status: str,
    qrdf_profile: str = "",
    report_ref: Any = None,
    objects: list[Any] | None = None,
    integrity_source: str = "server",
    error_code: str = "",
    error_message: str = "",
) -> EpisodeAdmissionFact:
    """Persist one worker conclusion, making only its attempt current.

    An identical retry is idempotent; any changed payload requires a new
    attempt (including pending/running to terminal transitions).
    A worker from an older attempt cannot overwrite the current fact because
    the episode row is locked and
    the attempt ordering is checked in the same transaction.
    """

    if attempt < 1:
        raise ValueError("attempt must be positive")

    if integrity_source not in {"client", "server"}:
        raise ValueError("integrity_source must be client or server")

    # Worker conclusions are an API boundary: reject provider SDK objects,
    # paths, or other values that cannot survive a JSON/JSONB round trip.
    def _json_value(value: Any) -> Any:
        return asdict(value) if is_dataclass(value) else value

    normalized_report_ref = _json_value(report_ref or {})
    try:
        json.dumps(normalized_report_ref)
    except (TypeError, ValueError) as exc:
        raise ValueError("report_ref must be JSON serializable") from exc

    from data.services.episode_objects import (
        EpisodeObjectsError,
        parse_objects,
        require_verified_objects,
    )

    try:
        parsed_objects = parse_objects(list(objects or []))
        if output_verification_status == "verified":
            require_verified_objects(parsed_objects)
    except EpisodeObjectsError as exc:
        raise ValueError(f"episode_objects_unverified:{exc.code}") from exc
    normalized_objects = [item.as_entry() for item in parsed_objects]

    episode = db.get(Episode, episode_id)
    if episode is not None and episode.data_package_id is not None:
        db.query(DataPackage.id).filter(
            DataPackage.id == episode.data_package_id
        ).with_for_update().one()
        if (
            db.query(PackageIntakeReview.id)
            .filter(PackageIntakeReview.data_package_id == episode.data_package_id)
            .first()
        ):
            raise EpisodeAdmissionConflictError("package_intake_finalized")
    episode = db.query(Episode).filter(Episode.id == episode_id).with_for_update().one_or_none()
    if episode is None:
        raise LookupError("episode does not exist")

    current = (
        db.query(EpisodeAdmissionFact)
        .filter(
            EpisodeAdmissionFact.episode_id == episode_id,
            EpisodeAdmissionFact.is_current.is_(True),
        )
        .with_for_update()
        .one_or_none()
    )
    if current is not None and attempt < current.attempt:
        raise EpisodeAdmissionConflictError("stale_admission_attempt")

    fact = (
        db.query(EpisodeAdmissionFact)
        .filter(
            EpisodeAdmissionFact.episode_id == episode_id,
            EpisodeAdmissionFact.attempt == attempt,
        )
        .with_for_update()
        .one_or_none()
    )
    if fact is not None and current is not None and fact.id == current.id:
        same_payload = (
            fact.source_fingerprint == str(source_fingerprint or "")
            and fact.validation_policy_version == str(validation_policy_version or "")
            and fact.integrity_status == integrity_status
            and fact.preview_status == preview_status
            and fact.output_verification_status == output_verification_status
            and fact.qrdf_profile == (qrdf_profile or "")
            and fact.report_ref_json == normalized_report_ref
            and fact.objects_json == normalized_objects
            and fact.error_code == (error_code or "")
            and fact.error_message == (error_message or "")
            and fact.integrity_source == integrity_source
        )
        if same_payload:
            return fact
        raise EpisodeAdmissionConflictError("admission_attempt_conflict")
    if fact is None:
        fact = EpisodeAdmissionFact(
            episode_id=episode_id,
            attempt=attempt,
            is_current=True,
        )
        db.add(fact)
    if current is not None and current.id != fact.id:
        current.is_current = False
    fact.is_current = True
    fact.source_fingerprint = str(source_fingerprint or "")
    fact.validation_policy_version = str(validation_policy_version or "")
    fact.integrity_status = integrity_status
    fact.preview_status = preview_status
    fact.output_verification_status = output_verification_status
    fact.qrdf_profile = qrdf_profile or ""
    fact.report_ref_json = normalized_report_ref
    fact.objects_json = normalized_objects
    fact.integrity_source = integrity_source
    fact.error_code = error_code or ""
    fact.error_message = error_message or ""
    fact.checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.flush()
    return fact


def current_episode_admission_fact(
    db: Session, *, episode_id: int, for_update: bool = False
) -> EpisodeAdmissionFact | None:
    query = db.query(EpisodeAdmissionFact).filter(
        EpisodeAdmissionFact.episode_id == episode_id,
        EpisodeAdmissionFact.is_current.is_(True),
    )
    if for_update:
        query = query.with_for_update()
    return query.one_or_none()


def admission_eligibility(
    episode: Episode,
    fact: EpisodeAdmissionFact | None,
    *,
    required_policy_version: str = REQUIRED_ADMISSION_POLICY_VERSION,
    require_content_checks: bool = True,
) -> tuple[bool, str]:
    """Evaluate eligibility from persisted fields only; fail closed."""

    if fact is None:
        return False, "admission_fact_missing"
    if not fact.is_current:
        return False, "admission_fact_not_current"
    if not episode.source_fingerprint or not fact.source_fingerprint:
        return False, "source_fingerprint_missing"
    if fact.source_fingerprint != episode.source_fingerprint:
        return False, "source_fingerprint_changed"
    if fact.validation_policy_version != required_policy_version:
        return False, "validation_policy_outdated"
    if fact.integrity_status != "passed":
        return False, f"integrity_{fact.integrity_status}"
    # Intake is the completeness gate. Preview/output/report checks belong to
    # data-batch construction and are deliberately deferred there.
    if require_content_checks:
        if fact.preview_status not in _READY_PREVIEW_STATES:
            return False, f"preview_{fact.preview_status}"
        if fact.output_verification_status != "verified":
            return False, f"output_{fact.output_verification_status}"
        if not isinstance(fact.report_ref_json, dict) or not fact.report_ref_json:
            return False, "report_missing"
    if episode.validity_status != "valid":
        return False, f"episode_{episode.validity_status}"
    return True, ""


def package_episode_admission_rows(
    db: Session,
    *,
    workspace_id: int,
    data_package_id: int,
    for_update: bool = False,
    require_content_checks: bool = True,
) -> list[tuple[Episode, EpisodeAdmissionFact | None, bool, str]]:
    query = (
        db.query(Episode)
        .filter(
            Episode.workspace_id == workspace_id,
            Episode.data_package_id == data_package_id,
        )
        .order_by(Episode.id.asc())
    )
    if for_update:
        query = query.with_for_update()
    episodes = query.all()
    if not episodes:
        return []
    facts = (
        db.query(EpisodeAdmissionFact)
        .filter(
            EpisodeAdmissionFact.episode_id.in_([episode.id for episode in episodes]),
            EpisodeAdmissionFact.is_current.is_(True),
        )
        .all()
    )
    facts_by_episode = {fact.episode_id: fact for fact in facts}
    return [
        (
            episode,
            facts_by_episode.get(episode.id),
            *admission_eligibility(
                episode,
                facts_by_episode.get(episode.id),
                require_content_checks=require_content_checks,
            ),
        )
        for episode in episodes
    ]


def package_admission_counts(
    db: Session,
    *,
    workspace_id: int,
    data_package_id: int,
) -> dict[str, int]:
    counts = {"ready": 0, "running": 0, "failed": 0, "reviewed": 0}
    package = db.get(DataPackage, data_package_id)
    if package is not None and package.workspace_id == workspace_id:
        source_states = (
            (package.qrdf_facts_json or {}).get("source_admission", {}).get("sources", [])
        )
        counts["failed"] += sum(
            state.get("status") == "failed" and state.get("episode_id") is None
            for state in source_states
        )
    rows = package_episode_admission_rows(
        db,
        workspace_id=workspace_id,
        data_package_id=data_package_id,
    )
    decisions = episode_review_decisions(db, data_package_id=data_package_id)
    for episode, fact, eligible, reason in rows:
        if eligible:
            counts["ready"] += 1
        elif fact is None or any(marker in reason for marker in ("pending", "running", "missing")):
            counts["running"] += 1
        else:
            counts["failed"] += 1
        if fact is not None and (episode.id, fact.attempt) in decisions:
            counts["reviewed"] += 1
    return counts


def episode_review_decisions(db: Session, *, data_package_id: int) -> dict[tuple[int, int], str]:
    """Human decisions keyed by Episode/attempt; exclusions are not reviews."""
    decisions: dict[tuple[int, int], str] = {}
    reviews = (
        db.query(PackageIntakeReview)
        .filter(PackageIntakeReview.data_package_id == data_package_id)
        .order_by(PackageIntakeReview.id.asc())
        .all()
    )
    for review in reviews:
        attempts = review.fact_attempts_json or {}
        for decision, ids in (
            (
                "accepted",
                review.accepted_episode_ids_json if review.verdict == "approved" else [],
            ),
            ("rejected", review.rejected_episode_ids_json),
        ):
            for episode_id in ids or []:
                attempt = attempts.get(str(episode_id))
                if attempt is not None:
                    decisions[(int(episode_id), int(attempt))] = decision
    return decisions


def eligible_package_episodes(
    db: Session,
    *,
    workspace_id: int,
    data_package_id: int,
    occupied_episode_ids: Iterable[int] = (),
    for_update: bool = False,
) -> list[tuple[Episode, EpisodeAdmissionFact]]:
    occupied = {int(item) for item in occupied_episode_ids}
    rows = package_episode_admission_rows(
        db,
        workspace_id=workspace_id,
        data_package_id=data_package_id,
        for_update=for_update,
    )
    # A worker fact alone is never enough to enter a batch.  Bind the current
    # attempt to a persisted human ``approved`` review for this exact Episode;
    # repairing an Episode therefore removes it from candidates until the
    # package is reviewed again.
    decisions = episode_review_decisions(db, data_package_id=data_package_id)
    return [
        (episode, fact)
        for episode, fact, eligible, _reason in rows
        if eligible
        and episode.id not in occupied
        and fact is not None
        and decisions.get((episode.id, fact.attempt)) == "accepted"
    ]
