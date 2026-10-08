"""Data batch governance stage execution: skip, QC elimination, compliance recording, and duration writeback."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy.orm import Session, joinedload

from data.database import Episode, EpisodeArtifact
from data.models.data_batch import (
    GOVERNANCE_STAGES,
    DataBatch,
    DataBatchEpisode,
    DataBatchGovernanceRun,
    DataBatchStageRun,
)
from data.models.data_package import DataPackage
from data.security.audit import emit_audit_event

_DURATION_QUANTUM = Decimal("0.01")
_COMPLIANCE_KEYS = (
    "privacy_sensitive",
    "compliance",
    "license",
    "pii",
    "consent",
    "data_classification",
)


def schedule_annotation_assignment(batch_id: int, db: Session | None = None) -> None:
    """Create annotation/review work items after governance → annotating."""
    from data.services.annotation_work_items import assign_work_items_for_batch

    if db is not None:
        assign_work_items_for_batch(db, batch_id=batch_id)
        return

    from data.database import SessionLocal

    session = SessionLocal()
    try:
        assign_work_items_for_batch(session, batch_id=batch_id)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def schedule_asset_creation(
    batch_id: int, db: Session | None = None, *, sync: bool = False
) -> None:
    """Publish at most one data asset for the batch.

    Prefer Celery after commit. Pass ``db`` (in-transaction) or ``sync=True``
    for tests / governance finalize.
    """
    from data.services.data_assets import publish_data_asset

    if db is not None:
        publish_data_asset(db, batch_id=batch_id)
        return

    if sync:
        from data.database import SessionLocal

        session = SessionLocal()
        try:
            publish_data_asset(session, batch_id=batch_id)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        return

    from data.tasks.asset_tasks import execute_asset_publish

    execute_asset_publish.delay(batch_id)


class GovernanceError(ValueError):
    """Raised for invalid governance transitions."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _batch_with_run(db: Session, batch_id: int) -> DataBatch:
    batch = (
        db.query(DataBatch)
        .options(
            joinedload(DataBatch.governance_run).joinedload(DataBatchGovernanceRun.stages),
            joinedload(DataBatch.packages),
        )
        .filter(DataBatch.id == batch_id)
        .one_or_none()
    )
    if batch is None:
        raise LookupError("data batch does not exist")
    return batch


def _package_ids(batch: DataBatch) -> list[int]:
    return [link.data_package_id for link in batch.packages]


def _batch_episode_ids(db: Session, batch_id: int) -> list[int]:
    return [
        row[0]
        for row in db.query(DataBatchEpisode.episode_id)
        .filter(DataBatchEpisode.data_batch_id == batch_id)
        .order_by(DataBatchEpisode.episode_id.asc())
        .all()
    ]


def _valid_episodes(db: Session, episode_ids: list[int]) -> list[Episode]:
    if not episode_ids:
        return []
    return (
        db.query(Episode)
        .filter(
            Episode.id.in_(episode_ids),
            Episode.validity_status == "valid",
        )
        .order_by(Episode.id.asc())
        .all()
    )


def _quantize_hours(value: Decimal) -> Decimal:
    return value.quantize(_DURATION_QUANTUM, rounding=ROUND_HALF_UP)


def _writeback_governed_duration(db: Session, *, batch_id: int, package_ids: list[int]) -> None:
    packages = (
        db.query(DataPackage)
        .filter(DataPackage.id.in_(package_ids))
        .order_by(DataPackage.id.asc())
        .with_for_update()
        .all()
    )
    # The package is a cumulative summary across completed governance runs
    # plus this run. Batch duration remains scoped to its immutable members.
    completed_batch_ids = db.query(DataBatchGovernanceRun.data_batch_id).filter(
        DataBatchGovernanceRun.status == "passed"
    )
    members = (
        db.query(DataBatchEpisode)
        .join(Episode, Episode.id == DataBatchEpisode.episode_id)
        .filter(
            DataBatchEpisode.data_package_id.in_(package_ids),
            Episode.validity_status == "valid",
            (DataBatchEpisode.data_batch_id == batch_id)
            | DataBatchEpisode.data_batch_id.in_(completed_batch_ids),
        )
        .order_by(DataBatchEpisode.episode_id.asc())
        .all()
    )
    for package in packages:
        package.governed_valid_duration_hours = _quantize_hours(
            sum(
                (
                    Decimal(str(member.duration_hours))
                    for member in members
                    if member.data_package_id == package.id
                ),
                Decimal(0),
            )
        )
    batch = db.get(DataBatch, batch_id)
    batch.valid_duration_hours = _quantize_hours(
        sum(
            (
                Decimal(str(member.duration_hours))
                for member in members
                if member.data_batch_id == batch_id
            ),
            Decimal(0),
        )
    )


def _quality_fail_reason(episode: Episode, *, injected: dict[str, Any]) -> str | None:
    drop_ids = {int(item) for item in (injected.get("drop_episode_ids") or []) if item is not None}
    drop_reasons = injected.get("drop_reasons") or {}
    if not isinstance(drop_reasons, dict):
        drop_reasons = {}
    if episode.id in drop_ids:
        reason = drop_reasons.get(str(episode.id)) or drop_reasons.get(episode.id)
        return str(reason or "injected_quality_drop")

    metadata = episode.metadata_json if isinstance(episode.metadata_json, dict) else {}
    quality = metadata.get("quality")
    if isinstance(quality, dict) and (
        quality.get("pass") is False
        or str(quality.get("status") or "").lower()
        in {
            "fail",
            "failed",
            "drop",
            "dropped",
        }
    ):
        return str(quality.get("reason") or "quality_check_failed")
    if metadata.get("qc_fail") is True:
        return str(metadata.get("qc_fail_reason") or "quality_check_failed")
    return None


def _mark_qc_dropped(episode: Episode, *, reason: str) -> None:
    if episode.validity_status == "qc_dropped":
        return
    if episode.validity_status != "valid":
        return
    metadata = dict(episode.metadata_json or {})
    metadata["qc"] = {"reason": reason, "at": _utcnow().isoformat() + "Z"}
    episode.metadata_json = metadata
    episode.validity_status = "qc_dropped"


def _run_quality_stage(db: Session, *, batch: DataBatch, stage: DataBatchStageRun) -> None:
    stage.status = "running"
    db.flush()
    package_ids = _package_ids(batch)
    injected = stage.result_json if isinstance(stage.result_json, dict) else {}
    dropped: list[dict[str, Any]] = []
    for episode in _valid_episodes(db, _batch_episode_ids(db, batch.id)):
        reason = _quality_fail_reason(episode, injected=injected)
        if reason is None:
            continue
        _mark_qc_dropped(episode, reason=reason)
        dropped.append({"episode_id": episode.id, "reason": reason})
    _writeback_governed_duration(db, batch_id=batch.id, package_ids=package_ids)
    result = dict(injected)
    result["dropped"] = dropped
    result["dropped_count"] = len(dropped)
    stage.result_json = result
    stage.error_message = ""
    stage.status = "passed"
    if dropped:
        emit_audit_event(
            "governance.qc.drop",
            actor=None,
            resource=f"data_batch:{batch.id}",
            detail={
                "workspace_id": batch.workspace_id,
                "data_batch_id": batch.id,
                "stage": "quality",
                "dropped": dropped,
                "dropped_count": len(dropped),
            },
        )


def _run_compliance_stage(db: Session, *, batch: DataBatch, stage: DataBatchStageRun) -> None:
    stage.status = "running"
    db.flush()
    package_ids = _package_ids(batch)
    packages = (
        db.query(DataPackage).filter(DataPackage.id.in_(package_ids)).all() if package_ids else []
    )
    packages_out: list[dict[str, Any]] = []
    for package in packages:
        facts = package.qrdf_facts_json if isinstance(package.qrdf_facts_json, dict) else {}
        copied = {key: facts[key] for key in _COMPLIANCE_KEYS if key in facts}
        # Also surface privacy markers nested under common QRDF bags.
        for nested_key in ("capture", "source", "compliance"):
            nested = facts.get(nested_key)
            if isinstance(nested, dict):
                for key in _COMPLIANCE_KEYS:
                    if key in nested and key not in copied:
                        copied[key] = nested[key]
        episode_marks: list[dict[str, Any]] = []
        package_episode_ids = [
            row[0]
            for row in db.query(DataBatchEpisode.episode_id)
            .filter(
                DataBatchEpisode.data_batch_id == batch.id,
                DataBatchEpisode.data_package_id == package.id,
            )
            .order_by(DataBatchEpisode.episode_id.asc())
            .all()
        ]
        for episode in _valid_episodes(db, package_episode_ids):
            metadata = episode.metadata_json if isinstance(episode.metadata_json, dict) else {}
            mark = {key: metadata[key] for key in _COMPLIANCE_KEYS if key in metadata}
            if mark:
                episode_marks.append({"episode_id": episode.id, **mark})
        packages_out.append(
            {
                "data_package_id": package.id,
                "compliance": copied,
                "episodes": episode_marks,
            }
        )
    stage.result_json = {"packages": packages_out}
    stage.error_message = ""
    stage.status = "passed"


def _artifact_count(db: Session, episode_id: int) -> int:
    return db.query(EpisodeArtifact.id).filter(EpisodeArtifact.episode_id == episode_id).count()


def _run_integrity_stage(db: Session, *, batch: DataBatch, stage: DataBatchStageRun) -> None:
    stage.status = "running"
    db.flush()
    issues: list[dict[str, Any]] = []
    require_artifacts = bool(
        (stage.result_json or {}).get("require_artifacts")
        if isinstance(stage.result_json, dict)
        else False
    )
    for episode in _valid_episodes(db, _batch_episode_ids(db, batch.id)):
        metadata = episode.metadata_json if isinstance(episode.metadata_json, dict) else {}
        integrity = metadata.get("integrity")
        if isinstance(integrity, dict) and integrity.get("ok") is False:
            issues.append(
                {
                    "episode_id": episode.id,
                    "reason": str(integrity.get("reason") or "integrity_ok_false"),
                }
            )
            continue
        declared = metadata.get("declared_files")
        if isinstance(declared, list) and declared:
            artifact_n = _artifact_count(db, episode.id)
            if artifact_n < 1:
                issues.append(
                    {
                        "episode_id": episode.id,
                        "reason": "declared_files_missing_artifacts",
                        "declared_count": len(declared),
                        "artifact_count": artifact_n,
                    }
                )
                continue
        elif require_artifacts and _artifact_count(db, episode.id) < 1:
            issues.append(
                {
                    "episode_id": episode.id,
                    "reason": "missing_artifacts",
                }
            )
    result = dict(stage.result_json or {}) if isinstance(stage.result_json, dict) else {}
    result["issues"] = issues
    result["issue_count"] = len(issues)
    stage.result_json = result
    if issues:
        stage.status = "failed"
        stage.error_message = f"integrity check failed for {len(issues)} episode(s)"
    else:
        stage.status = "passed"
        stage.error_message = ""


_STAGE_RUNNERS = {
    "integrity": _run_integrity_stage,
    "quality": _run_quality_stage,
    "compliance": _run_compliance_stage,
}


def _finalize_batch_after_governance(db: Session, batch: DataBatch) -> None:
    package_ids = _package_ids(batch)
    if package_ids:
        _writeback_governed_duration(db, batch_id=batch.id, package_ids=package_ids)
    remaining = len(_valid_episodes(db, _batch_episode_ids(db, batch.id)))
    if remaining == 0:
        batch.status = "no_publishable_asset"
        return
    if batch.annotation_enabled:
        batch.status = "annotating"
        schedule_annotation_assignment(batch.id, db)
        return
    schedule_asset_creation(batch.id, db)


def _lock_governance_execution(
    db: Session, batch_id: int
) -> tuple[DataBatch, DataBatchGovernanceRun | None]:
    """Lock batch → run → stages before mutating (acks_late / double-delivery safe)."""
    batch_row = db.query(DataBatch).filter(DataBatch.id == batch_id).with_for_update().one_or_none()
    if batch_row is None:
        raise LookupError("data batch does not exist")

    run = (
        db.query(DataBatchGovernanceRun)
        .filter(DataBatchGovernanceRun.data_batch_id == batch_id)
        .with_for_update()
        .one_or_none()
    )
    if run is not None:
        (
            db.query(DataBatchStageRun)
            .filter(DataBatchStageRun.run_id == run.id)
            .with_for_update()
            .all()
        )
    return batch_row, run


def _execute_pending_stages(db: Session, *, batch: DataBatch, run: DataBatchGovernanceRun) -> None:
    _lock_governance_execution(db, batch.id)
    run.status = "running"
    if batch.status not in {"failed", "no_publishable_asset", "annotating", "published"}:
        batch.status = "governing"
    db.flush()

    stages_by_name = {stage.stage: stage for stage in run.stages}
    for stage_name in GOVERNANCE_STAGES:
        stage = stages_by_name.get(stage_name)
        if stage is None:
            continue
        if stage.status == "skipped":
            continue
        if stage.status in {"passed"}:
            continue
        if stage.status not in {"queued", "running", "failed"}:
            continue
        # Only execute queued/running on full run; failed is for retry path.
        if stage.status == "failed":
            run.status = "failed"
            batch.status = "failed"
            db.flush()
            return
        runner = _STAGE_RUNNERS[stage_name]
        runner(db, batch=batch, stage=stage)
        db.flush()
        if stage.status == "failed":
            run.status = "failed"
            batch.status = "failed"
            db.flush()
            return

    run.status = "passed"
    db.flush()
    _finalize_batch_after_governance(db, batch)
    db.flush()


def run_governance(db: Session, batch_id: int, *, sync: bool = False) -> DataBatch:
    """Execute or enqueue governance for a data batch.

    ``sync=True`` runs stages inline (tests / worker). ``sync=False`` enqueues
    the Celery task and returns the batch unchanged.
    """
    batch = _batch_with_run(db, batch_id)
    if not sync:
        from data.tasks.governance_tasks import execute_governance_run

        execute_governance_run.delay(batch_id)
        return batch

    _lock_governance_execution(db, batch_id)
    batch = _batch_with_run(db, batch_id)
    run = batch.governance_run
    if run is None:
        _finalize_batch_after_governance(db, batch)
        return batch
    if run.status == "passed":
        return batch

    _execute_pending_stages(db, batch=batch, run=run)
    return batch


def retry_failed_stage(
    db: Session, batch_id: int, stage: str, *, sync: bool = True
) -> DataBatchStageRun:
    """Retry a single failed governance stage without reviving qc_dropped.

    Locks the governance run and target stage row (``FOR UPDATE``) before the
    ``failed`` CAS check so concurrent retries cannot double-run / finalize.
    """
    if stage not in GOVERNANCE_STAGES:
        raise GovernanceError(f"unknown governance stage: {stage}")

    batch_row = db.query(DataBatch).filter(DataBatch.id == batch_id).with_for_update().one_or_none()
    if batch_row is None:
        raise LookupError("data batch does not exist")

    run = (
        db.query(DataBatchGovernanceRun)
        .filter(DataBatchGovernanceRun.data_batch_id == batch_id)
        .with_for_update()
        .one_or_none()
    )
    if run is None:
        raise GovernanceError("data batch has no governance run")

    stage_row = (
        db.query(DataBatchStageRun)
        .filter(
            DataBatchStageRun.run_id == run.id,
            DataBatchStageRun.stage == stage,
        )
        .with_for_update()
        .one_or_none()
    )
    if stage_row is None:
        raise GovernanceError(f"stage not found: {stage}")
    if stage_row.status != "failed":
        raise GovernanceError("only failed stages can be retried")

    stage_row.attempt = int(stage_row.attempt or 1) + 1
    stage_row.status = "queued"
    stage_row.error_message = ""
    # Keep prior result_json injection keys (e.g. drop_episode_ids) but clear
    # previous failure issue lists so retry can pass.
    prior = dict(stage_row.result_json or {}) if isinstance(stage_row.result_json, dict) else {}
    prior.pop("issues", None)
    prior.pop("issue_count", None)
    stage_row.result_json = prior
    run.status = "running"
    batch_row.status = "governing"
    db.flush()

    if not sync:
        from data.tasks.governance_tasks import execute_governance_run

        execute_governance_run.delay(batch_id)
        return stage_row

    # Reload relationships for stage runners / pending continuation.
    batch = _batch_with_run(db, batch_id)
    run = batch.governance_run
    assert run is not None
    stage_row = next(item for item in run.stages if item.stage == stage)

    runner = _STAGE_RUNNERS[stage]
    runner(db, batch=batch, stage=stage_row)
    db.flush()
    if stage_row.status == "failed":
        run.status = "failed"
        batch.status = "failed"
        return stage_row

    # Continue any remaining queued stages, then finalize if all done.
    _execute_pending_stages(db, batch=batch, run=run)
    return stage_row


def get_governance_report(db: Session, *, workspace_id: int, batch_id: int) -> dict[str, Any]:
    batch = (
        db.query(DataBatch)
        .options(joinedload(DataBatch.governance_run).joinedload(DataBatchGovernanceRun.stages))
        .filter(DataBatch.id == batch_id, DataBatch.workspace_id == workspace_id)
        .one_or_none()
    )
    if batch is None:
        raise LookupError("data batch does not exist in this workspace")
    run = batch.governance_run
    if run is None:
        return {
            "data_batch_id": batch.id,
            "status": None,
            "stages": [],
        }
    stages = sorted(run.stages, key=lambda item: GOVERNANCE_STAGES.index(item.stage))
    return {
        "data_batch_id": batch.id,
        "run_id": run.id,
        "status": run.status,
        "stages": [
            {
                "stage": item.stage,
                "status": item.status,
                "attempt": item.attempt,
                "result_json": item.result_json or {},
                "error_message": item.error_message or "",
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
            for item in stages
        ],
    }
