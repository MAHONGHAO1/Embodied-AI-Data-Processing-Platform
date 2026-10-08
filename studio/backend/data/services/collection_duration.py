"""Exact collection duration expressions shared by read-only projections."""

from decimal import Decimal, InvalidOperation

from sqlalchemy import Numeric, String, and_, case, cast, func, select

from data.database import Episode
from data.models.data_package import PackageIntakeReview
from data.models.episode_admission import EpisodeAdmissionFact


def episode_duration_seconds(metadata: object) -> Decimal | None:
    if not isinstance(metadata, dict):
        return None
    timing = metadata.get("timing") or {}
    if isinstance(timing, dict):
        start, end = timing.get("start_timestamp_ns"), timing.get("end_timestamp_ns")
        if all(str(value).isdigit() for value in (start, end)) and int(end) > int(start):
            return Decimal(int(end) - int(start)) / Decimal(1_000_000_000)
    for key in ("timing", "metrics"):
        container = metadata.get(key)
        value = container.get("duration_s") if isinstance(container, dict) else None
        if value is None or isinstance(value, bool):
            continue
        try:
            duration = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if duration.is_finite() and duration >= 0:
            return duration
    return None


def episode_duration_sql():
    """Use nanosecond endpoints when present; never recover seconds from rounded hours."""
    timing = Episode.metadata_json["timing"]
    start, end = timing["start_timestamp_ns"].as_string(), timing["end_timestamp_ns"].as_string()
    start_number = case((start.op("~")(r"^[0-9]+$"), cast(start, Numeric)))
    end_number = case((end.op("~")(r"^[0-9]+$"), cast(end, Numeric)))
    interval = case((end_number > start_number, (end_number - start_number) / 1_000_000_000))
    values = []
    for key in ("timing", "metrics"):
        raw = Episode.metadata_json[key]["duration_s"].as_string()
        values.append(
            case((raw.op("~")(r"^[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?$"), cast(raw, Numeric)))
        )
    return func.coalesce(interval, *values)


def latest_intake_review_ids():
    return (
        select(
            PackageIntakeReview.data_package_id,
            func.max(PackageIntakeReview.id).label("review_id"),
        )
        .group_by(PackageIntakeReview.data_package_id)
        .subquery()
    )


def accepted_intake_episode_sql():
    """Approval must refer to this Episode's frozen admission attempt."""
    attempt = PackageIntakeReview.fact_attempts_json.op("->>")(cast(Episode.id, String))
    return and_(
        PackageIntakeReview.verdict == "approved",
        PackageIntakeReview.accepted_episode_ids_json.op("@>")(func.jsonb_build_array(Episode.id)),
        select(EpisodeAdmissionFact.id)
        .where(
            EpisodeAdmissionFact.episode_id == Episode.id,
            cast(EpisodeAdmissionFact.attempt, String) == attempt,
            EpisodeAdmissionFact.source_fingerprint == Episode.source_fingerprint,
        )
        .correlate(Episode, PackageIntakeReview)
        .exists(),
    )
