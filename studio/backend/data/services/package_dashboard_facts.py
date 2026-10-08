"""Package-level dashboard facts derived once at parse and intake review."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy.orm import Session

from data.database import Episode
from data.models.data_package import DataPackage, PackageIntakeReview
from data.models.episode_admission import EpisodeAdmissionFact
from data.services.collection_duration import episode_duration_seconds
from data.services.episode_visibility import IMPORT_PLACEHOLDER_WORKFLOW_STATUSES

_SECONDS_QUANTUM = Decimal("0.001")
_EPOCH = datetime(1970, 1, 1)
CAPTURE_FACT_COLUMNS = ("captured_started_at", "captured_duration_s", "captured_size_bytes")
REJECTED_INTAKE_FACTS: dict[str, Any] = {
    "intake_valid_duration_s": Decimal("0.000"),
    "intake_valid_size_bytes": 0,
}


def _seconds(value: Decimal) -> Decimal:
    return value.quantize(_SECONDS_QUANTUM, rounding=ROUND_HALF_UP)


def raw_size_bytes(objects: object) -> int:
    if not isinstance(objects, list):
        return 0
    total = 0
    for entry in objects:
        ref = entry.get("ref") if isinstance(entry, dict) else None
        if not isinstance(ref, dict) or ref.get("bucket_role") != "raw":
            continue
        size = ref.get("size_bytes")
        if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
            total += size
    return total


def episode_start_ns(metadata: object) -> int | None:
    timing = metadata.get("timing") if isinstance(metadata, dict) else None
    value = timing.get("start_timestamp_ns") if isinstance(timing, dict) else None
    text = str(value) if value is not None else ""
    return int(text) if text.isdigit() else None


def _source_episodes(db: Session, package_id: int) -> list[Episode]:
    return (
        db.query(Episode)
        .filter(
            Episode.data_package_id == package_id,
            Episode.kind == "source",
            Episode.workflow_status.notin_(IMPORT_PLACEHOLDER_WORKFLOW_STATUSES),
        )
        .order_by(Episode.id.asc())
        .all()
    )


def capture_facts(db: Session, package_id: int) -> dict[str, Any]:
    episodes = _source_episodes(db, package_id)
    if not episodes:
        return dict.fromkeys(CAPTURE_FACT_COLUMNS)
    durations = [episode_duration_seconds(episode.metadata_json) for episode in episodes]
    starts = [
        start
        for start in (episode_start_ns(episode.metadata_json) for episode in episodes)
        if start is not None
    ]
    facts = (
        db.query(EpisodeAdmissionFact)
        .filter(
            EpisodeAdmissionFact.episode_id.in_([episode.id for episode in episodes]),
            EpisodeAdmissionFact.is_current.is_(True),
        )
        .all()
    )
    return {
        "captured_started_at": _EPOCH + timedelta(microseconds=min(starts) // 1000)
        if starts
        else None,
        "captured_duration_s": None
        if any(duration is None for duration in durations)
        else _seconds(sum(durations, Decimal(0))),
        "captured_size_bytes": sum(raw_size_bytes(fact.objects_json) for fact in facts),
    }


def intake_valid_facts(
    accepted: Iterable[tuple[Episode, EpisodeAdmissionFact | None]],
) -> dict[str, Any]:
    duration = Decimal(0)
    size = 0
    for episode, fact in accepted:
        if fact is None:
            continue
        duration += episode_duration_seconds(episode.metadata_json) or Decimal(0)
        size += raw_size_bytes(fact.objects_json)
    return {"intake_valid_duration_s": _seconds(duration), "intake_valid_size_bytes": size}


def apply_facts(package: DataPackage, facts: dict[str, Any]) -> None:
    for column, value in facts.items():
        setattr(package, column, value)


def recompute_package_dashboard_facts(db: Session, package: DataPackage) -> None:
    apply_facts(package, capture_facts(db, package.id))
    review = (
        db.query(PackageIntakeReview)
        .filter(PackageIntakeReview.data_package_id == package.id)
        .order_by(PackageIntakeReview.id.desc())
        .first()
    )
    if review is None:
        apply_facts(package, {"intake_valid_duration_s": None, "intake_valid_size_bytes": None})
        return
    if review.verdict != "approved":
        apply_facts(package, REJECTED_INTAKE_FACTS)
        return
    accepted_ids = {int(value) for value in review.accepted_episode_ids_json or []}
    attempts = review.fact_attempts_json if isinstance(review.fact_attempts_json, dict) else {}
    rows: list[tuple[Episode, EpisodeAdmissionFact | None]] = []
    for episode in _source_episodes(db, package.id):
        if episode.id not in accepted_ids:
            continue
        attempt = attempts.get(str(episode.id))
        fact = (
            db.query(EpisodeAdmissionFact)
            .filter(
                EpisodeAdmissionFact.episode_id == episode.id,
                EpisodeAdmissionFact.attempt == int(attempt),
                EpisodeAdmissionFact.source_fingerprint == episode.source_fingerprint,
            )
            .one_or_none()
            if attempt is not None
            else None
        )
        rows.append((episode, fact))
    apply_facts(package, intake_valid_facts(rows))
