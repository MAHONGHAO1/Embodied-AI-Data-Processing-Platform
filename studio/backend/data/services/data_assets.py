"""Data batch -> single data asset publication."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from data.database import Episode, EpisodeAnnotation
from data.models.annotation_work import (
    AnnotationSubmission,
    AnnotationSubmissionReview,
    AnnotationWorkItem,
    ReviewWorkItem,
)
from data.models.data_asset import DataAsset
from data.models.data_batch import DataBatch, DataBatchEpisode, DataBatchGovernanceRun
from data.models.data_package import DataPackage
from data.models.episode_admission import EpisodeAdmissionFact
from data.security.audit import add_transaction_audit
from data.services.collection_intake_review import _episode_duration_hours
from data.services.resource_names import is_constraint_conflict

_DURATION_QUANTUM = Decimal("0.01")
_ASSET_BATCH_CONSTRAINT = "uq_data_assets_batch"
_STAGE_KEYS = ("integrity", "quality", "compliance", "annotation")


class DataAssetSnapshotError(ValueError):
    """An asset source manifest contains an unsafe or unverifiable ref."""


def publish_data_asset(db: Session, *, batch_id: int) -> DataAsset | None:
    """Publish at most one asset for a batch (idempotent).

    Locks the batch row (``FOR UPDATE``) to avoid duplicate inserts. Returns
    the existing asset when already published; returns ``None`` and sets
    ``no_publishable_asset`` when no ``valid`` episodes remain.
    """
    batch = (
        db.query(DataBatch)
        .filter(DataBatch.id == batch_id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if batch is None:
        raise LookupError("data batch does not exist")

    # Load relationships after the row lock — FOR UPDATE cannot target the
    # nullable side of the outer joins produced by joinedload.
    batch = (
        db.query(DataBatch)
        .options(
            joinedload(DataBatch.packages),
            joinedload(DataBatch.governance_run).joinedload(DataBatchGovernanceRun.stages),
        )
        .filter(DataBatch.id == batch_id)
        .one()
    )

    existing = db.query(DataAsset).filter(DataAsset.data_batch_id == batch.id).one_or_none()
    if existing is not None:
        return existing
    if batch.status == "no_publishable_asset":
        return None

    package_ids = [link.data_package_id for link in batch.packages]
    packages = (
        db.query(DataPackage).filter(DataPackage.id.in_(package_ids)).with_for_update().all()
        if package_ids
        else []
    )
    packages_by_id = {package.id: package for package in packages}

    members = (
        db.query(DataBatchEpisode)
        .filter(DataBatchEpisode.data_batch_id == batch.id)
        .order_by(DataBatchEpisode.episode_id.asc())
        .with_for_update()
        .all()
    )
    fixed_rows = _fixed_episode_rows(db, members)
    approved_entries = (
        _approved_submission_entries(db, batch=batch) if batch.annotation_enabled else None
    )
    no_valid_entries = []
    if approved_entries is not None:
        for episode, member, _fact in fixed_rows:
            entry = approved_entries.get(episode.id)
            if entry is None or entry["admission_attempt"] != member.admission_attempt:
                raise DataAssetSnapshotError("approved_annotation_membership_mismatch")
        no_valid_entries = [
            entry
            for entry in approved_entries.values()
            if entry["conclusion"] == "no_valid_segments"
        ]
        fixed_rows = [
            row for row in fixed_rows if approved_entries[row[0].id]["conclusion"] == "segments"
        ]
        batch.assignment_snapshot_json = {
            **(batch.assignment_snapshot_json or {}),
            "annotation_outcome_summary": {
                "effective_duration_ns": str(
                    sum(int(entry["effective_duration_ns"]) for entry in approved_entries.values())
                ),
                "no_valid_episode_ids": [entry["episode_id"] for entry in no_valid_entries],
                "submission_ids": sorted(
                    {entry["submission_id"] for entry in approved_entries.values()}
                ),
            },
        }
    valid_episodes = [episode for episode, _member, _fact in fixed_rows]
    if not valid_episodes:
        batch.status = "no_publishable_asset"
        db.flush()
        return None

    duration = (
        _quantize_hours(
            sum(
                (
                    Decimal(approved_entries[episode.id]["effective_duration_ns"])
                    / Decimal(3_600_000_000_000)
                    for episode in valid_episodes
                ),
                Decimal(0),
            )
        )
        if approved_entries is not None
        else _asset_duration_hours(valid_episodes, packages, members)
    )
    asset = DataAsset(
        data_batch_id=batch.id,
        workspace_id=batch.workspace_id,
        governed_valid_duration_hours=duration,
        stage_snapshot_json=_stage_snapshot(batch),
        episode_ids_json=[episode.id for episode in valid_episodes],
        source_json=_source_json(
            packages_by_id,
            package_ids,
            valid_episodes=valid_episodes,
        ),
    )
    source_snapshot = _source_snapshot(
        db,
        batch=batch,
        rows=fixed_rows,
        governance_run=batch.governance_run,
        approved_entries=approved_entries,
    )
    asset.source_snapshot_json = source_snapshot
    asset.source_snapshot_id = _snapshot_id(source_snapshot)
    try:
        with db.begin_nested():
            db.add(asset)
            db.flush()
    except IntegrityError as exc:
        if is_constraint_conflict(exc, _ASSET_BATCH_CONSTRAINT):
            winner = db.query(DataAsset).filter(DataAsset.data_batch_id == batch.id).one()
            return winner
        raise

    for package in packages:
        package.status = "published"
    batch.status = "published"
    db.flush()
    db.refresh(asset)
    add_transaction_audit(
        db,
        "asset.publish",
        actor_id=None,
        workspace_id=asset.workspace_id,
        resource_type="data_asset",
        resource_id=asset.id,
        detail={"data_batch_id": batch.id, "episode_count": len(asset.episode_ids_json or [])},
    )
    return asset


def _asset_list_query(db: Session, *, source_workspace_id: int | None = None):
    query = db.query(DataAsset)
    if source_workspace_id is not None:
        query = query.filter(DataAsset.workspace_id == source_workspace_id)
    return query.order_by(DataAsset.created_at.desc(), DataAsset.id.desc())


def list_data_assets(db: Session, *, source_workspace_id: int | None = None) -> list[DataAsset]:
    return _asset_list_query(db, source_workspace_id=source_workspace_id).all()


def list_data_assets_page(
    db: Session, *, source_workspace_id: int | None, limit: int | None, offset: int | None
) -> dict:
    from data.services.list_pagination import paginate_list

    return paginate_list(
        _asset_list_query(db, source_workspace_id=source_workspace_id), limit=limit, offset=offset
    )


def get_data_asset(db: Session, *, asset_id: int) -> DataAsset:
    asset = db.query(DataAsset).filter(DataAsset.id == asset_id).one_or_none()
    if asset is None:
        raise LookupError("data asset does not exist")
    return asset


def _valid_episodes(db: Session, batch_id: int) -> list[Episode]:
    return (
        db.query(Episode)
        .join(
            DataBatchEpisode,
            DataBatchEpisode.episode_id == Episode.id,
        )
        .filter(
            DataBatchEpisode.data_batch_id == batch_id,
            Episode.validity_status == "valid",
        )
        .order_by(Episode.id.asc())
        .all()
    )


def _fixed_episode_rows(
    db: Session, members: list[DataBatchEpisode]
) -> list[tuple[Episode, DataBatchEpisode, EpisodeAdmissionFact]]:
    """Resolve each member's persisted admission attempt, never ``current``."""
    if not members:
        return []
    episode_ids = [member.episode_id for member in members]
    episodes = {
        episode.id: episode
        for episode in db.query(Episode).filter(Episode.id.in_(episode_ids)).all()
    }
    facts = {
        (fact.episode_id, fact.attempt): fact
        for fact in db.query(EpisodeAdmissionFact)
        .filter(EpisodeAdmissionFact.episode_id.in_(episode_ids))
        .all()
    }
    rows: list[tuple[Episode, DataBatchEpisode, EpisodeAdmissionFact]] = []
    for member in members:
        episode = episodes.get(member.episode_id)
        fact = facts.get((member.episode_id, member.admission_attempt))
        if episode is None or fact is None:
            continue
        if (
            episode.validity_status != "valid"
            or not episode.source_fingerprint
            or fact.source_fingerprint != episode.source_fingerprint
            or fact.integrity_status != "passed"
            or fact.preview_status not in {"ready", "not_applicable"}
            or fact.output_verification_status != "verified"
            or not isinstance(fact.report_ref_json, dict)
            or not fact.report_ref_json
        ):
            continue
        rows.append((episode, member, fact))
    return rows


def _approved_submission_entries(db: Session, *, batch: DataBatch) -> dict[int, dict]:
    """Fail closed for historical/mismatched evidence; never infer a latest revision."""
    result = {}
    items = db.query(AnnotationWorkItem).filter_by(data_batch_id=batch.id).populate_existing().all()
    for item in items:
        submission = (
            db.get(AnnotationSubmission, item.current_submission_id)
            if item.current_submission_id
            else None
        )
        review = (
            db.query(ReviewWorkItem)
            .filter_by(annotation_work_item_id=item.id)
            .populate_existing()
            .one_or_none()
        )
        decision = (
            db.query(AnnotationSubmissionReview)
            .filter_by(submission_id=submission.id)
            .one_or_none()
            if submission
            else None
        )
        if (
            item.status != "done"
            or submission is None
            or review is None
            or review.status != "approved"
            or review.submission_id != submission.id
            or decision is None
            or decision.decision != "approved"
            or submission.annotation_work_item_id != item.id
            or submission.workspace_id != batch.workspace_id
        ):
            raise DataAssetSnapshotError("approved_annotation_submission_unavailable")
        expected = {
            (int(member["episode_id"]), int(member["admission_attempt"]))
            for member in item.episode_members_json or []
        }
        entries = submission.episodes_json or []
        if expected != {(entry["episode_id"], entry["admission_attempt"]) for entry in entries}:
            raise DataAssetSnapshotError("approved_annotation_membership_mismatch")
        for entry in entries:
            if entry["episode_id"] in result:
                raise DataAssetSnapshotError("approved_annotation_membership_mismatch")
            frozen = deepcopy(entry)
            frozen["submission_id"] = submission.id
            frozen["annotation_work_item_id"] = item.id
            result[entry["episode_id"]] = frozen
    return result


def _source_snapshot(
    db: Session,
    *,
    batch: DataBatch,
    rows: list[tuple[Episode, DataBatchEpisode, EpisodeAdmissionFact]],
    governance_run: DataBatchGovernanceRun | None,
    approved_entries: dict[int, dict] | None = None,
) -> dict[str, Any]:
    episodes: list[dict[str, Any]] = []
    for episode, member, fact in rows:
        from data.services.episode_objects import (
            EpisodeObjectsError,
            fact_objects,
            object_of_kind,
            require_verified_objects,
        )

        try:
            episode_objects = fact_objects(fact)
            require_verified_objects(episode_objects)
        except EpisodeObjectsError as exc:
            raise DataAssetSnapshotError(exc.code) from exc
        files = [
            {"path": item.path, "kind": item.kind, "ref": dict(item.ref)}
            for item in episode_objects
        ]
        report_object = object_of_kind(episode_objects, "admission_report")
        approved = approved_entries.get(episode.id) if approved_entries is not None else None
        annotation_revision: dict[str, Any] = {}
        if approved is not None:
            annotation = db.get(EpisodeAnnotation, approved["annotation_revision_id"])
            if (
                annotation is None
                or annotation.episode_id != episode.id
                or annotation.version != approved["annotation_version"]
                or annotation.payload_json != approved["annotation_payload"]
                or approved["source_fingerprint"] != fact.source_fingerprint
            ):
                raise DataAssetSnapshotError("approved_annotation_revision_unavailable")
            annotation_revision = {
                "id": annotation.id,
                "version": annotation.version,
                "payload": deepcopy(approved["annotation_payload"]),
                "process_refs": [],
            }
        elif not batch.annotation_enabled:
            # Unannotated batches continue to publish their governed members.
            # Unrelated legacy annotation revisions are not an approval record.
            annotation_revision = {}
        episodes.append(
            {
                "episode_id": episode.id,
                "admission_attempt": member.admission_attempt,
                "source_fingerprint": episode.source_fingerprint,
                "files": files,
                "annotation_revision": annotation_revision,
                **(
                    {
                        "annotation_submission_id": approved["submission_id"],
                        "annotation_work_item_id": approved["annotation_work_item_id"],
                        "annotation_conclusion": approved["conclusion"],
                        "effective_segments": deepcopy(approved["segments"]),
                        "effective_duration_ns": approved["effective_duration_ns"],
                        "source_start_ns": approved["start_ns"],
                        "source_end_ns": approved["end_ns"],
                        "qrdf_episode_id": approved["qrdf_episode_id"],
                    }
                    if approved
                    else {}
                ),
                "governance_report_refs": [
                    {
                        "object_ref": dict(report_object.ref),
                        "path": report_object.path,
                        "admission_attempt": member.admission_attempt,
                    }
                ],
                "admission_report_inline": deepcopy(fact.report_ref_json),
                "valid_duration_hours": (
                    format(
                        Decimal(approved["effective_duration_ns"]) / Decimal(3_600_000_000_000), "f"
                    )
                    if approved
                    else f"{Decimal(str(member.duration_hours)):.2f}"
                ),
            }
        )
    governance = {}
    if governance_run is not None:
        governance = {
            "run_id": governance_run.id,
            "status": governance_run.status,
            "stages": [
                {
                    "id": stage.id,
                    "stage": stage.stage,
                    "attempt": stage.attempt,
                    "status": stage.status,
                    "result": deepcopy(stage.result_json or {}),
                }
                for stage in sorted(governance_run.stages, key=lambda item: item.stage)
            ],
        }
    return {
        "schema": "quicstudio.asset-source.v1",
        "data_batch_id": batch.id,
        "workspace_id": batch.workspace_id,
        "episodes": episodes,
        "governance": governance,
        "annotation_enabled": bool(batch.annotation_enabled),
        "no_valid_episode_conclusions": [
            {
                key: deepcopy(entry[key])
                for key in (
                    "episode_id",
                    "admission_attempt",
                    "source_fingerprint",
                    "submission_id",
                    "conclusion",
                    "reason",
                )
            }
            for entry in (approved_entries or {}).values()
            if entry["conclusion"] == "no_valid_segments"
        ],
    }


def _snapshot_id(snapshot: dict[str, Any]) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _asset_duration_hours(
    episodes: list[Episode],
    packages: list[DataPackage],
    members: list[DataBatchEpisode],
) -> Decimal:
    del packages
    durations = {member.episode_id: Decimal(str(member.duration_hours)) for member in members}
    return _quantize_hours(
        sum(
            (durations.get(episode.id, _episode_duration_hours(episode)) for episode in episodes),
            Decimal(0),
        )
    )


def _quantize_hours(value: Decimal) -> Decimal:
    return value.quantize(_DURATION_QUANTUM, rounding=ROUND_HALF_UP)


def _stage_snapshot(batch: DataBatch) -> dict[str, str]:
    stage_status: dict[str, str] = {}
    run = batch.governance_run
    by_stage: dict[str, str] = {}
    if run is not None:
        for stage in run.stages:
            by_stage[stage.stage] = str(stage.status)

    enabled = {
        "integrity": bool(batch.integrity_check_enabled),
        "quality": bool(batch.quality_check_enabled),
        "compliance": bool(batch.compliance_check_enabled),
    }
    for key in ("integrity", "quality", "compliance"):
        if not enabled[key]:
            stage_status[key] = "skipped"
            continue
        raw = by_stage.get(key)
        if raw == "passed":
            stage_status[key] = "passed"
        elif raw == "skipped":
            stage_status[key] = "skipped"
        elif raw == "failed":
            stage_status[key] = "eliminated"
        else:
            stage_status[key] = "not_generated"

    if batch.annotation_enabled:
        stage_status["annotation"] = "passed"
    else:
        stage_status["annotation"] = "skipped"
    # Ensure stable key set.
    for key in _STAGE_KEYS:
        stage_status.setdefault(key, "not_generated")
    return stage_status


def _source_json(
    packages_by_id: dict[int, DataPackage],
    package_ids: list[int],
    *,
    valid_episodes: list[Episode],
) -> dict[str, Any]:
    project_ids: list[int] = []
    task_ids: list[int] = []
    for package_id in package_ids:
        package = packages_by_id.get(package_id)
        if package is None:
            continue
        if package.collection_project_id not in project_ids:
            project_ids.append(package.collection_project_id)
        if package.collection_task_id not in task_ids:
            task_ids.append(package.collection_task_id)
    package_snapshots = [
        {
            "data_package_id": package.id,
            "package_uid": package.package_uid,
            "collection_project_id": package.collection_project_id,
            "collection_task_id": package.collection_task_id,
            "qrdf_facts": package.qrdf_facts_json or {},
        }
        for package in (packages_by_id.get(package_id) for package_id in package_ids)
        if package is not None
    ]
    episode_snapshots = [
        {
            "episode_id": episode.id,
            "episode_uid": episode.episode_uid,
            "source_fingerprint": episode.source_fingerprint,
            "metadata_sha256": hashlib.sha256(
                json.dumps(
                    episode.metadata_json or {},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        }
        for episode in valid_episodes
    ]
    return {
        "data_package_ids": list(package_ids),
        "collection_project_ids": project_ids,
        "collection_task_ids": task_ids,
        "package_snapshots": package_snapshots,
        "episode_snapshots": episode_snapshots,
    }
