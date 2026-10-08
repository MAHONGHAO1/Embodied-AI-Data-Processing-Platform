"""Bounded-query summaries for package lists; no media signing or per-row reads."""

from collections import defaultdict
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import load_only, selectinload

from data.database import Episode
from data.models.collection_core import CollectionTask, CollectionTaskLabel
from data.models.data_batch import DataBatchPackage
from data.models.data_package import DataPackage, PackageIntakeReview
from data.models.episode_admission import EpisodeAdmissionFact
from data.services.episode_admission import admission_eligibility


def _episode_duration_s(metadata: object) -> Decimal | None:
    """Return a persisted Episode duration without treating missing metadata as zero."""
    if not isinstance(metadata, dict):
        return None
    for container_key in ("timing", "metrics"):
        container = metadata.get(container_key)
        if not isinstance(container, dict) or "duration_s" not in container:
            continue
        raw = container.get("duration_s")
        if raw is None or isinstance(raw, bool):
            continue
        try:
            duration = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if duration.is_finite() and duration >= 0:
            return duration
    return None


def _sum_episode_durations(episodes: list[Episode]) -> Decimal | None:
    """Sum durations only when every selected Episode has a reliable duration."""
    if not episodes:
        return None
    total = Decimal(0)
    for episode in episodes:
        duration = _episode_duration_s(episode.metadata_json)
        if duration is None:
            return None
        total += duration
    return total


def _duration_json_value(duration: Decimal | None) -> float | None:
    return float(duration) if duration is not None else None


def _reviewed_duration_s(
    package_id: int,
    episodes: list[Episode],
    fact_by_id: dict[int, EpisodeAdmissionFact],
    reviews_by_package: dict[int, PackageIntakeReview],
) -> Decimal | None:
    """Project intake duration from a terminal review and its frozen attempts."""
    review = reviews_by_package.get(package_id)
    if review is None:
        return None
    if review.verdict == "rejected":
        return Decimal(0)
    if review.verdict != "approved":
        return None

    accepted_ids = set()
    for episode_id in review.accepted_episode_ids_json or []:
        try:
            accepted_ids.add(int(episode_id))
        except (TypeError, ValueError):
            return None
    episode_by_id = {episode.id: episode for episode in episodes}
    if not accepted_ids.issubset(episode_by_id):
        return None
    attempts = review.fact_attempts_json if isinstance(review.fact_attempts_json, dict) else {}
    accepted: list[Episode] = []
    for episode_id in accepted_ids:
        expected_attempt = attempts.get(str(episode_id), attempts.get(episode_id))
        fact = fact_by_id.get(episode_id)
        try:
            frozen = int(expected_attempt)
            current = int(fact.attempt) if fact is not None else None
        except (TypeError, ValueError):
            return None
        if current != frozen:
            return None
        accepted.append(episode_by_id[episode_id])
    if not accepted:
        return Decimal(0)
    return _sum_episode_durations(accepted)


def package_list_extras(db, packages):
    if not packages:
        return {}
    ids = [package.id for package in packages]
    loaded = (
        db.query(DataPackage)
        .filter(DataPackage.id.in_(ids))
        .options(
            selectinload(DataPackage.project),
            selectinload(DataPackage.task)
            .selectinload(CollectionTask.labels)
            .selectinload(CollectionTaskLabel.label),
            selectinload(DataPackage.operator_collector),
            selectinload(DataPackage.device),
        )
        .all()
    )
    episodes = (
        db.query(Episode)
        .filter(Episode.data_package_id.in_(ids))
        .options(
            load_only(
                Episode.id,
                Episode.data_package_id,
                Episode.source_fingerprint,
                Episode.validity_status,
                Episode.modality,
                Episode.metadata_json,
            )
        )
        .all()
    )
    episode_ids = [episode.id for episode in episodes]
    facts = (
        db.query(EpisodeAdmissionFact)
        .filter(
            EpisodeAdmissionFact.episode_id.in_(episode_ids),
            EpisodeAdmissionFact.is_current.is_(True),
        )
        .options(
            load_only(
                EpisodeAdmissionFact.episode_id,
                EpisodeAdmissionFact.attempt,
                EpisodeAdmissionFact.is_current,
                EpisodeAdmissionFact.source_fingerprint,
                EpisodeAdmissionFact.validation_policy_version,
                EpisodeAdmissionFact.integrity_status,
                EpisodeAdmissionFact.preview_status,
                EpisodeAdmissionFact.output_verification_status,
                EpisodeAdmissionFact.report_ref_json,
            )
        )
        .all()
        if episode_ids
        else []
    )
    fact_by_id = {fact.episode_id: fact for fact in facts}
    reviews = (
        db.query(PackageIntakeReview)
        .filter(PackageIntakeReview.data_package_id.in_(ids))
        .options(
            load_only(
                PackageIntakeReview.id,
                PackageIntakeReview.data_package_id,
                PackageIntakeReview.verdict,
                PackageIntakeReview.accepted_episode_ids_json,
                PackageIntakeReview.rejected_episode_ids_json,
                PackageIntakeReview.fact_attempts_json,
                PackageIntakeReview.reviewed_at,
            )
        )
        .all()
    )
    reviews_by_package = {}
    for review in reviews:
        previous = reviews_by_package.get(review.data_package_id)
        if previous is None or (review.id or 0) > (previous.id or 0):
            reviews_by_package[review.data_package_id] = review
    reviewed = defaultdict(set)
    for review in reviews:
        decided = set(review.rejected_episode_ids_json or [])
        if review.verdict == "approved":
            decided.update(review.accepted_episode_ids_json or [])
        for episode_id in decided:
            attempt = (review.fact_attempts_json or {}).get(str(episode_id))
            if attempt is not None:
                reviewed[review.data_package_id].add((episode_id, attempt))
    batched = {
        package_id
        for (package_id,) in db.query(DataBatchPackage.data_package_id)
        .filter(DataBatchPackage.data_package_id.in_(ids))
        .distinct()
        .all()
    }
    counts = {
        package.id: {"ready": 0, "running": 0, "failed": 0, "reviewed": 0} for package in loaded
    }
    totals = defaultdict(int)
    modalities = defaultdict(set)
    episodes_by_package = defaultdict(list)
    for episode in episodes:
        package_id = episode.data_package_id
        episodes_by_package[package_id].append(episode)
        totals[package_id] += 1
        if episode.modality:
            modalities[package_id].add(episode.modality)
        fact = fact_by_id.get(episode.id)
        eligible, reason = admission_eligibility(episode, fact)
        if eligible:
            counts[package_id]["ready"] += 1
        elif fact is None or any(marker in reason for marker in ("pending", "running", "missing")):
            counts[package_id]["running"] += 1
        else:
            counts[package_id]["failed"] += 1
        if fact is not None and (episode.id, fact.attempt) in reviewed[package_id]:
            counts[package_id]["reviewed"] += 1
    result = {}
    for package in loaded:
        states = (package.qrdf_facts_json or {}).get("source_admission", {}).get("sources", [])
        counts[package.id]["failed"] += sum(
            state.get("status") == "failed" and state.get("episode_id") is None for state in states
        )
        labels = [link.label for link in package.task.labels] if package.task else []
        result[package.id] = {
            "project_name": package.project.name if package.project else None,
            "task_name": package.task.name if package.task else None,
            "collector_name": package.operator_collector.name
            if package.operator_collector
            else None,
            "device_name": package.device.name if package.device else None,
            "episode_count": totals[package.id],
            "modalities": sorted(modalities[package.id]),
            "captured_duration_s": _duration_json_value(
                _sum_episode_durations(episodes_by_package[package.id])
            ),
            "intake_valid_duration_s": _duration_json_value(
                _reviewed_duration_s(
                    package.id,
                    episodes_by_package[package.id],
                    fact_by_id,
                    reviews_by_package,
                )
            ),
            "labels": [
                {"id": label.id, "name": label.name, "category": label.category} for label in labels
            ],
            "admission_counts": counts[package.id],
            "is_batched": package.id in batched,
            "can_batch": package.status == "intake_approved"
            and package.id not in batched
            and counts[package.id]["ready"] > 0,
        }
    return result
