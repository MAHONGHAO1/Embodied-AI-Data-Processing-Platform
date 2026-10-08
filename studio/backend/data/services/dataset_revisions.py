"""Immutable workspace-scoped DatasetRevision and PublishedSample manifests."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    String,
    case,
    func,
    literal,
    or_,
    select,
    union_all,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from data.database import (
    Batch,
    Dataset,
    DatasetItem,
    DatasetRevision,
    Episode,
    EpisodeArtifact,
    JobRun,
    NativeLerobotDataset,
    PublishedEpisode,
)
from data.services.job_runs import create_or_get_job
from data.services.published_sample_manifest import calculate_effective_range
from data.services.resource_names import (
    DATASET_NAME_CONFLICT,
    is_constraint_conflict,
    normalized_key,
    normalized_name,
)
from data.services.workflow_conflict import WorkflowConflict
from data.services.workspace_access import require_workspace_actor
from data.utils.formatting import format_api_datetime

DATASET_EXPORT_JOB_KIND = "dataset_export"
DATASET_EXPORT_QUEUE = "export"
_ALLOWED_SPLITS = frozenset({"train", "val", "test"})
_ALLOWED_EXPORT_PROFILES = frozenset({"lerobot", "qrdf"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_DATASET_REVISION_ITEMS = 1000


class IdempotencyKeyReused(WorkflowConflict):
    """A client request id was replayed with a different authorized payload."""


class DatasetRevisionCandidatesChanged(WorkflowConflict):
    def __init__(self, invalid_items: list[dict[str, object]]) -> None:
        super().__init__("dataset revision candidates changed")
        self.invalid_items = invalid_items

    def as_detail(self) -> dict[str, object]:
        return {
            "code": "dataset_revision_candidates_changed",
            "invalid_items": self.invalid_items,
        }


class DatasetNameConflict(WorkflowConflict):
    def __init__(self) -> None:
        super().__init__(DATASET_NAME_CONFLICT.message)

    def as_detail(self) -> dict[str, str]:
        return {"code": DATASET_NAME_CONFLICT.code, "message": DATASET_NAME_CONFLICT.message}


@dataclass(frozen=True)
class DatasetItemInput:
    episode_id: int
    split: str = "train"
    sample_id: str | None = None
    annotation_revision_id: int | None = None
    annotation_segment_id: str | None = None
    core_start_ns: int | None = None
    core_end_ns: int | None = None
    effective_start_ns: int | None = None
    effective_end_ns: int | None = None
    pre_roll_s: float = 0.0
    post_roll_s: float = 0.0
    task: str = ""
    outcome: str = ""


@dataclass(frozen=True)
class DatasetExportEnqueueResult:
    """The current export JobRun and whether this request created it."""

    job: JobRun
    created: bool


def create_dataset_container(
    db: Session,
    *,
    workspace_id: int,
    name: str,
    description: str = "",
    actor_id: int,
) -> Dataset:
    """Create an empty, workspace-scoped Dataset container.

    A container is deliberately separate from a revision: callers must make a
    second explicit request to freeze a set of published Episodes.
    """
    workspace_id = _positive_int(workspace_id, "workspace")
    normalized_name = _normalized_dataset_name(name)
    _require_dataset_writer(db, actor_id=actor_id, workspace_id=workspace_id)
    if (
        _locked_dataset_query(db, workspace_id=workspace_id, name=normalized_name).one_or_none()
        is not None
    ):
        raise DatasetNameConflict()
    try:
        with db.begin_nested():
            dataset = Dataset(
                workspace_id=workspace_id,
                created_by_user_id=actor_id,
                name=normalized_name,
                description=str(description or ""),
            )
            db.add(dataset)
            db.flush()
    except IntegrityError as exc:
        # The unique constraint is the final authority when concurrent clients
        # create the same name after the optimistic lookup above.
        db.expire_all()
        if is_constraint_conflict(exc, "uq_datasets_workspace_normalized_name"):
            raise DatasetNameConflict() from exc
        raise
    db.commit()
    return get_dataset(db, dataset.id)


def create_dataset_revision(
    db: Session,
    *,
    workspace_id: int,
    name: str,
    items: list[DatasetItemInput | Mapping[str, object]],
    actor_id: int,
    filter_json: dict[str, object] | None = None,
    description: str = "",
    request_id: str | None = None,
) -> DatasetRevision:
    """Create a revision whose item ranges and source facts are immutable."""
    workspace_id = _positive_int(workspace_id, "workspace")
    normalized_name = _normalized_dataset_name(name)
    actor = _require_dataset_writer(db, actor_id=actor_id, workspace_id=workspace_id)

    dataset = _logical_dataset(
        db,
        workspace_id=workspace_id,
        name=normalized_name,
        description=description,
        actor_id=actor.id,
    )
    return _create_dataset_revision_for_dataset(
        db,
        dataset=dataset,
        actor_id=actor.id,
        items=items,
        filter_json=filter_json,
        request_id=request_id,
    )


def create_dataset_revision_for_dataset(
    db: Session,
    *,
    dataset_id: int,
    items: list[DatasetItemInput | Mapping[str, object]],
    actor_id: int,
    filter_json: dict[str, object] | None = None,
    request_id: str | None = None,
) -> DatasetRevision:
    """Freeze a new revision beneath an existing Dataset container."""
    # Serializes version allocation for an existing Dataset.  The unique
    # constraint remains the final invariant, while this avoids surfacing an
    # otherwise routine concurrent revision request as an IntegrityError.
    dataset = _locked_dataset(db, dataset_id)
    actor = _require_dataset_writer(db, actor_id=actor_id, workspace_id=dataset.workspace_id)
    return _create_dataset_revision_for_dataset(
        db,
        dataset=dataset,
        actor_id=actor.id,
        items=items,
        filter_json=filter_json,
        request_id=request_id,
    )


def _create_dataset_revision_for_dataset(
    db: Session,
    *,
    dataset: Dataset,
    actor_id: int,
    items: list[DatasetItemInput | Mapping[str, object]],
    filter_json: dict[str, object] | None,
    request_id: str | None,
) -> DatasetRevision:
    """Build, validate, and atomically persist a revision for an authorized container."""
    workspace_id = dataset.workspace_id

    item_inputs = [_coerce_item(item) for item in items]
    if not item_inputs:
        raise WorkflowConflict("dataset revision requires published episodes")
    if len(item_inputs) > MAX_DATASET_REVISION_ITEMS:
        raise WorkflowConflict("dataset revision exceeds the item limit")
    episode_ids = [item.episode_id for item in item_inputs]
    canonical_filter = _canonical_json(filter_json or {})
    normalized_request_id = _request_id(request_id)
    request_fingerprint = _revision_request_fingerprint(
        dataset_id=dataset.id,
        actor_id=actor_id,
        filter_json=canonical_filter,
        items=item_inputs,
    )
    if normalized_request_id is not None:
        existing = (
            db.query(DatasetRevision)
            .filter(
                DatasetRevision.dataset_id == dataset.id,
                DatasetRevision.client_request_id == normalized_request_id,
            )
            .one_or_none()
        )
        if existing is not None:
            if existing.request_fingerprint != request_fingerprint:
                raise IdempotencyKeyReused("dataset revision request id was reused")
            return _get_revision(db, existing.id)
    published_episodes = _published_workspace_episodes(
        db,
        workspace_id=workspace_id,
        episode_ids=episode_ids,
    )
    specs = [
        _build_item_spec(db, published_episodes[item.episode_id], item) for item in item_inputs
    ]
    _validate_revision_specs(specs)

    revision = _new_revision_with_allocated_version(
        db,
        dataset=dataset,
        workspace_id=workspace_id,
        actor_id=actor_id,
        filter_json=canonical_filter,
        episode_count=len(specs),
        client_request_id=normalized_request_id,
        request_fingerprint=request_fingerprint if normalized_request_id is not None else None,
    )
    for position, spec in enumerate(specs):
        db.add(
            DatasetItem(
                revision_id=revision.id,
                published_episode_id=int(spec["published_episode_id"]),
                position=position,
                split=str(spec["split"]),
                source_snapshot_hash=str(spec["source_snapshot_hash"]),
                item_kind=str(spec["item_kind"]),
                sample_id=str(spec["sample_id"]),
                annotation_revision_id=spec["annotation_revision_id"],
                annotation_segment_id=spec["annotation_segment_id"],
                source_episode_id=str(spec["source_episode_id"]),
                source_fingerprint=str(spec["source_fingerprint"]),
                core_start_ns=spec["core_start_ns"],
                core_end_ns=spec["core_end_ns"],
                effective_start_ns=spec["effective_start_ns"],
                effective_end_ns=spec["effective_end_ns"],
                pre_roll_s=float(spec["pre_roll_s"]),
                post_roll_s=float(spec["post_roll_s"]),
                task_text=str(spec["task"]),
                outcome=str(spec["outcome"]),
                modality_signature=str(spec["modality_signature"]),
            )
        )
    db.flush()
    revision.manifest_hash = _manifest_hash(build_revision_manifest(db, revision.id))
    db.commit()
    return _get_revision(db, revision.id)


def build_revision_manifest(db: Session, revision_id: int) -> dict[str, object]:
    """Return the canonical, path-free export input for one revision."""
    revision = _get_revision(db, revision_id)
    dataset = db.get(Dataset, revision.dataset_id)
    if dataset is None or dataset.workspace_id != revision.workspace_id:
        raise WorkflowConflict("dataset revision scope is unavailable")
    rows: list[dict[str, object]] = []
    for item in revision.items:
        published = db.get(PublishedEpisode, item.published_episode_id)
        if published is None:
            raise WorkflowConflict("dataset revision published episode is unavailable")
        episode = db.get(Episode, published.episode_id)
        artifact = db.get(EpisodeArtifact, published.official_artifact_id)
        if (
            episode is None
            or artifact is None
            or episode.workspace_id != revision.workspace_id
            or artifact.storage_role != "official"
            or artifact.artifact_type != "official_qrdf"
            or not artifact.storage_uri
            or not _SHA256.fullmatch(str(artifact.checksum_sha256 or "").lower())
        ):
            raise WorkflowConflict("dataset revision official artifact is unavailable")
        rows.append(
            {
                "position": int(item.position),
                "split": item.split,
                "item_kind": item.item_kind,
                "sample_id": item.sample_id or f"episode:{published.id}",
                "published_episode_id": int(published.id),
                "episode_id": int(episode.id),
                "source_episode_id": item.source_episode_id or episode.episode_uid,
                "source_fingerprint": item.source_fingerprint or artifact.checksum_sha256,
                "package_uri": artifact.storage_uri,
                "package_checksum": artifact.checksum_sha256,
                "annotation_revision_id": item.annotation_revision_id,
                "annotation_segment_id": item.annotation_segment_id,
                "core_start_ns": item.core_start_ns,
                "core_end_ns": item.core_end_ns,
                "effective_start_ns": item.effective_start_ns,
                "effective_end_ns": item.effective_end_ns,
                "pre_roll_s": float(item.pre_roll_s or 0),
                "post_roll_s": float(item.post_roll_s or 0),
                "task": item.task_text,
                "outcome": item.outcome,
                "modality": episode.modality,
                "modality_signature": item.modality_signature,
                "provenance_snapshot_hash": item.source_snapshot_hash,
            }
        )
    return {
        "revision_id": int(revision.id),
        "dataset_id": int(dataset.id),
        "workspace_id": int(revision.workspace_id),
        "name": dataset.name,
        "version": int(revision.version),
        "filter_json": _canonical_json(revision.filter_json or {}),
        "items": rows,
    }


def enqueue_dataset_export(
    db: Session,
    revision_id: int,
    *,
    actor_id: int,
    export_profile: str = "lerobot",
) -> JobRun:
    """Return the current immutable export attempt for compatibility callers."""
    return enqueue_dataset_export_attempt(
        db,
        revision_id,
        actor_id=actor_id,
        export_profile=export_profile,
    ).job


def enqueue_dataset_export_attempt(
    db: Session,
    revision_id: int,
    *,
    actor_id: int,
    export_profile: str = "lerobot",
) -> DatasetExportEnqueueResult:
    """Create at most one initial export attempt for an immutable revision.

    A retry remains a separate, explicitly audited operation. Repeated initial
    requests return the latest existing JobRun so a browser retry cannot encode
    the same revision more than once.
    """
    revision = _locked_revision(db, revision_id)
    actor = require_workspace_actor(db, actor_id=actor_id, workspace_id=revision.workspace_id)
    if actor.role != "admin":
        raise PermissionError("only an operator or admin can export dataset revisions")
    profile = str(export_profile or "").strip()
    if profile not in _ALLOWED_EXPORT_PROFILES:
        raise WorkflowConflict("unsupported dataset export profile")
    if revision.retired_at is not None:
        raise WorkflowConflict("dataset revision is retired")
    idempotency_key = f"dataset-export:{revision.id}:{profile}:{revision.manifest_hash}"
    existing = (
        db.query(JobRun)
        .filter(
            JobRun.idempotency_key == idempotency_key,
        )
        .order_by(JobRun.created_at.desc(), JobRun.id.desc())
        .with_for_update()
        .first()
    )
    if existing is not None:
        if existing.workspace_id != revision.workspace_id:
            raise WorkflowConflict("dataset export scope is unavailable")
        db.commit()
        db.refresh(existing)
        return DatasetExportEnqueueResult(job=existing, created=False)
    manifest = build_revision_manifest(db, revision.id)
    if _manifest_hash(manifest) != revision.manifest_hash:
        raise WorkflowConflict("dataset revision manifest integrity check failed")
    job = create_or_get_job(
        db,
        kind=DATASET_EXPORT_JOB_KIND,
        resource_type="dataset_revision",
        resource_id=revision.id,
        idempotency_key=idempotency_key,
        queue=DATASET_EXPORT_QUEUE,
        actor_id=actor.id,
        workspace_id=revision.workspace_id,
        detail={
            "phase": "queued",
            "export_profile": profile,
            "manifest_hash": revision.manifest_hash,
            "manifest": manifest,
        },
    )
    db.commit()
    return DatasetExportEnqueueResult(job=job, created=True)


def list_dataset_revisions(db: Session, *, workspace_id: int) -> list[DatasetRevision]:
    return list(
        db.query(DatasetRevision)
        .options(joinedload(DatasetRevision.items))
        .filter(DatasetRevision.workspace_id == workspace_id)
        .order_by(DatasetRevision.created_at.desc(), DatasetRevision.id.desc())
        .all()
    )


def get_dataset(db: Session, dataset_id: int) -> Dataset:
    dataset = db.get(Dataset, _positive_int(dataset_id, "dataset"))
    if dataset is None:
        raise WorkflowConflict("dataset does not exist")
    return dataset


def _locked_dataset(db: Session, dataset_id: int) -> Dataset:
    dataset = (
        db.query(Dataset)
        .filter(Dataset.id == _positive_int(dataset_id, "dataset"))
        .with_for_update()
        .one_or_none()
    )
    if dataset is None:
        raise WorkflowConflict("dataset does not exist")
    return dataset


def list_dataset_summaries(
    db: Session,
    *,
    workspace_id: int,
    limit: int,
    offset: int,
    keyword: str | None = None,
    created_by_user_id: int | None = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
) -> tuple[list[dict[str, object]], int]:
    if not 1 <= limit <= 100 or offset < 0:
        raise WorkflowConflict("dataset pagination is invalid")
    if sort_by not in {"name", "created_at", "revision_count", "latest_revision_at"}:
        raise WorkflowConflict("dataset sort is invalid")
    if sort_order not in {"asc", "desc"}:
        raise WorkflowConflict("dataset sort is invalid")
    workspace_id = _positive_int(workspace_id, "workspace")
    base = db.query(Dataset).filter(Dataset.workspace_id == workspace_id)
    normalized_keyword = str(keyword or "").strip()
    if normalized_keyword:
        base = base.filter(Dataset.name.ilike(f"%{_escape_like(normalized_keyword)}%", escape="\\"))
    if created_by_user_id is not None:
        base = base.filter(Dataset.created_by_user_id == _positive_int(created_by_user_id, "user"))
    total = int(base.with_entities(func.count(Dataset.id)).scalar() or 0)
    revision_count = func.count(DatasetRevision.id)
    latest_version = func.max(DatasetRevision.version)
    latest_revision_at = func.max(DatasetRevision.created_at)
    query = (
        base.with_entities(
            Dataset,
            revision_count.label("revision_count"),
            latest_version.label("latest_version"),
            latest_revision_at.label("latest_revision_at"),
        )
        .outerjoin(DatasetRevision, DatasetRevision.dataset_id == Dataset.id)
        .group_by(Dataset.id)
    )
    sort_fields = {
        "name": func.lower(Dataset.name),
        "created_at": Dataset.created_at,
        "revision_count": revision_count,
        "latest_revision_at": latest_revision_at,
    }
    direction = "asc" if sort_order == "asc" else "desc"
    primary = getattr(sort_fields[sort_by], direction)().nullslast()
    stable = getattr(Dataset.id, direction)()
    rows = query.order_by(primary, stable).offset(offset).limit(limit).all()
    return [
        _dataset_summary(
            dataset,
            revision_count=int(row_revision_count or 0),
            latest_version=row_latest_version,
            latest_revision_at=row_latest_revision_at,
        )
        for dataset, row_revision_count, row_latest_version, row_latest_revision_at in rows
    ], total


def list_dataset_catalog_entries(
    db: Session,
    *,
    workspace_id: int,
    limit: int,
    offset: int,
    keyword: str | None = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
) -> tuple[list[dict[str, object]], int]:
    """Return a paginated, safe projection of QRDF and native LeRobot records.

    The union deliberately selects no native ``oss_uri``.  URI delivery remains
    the separately authorized, audited native dataset endpoint.
    """
    if not 1 <= limit <= 100 or offset < 0:
        raise WorkflowConflict("dataset pagination is invalid")
    if sort_by not in {"name", "created_at", "revision_count", "latest_revision_at"}:
        raise WorkflowConflict("dataset sort is invalid")
    if sort_order not in {"asc", "desc"}:
        raise WorkflowConflict("dataset sort is invalid")
    workspace_id = _positive_int(workspace_id, "workspace")
    normalized_keyword = str(keyword or "").strip()

    revision_count = func.count(DatasetRevision.id)
    latest_version = func.max(DatasetRevision.version)
    latest_revision_at = func.max(DatasetRevision.created_at)
    qrdf_query = (
        select(
            literal("qrdf", type_=String()).label("type"),
            Dataset.id.label("id"),
            Dataset.workspace_id.label("workspace_id"),
            Dataset.name.label("name"),
            Dataset.description.label("description"),
            revision_count.label("revision_count"),
            latest_version.label("latest_version"),
            latest_revision_at.label("latest_revision_at"),
            Dataset.created_at.label("created_at"),
            Dataset.created_at.label("updated_at"),
            literal(None, type_=String(64)).label("robot_type"),
            literal(None, type_=String(256)).label("dataset_id"),
            literal(None, type_=Integer()).label("file_count"),
            literal(None, type_=BigInteger()).label("total_size"),
            literal(None, type_=DateTime()).label("completed_at"),
            literal(None, type_=String(16)).label("status"),
            literal(None, type_=Integer()).label("task_set_id"),
            literal(None, type_=Integer()).label("batch_id"),
            literal(None, type_=String(24)).label("copy_status"),
            literal(None, type_=String(64)).label("copy_error_code"),
            literal(None, type_=String()).label("copy_error_message"),
            literal(None, type_=String(36)).label("last_copy_job_id"),
            literal(False).label("oss_uri_available"),
            literal(None, type_=String(256)).label("batch_name"),
            literal(None, type_=String(32)).label("source_session"),
        )
        .select_from(Dataset)
        .outerjoin(DatasetRevision, DatasetRevision.dataset_id == Dataset.id)
        .where(Dataset.workspace_id == workspace_id)
        .group_by(Dataset.id)
    )
    native_query = (
        select(
            literal("native_lerobot", type_=String()).label("type"),
            NativeLerobotDataset.id.label("id"),
            NativeLerobotDataset.workspace_id.label("workspace_id"),
            NativeLerobotDataset.name.label("name"),
            NativeLerobotDataset.description.label("description"),
            literal(0, type_=BigInteger()).label("revision_count"),
            literal(None, type_=Integer()).label("latest_version"),
            NativeLerobotDataset.completed_at.label("latest_revision_at"),
            NativeLerobotDataset.created_at.label("created_at"),
            NativeLerobotDataset.updated_at.label("updated_at"),
            NativeLerobotDataset.robot_type.label("robot_type"),
            NativeLerobotDataset.dataset_id.label("dataset_id"),
            NativeLerobotDataset.file_count.label("file_count"),
            NativeLerobotDataset.total_size.label("total_size"),
            NativeLerobotDataset.completed_at.label("completed_at"),
            NativeLerobotDataset.status.label("status"),
            NativeLerobotDataset.task_set_id.label("task_set_id"),
            NativeLerobotDataset.batch_id.label("batch_id"),
            NativeLerobotDataset.copy_status.label("copy_status"),
            NativeLerobotDataset.copy_error_code.label("copy_error_code"),
            NativeLerobotDataset.copy_error_message.label("copy_error_message"),
            NativeLerobotDataset.last_copy_job_id.label("last_copy_job_id"),
            (NativeLerobotDataset.copy_status == "succeeded").label("oss_uri_available"),
            Batch.name.label("batch_name"),
            case(
                (NativeLerobotDataset.import_session_id.is_(None), "historical_single_import"),
                else_="batch_import_session",
            ).label("source_session"),
        )
        .join(Batch, Batch.id == NativeLerobotDataset.batch_id)
        .where(
            NativeLerobotDataset.workspace_id == workspace_id,
            NativeLerobotDataset.status == "active",
        )
    )
    if normalized_keyword:
        escaped_keyword = f"%{_escape_like(normalized_keyword)}%"
        qrdf_query = qrdf_query.where(Dataset.name.ilike(escaped_keyword, escape="\\"))
        native_query = native_query.where(
            or_(
                NativeLerobotDataset.name.ilike(escaped_keyword, escape="\\"),
                NativeLerobotDataset.dataset_id.ilike(escaped_keyword, escape="\\"),
            )
        )
    catalog = union_all(qrdf_query, native_query).subquery("dataset_catalog")
    total = int(db.execute(select(func.count()).select_from(catalog)).scalar_one() or 0)
    sort_fields = {
        "name": func.lower(catalog.c.name),
        "created_at": catalog.c.created_at,
        "revision_count": catalog.c.revision_count,
        "latest_revision_at": catalog.c.latest_revision_at,
    }
    direction = "asc" if sort_order == "asc" else "desc"
    rows = (
        db.execute(
            select(catalog)
            .order_by(
                getattr(sort_fields[sort_by], direction)().nullslast(),
                catalog.c.type.asc(),
                getattr(catalog.c.id, direction)(),
            )
            .offset(offset)
            .limit(limit)
        )
        .mappings()
        .all()
    )
    return [_dataset_catalog_item(row) for row in rows], total


def _dataset_catalog_item(row: Mapping[str, object]) -> dict[str, object]:
    item_type = str(row["type"])
    common = {
        "id": int(row["id"]),
        "type": item_type,
        "workspace_id": int(row["workspace_id"]),
        "name": str(row["name"]),
        "description": str(row["description"] or ""),
        "created_at": format_api_datetime(row["created_at"]),
        "updated_at": format_api_datetime(row["updated_at"]),
    }
    if item_type == "qrdf":
        return {
            **common,
            "revision_count": int(row["revision_count"] or 0),
            "latest_version": int(row["latest_version"])
            if row["latest_version"] is not None
            else None,
            "latest_revision_at": format_api_datetime(row["latest_revision_at"]),
        }
    return {
        **common,
        "task_set_id": int(row["task_set_id"]),
        "batch_id": int(row["batch_id"]),
        "robot_type": str(row["robot_type"]),
        "dataset_id": str(row["dataset_id"]),
        "file_count": int(row["file_count"]),
        "total_size": int(row["total_size"]),
        "completed_at": format_api_datetime(row["completed_at"]),
        "status": str(row["status"]),
        "copy_status": str(row["copy_status"]),
        "copy_error_code": str(row["copy_error_code"] or ""),
        "copy_error_message": str(row["copy_error_message"] or ""),
        "last_copy_job_id": str(row["last_copy_job_id"] or "") or None,
        "oss_uri_available": bool(row["oss_uri_available"]),
        "batch_name": str(row["batch_name"] or ""),
        "source_session": str(row["source_session"]),
    }


def serialize_dataset_summary(db: Session, dataset_id: int) -> dict[str, object]:
    dataset = get_dataset(db, dataset_id)
    revision_count, latest_version, latest_revision_at = (
        db.query(
            func.count(DatasetRevision.id),
            func.max(DatasetRevision.version),
            func.max(DatasetRevision.created_at),
        )
        .filter(DatasetRevision.dataset_id == dataset.id)
        .one()
    )
    return _dataset_summary(
        dataset,
        revision_count=int(revision_count or 0),
        latest_version=latest_version,
        latest_revision_at=latest_revision_at,
    )


def list_dataset_revisions_for_dataset(db: Session, *, dataset_id: int) -> list[DatasetRevision]:
    dataset = get_dataset(db, dataset_id)
    return list(
        db.query(DatasetRevision)
        .options(joinedload(DatasetRevision.items))
        .filter(DatasetRevision.dataset_id == dataset.id)
        .order_by(DatasetRevision.version.desc(), DatasetRevision.id.desc())
        .all()
    )


def serialize_revision_summary(db: Session, revision_id: int) -> dict[str, object]:
    revision = _get_revision(db, revision_id)
    dataset = db.get(Dataset, revision.dataset_id)
    if dataset is None:
        raise WorkflowConflict("dataset revision scope is unavailable")
    published_episode_ids = {item.published_episode_id for item in revision.items}
    published_to_episode_id = dict(
        db.query(PublishedEpisode.id, PublishedEpisode.episode_id)
        .filter(PublishedEpisode.id.in_(published_episode_ids))
        .all()
    )
    if len(published_to_episode_id) != len(published_episode_ids):
        raise WorkflowConflict("dataset revision published episode is unavailable")
    export_jobs = (
        db.query(JobRun)
        .filter(
            JobRun.kind == DATASET_EXPORT_JOB_KIND,
            JobRun.resource_type == "dataset_revision",
            JobRun.resource_id == str(revision.id),
        )
        .order_by(JobRun.created_at.desc(), JobRun.id.desc())
        .all()
    )
    exports: dict[str, dict[str, object]] = {}
    for job in export_jobs:
        profile = str((job.detail_json or {}).get("export_profile") or "")
        if profile in _ALLOWED_EXPORT_PROFILES and profile not in exports:
            summary = _export_job_summary(job)
            if summary is not None:
                exports[profile] = summary
    latest_export = _export_job_summary(export_jobs[0]) if export_jobs else None
    return {
        "id": revision.id,
        "dataset_id": dataset.id,
        "workspace_id": revision.workspace_id,
        "name": dataset.name,
        "version": revision.version,
        "status": "retired" if revision.retired_at is not None else "active",
        "episode_count": revision.episode_count,
        "retired_at": revision.retired_at.isoformat() if revision.retired_at else None,
        "latest_export": latest_export,
        "exports": exports,
        "items": [
            {
                "position": item.position,
                "episode_id": published_to_episode_id[item.published_episode_id],
                "item_kind": item.item_kind,
                "sample_id": item.sample_id,
                "core_start_ns": item.core_start_ns,
                "core_end_ns": item.core_end_ns,
                "effective_start_ns": item.effective_start_ns,
                "effective_end_ns": item.effective_end_ns,
                "task": item.task_text,
                "outcome": item.outcome,
            }
            for item in revision.items
        ],
    }


def _export_job_summary(job: JobRun | None) -> dict[str, object] | None:
    if job is None:
        return None
    result = job.result_json if isinstance(job.result_json, dict) else {}
    return {
        "id": job.id,
        "export_profile": str((job.detail_json or {}).get("export_profile") or ""),
        "status": job.status,
        "phase": job.phase,
        "progress_percent": max(0, min(100, int(job.progress_percent or 0))),
        "error_code": str(job.error_code or "")[:64],
        "realtime_version": int(job.realtime_version or 0),
        "download_available": bool(result.get("export_uri")),
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _dataset_summary(
    dataset: Dataset,
    *,
    revision_count: int,
    latest_version: object,
    latest_revision_at: datetime | None,
) -> dict[str, object]:
    return {
        "id": dataset.id,
        "workspace_id": dataset.workspace_id,
        "name": dataset.name,
        "description": dataset.description,
        "created_by_user_id": dataset.created_by_user_id,
        "revision_count": revision_count,
        "latest_version": int(latest_version) if latest_version is not None else None,
        "latest_revision_at": latest_revision_at.isoformat() if latest_revision_at else None,
        "created_at": dataset.created_at.isoformat() if dataset.created_at else None,
    }


def retire_dataset_revision(db: Session, revision_id: int, *, actor_id: int) -> DatasetRevision:
    revision = _get_revision(db, revision_id)
    actor = require_workspace_actor(db, actor_id=actor_id, workspace_id=revision.workspace_id)
    if actor.role != "admin":
        raise PermissionError("only an operator or admin can retire dataset revisions")
    if revision.retired_at is None:
        revision.retired_at = datetime.utcnow()
        db.commit()
    return _get_revision(db, revision.id)


def _build_item_spec(
    db: Session, published: PublishedEpisode, item: DatasetItemInput
) -> dict[str, object]:
    episode = db.get(Episode, published.episode_id)
    artifact = db.get(EpisodeArtifact, published.official_artifact_id)
    if episode is None or artifact is None or artifact.storage_role != "official":
        raise WorkflowConflict("published episode official artifact is unavailable")
    root = episode
    while root.parent_episode_id is not None:
        parent = db.get(Episode, root.parent_episode_id)
        if parent is None:
            raise WorkflowConflict("published episode source is unavailable")
        root = parent
    source_fingerprint = _first_checksum(
        episode.source_fingerprint,
        root.source_fingerprint,
    )
    sample_id = str(item.sample_id or f"episode:{published.id}").strip()
    if not sample_id or "/" in sample_id or "\\" in sample_id:
        raise WorkflowConflict("sample_id is invalid")
    is_sample = item.sample_id is not None
    if is_sample:
        if (
            item.core_start_ns is None
            or item.core_end_ns is None
            or item.core_start_ns >= item.core_end_ns
        ):
            raise WorkflowConflict("sample core range is invalid")
        task = str(item.task or "").strip()
        if not task:
            raise WorkflowConflict("sample task is required")
        target_start, target_end = _episode_target_range(episode)
        try:
            calculated_start, calculated_end = calculate_effective_range(
                core_start_ns=int(item.core_start_ns),
                core_end_ns=int(item.core_end_ns),
                target_start_ns=target_start,
                target_end_ns=target_end,
                pre_roll_s=float(item.pre_roll_s),
                post_roll_s=float(item.post_roll_s),
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise WorkflowConflict("sample range is outside the published Episode") from exc
        effective_start = (
            item.effective_start_ns if item.effective_start_ns is not None else calculated_start
        )
        effective_end = (
            item.effective_end_ns if item.effective_end_ns is not None else calculated_end
        )
        if (
            not isinstance(effective_start, int)
            or isinstance(effective_start, bool)
            or not isinstance(effective_end, int)
            or isinstance(effective_end, bool)
            or effective_start >= effective_end
            or effective_start < target_start
            or effective_end > target_end
        ):
            raise WorkflowConflict("sample effective range is invalid")
    else:
        task = str(item.task or episode.task_language or "").strip()
        if not task:
            raise WorkflowConflict("dataset item task is required")
        effective_start = effective_end = None
    return {
        "published_episode_id": int(published.id),
        "item_kind": "sample" if is_sample else "episode",
        "sample_id": sample_id,
        "annotation_revision_id": item.annotation_revision_id,
        "annotation_segment_id": item.annotation_segment_id,
        "source_episode_id": root.episode_uid,
        "source_fingerprint": source_fingerprint,
        "core_start_ns": item.core_start_ns,
        "core_end_ns": item.core_end_ns,
        "effective_start_ns": effective_start,
        "effective_end_ns": effective_end,
        "pre_roll_s": item.pre_roll_s,
        "post_roll_s": item.post_roll_s,
        "task": task,
        "outcome": str(item.outcome or ""),
        "modality_signature": _modality_signature(episode),
        "split": item.split,
        "source_snapshot_hash": _snapshot_hash(
            {
                "episode_id": episode.id,
                "artifact_id": published.official_artifact_id,
                "manifest": published.manifest_hash,
            }
        ),
    }


def _validate_revision_specs(specs: list[dict[str, object]]) -> None:
    identities: set[tuple[object, ...]] = set()
    source_splits: dict[str, str] = {}
    signatures: set[str] = set()
    for spec in specs:
        identity = _revision_item_identity(spec)
        if identity in identities:
            if spec["item_kind"] == "episode":
                raise WorkflowConflict("dataset revision contains duplicate episode items")
            raise WorkflowConflict("dataset revision contains duplicate revision items")
        identities.add(identity)
        source = str(spec["source_fingerprint"])
        split = str(spec["split"])
        previous = source_splits.setdefault(source, split)
        if previous != split:
            raise WorkflowConflict("one source episode cannot span multiple splits")
        signatures.add(str(spec["modality_signature"]))
    if len(signatures) > 1:
        raise WorkflowConflict("dataset revision mixes incompatible modality signatures")


def _revision_item_identity(spec: Mapping[str, object]) -> tuple[object, ...]:
    published_episode_id = int(spec["published_episode_id"])
    if spec["item_kind"] == "episode":
        return ("episode", published_episode_id)
    return (
        "sample",
        published_episode_id,
        str(spec["sample_id"]),
        spec["core_start_ns"],
        spec["core_end_ns"],
        spec["effective_start_ns"],
        spec["effective_end_ns"],
        spec["annotation_revision_id"],
        spec["annotation_segment_id"],
    )


def _normalized_dataset_name(value: str) -> str:
    try:
        return normalized_name(value)
    except ValueError as exc:
        raise WorkflowConflict("dataset name is required") from exc


def _require_dataset_writer(db: Session, *, actor_id: int, workspace_id: int):
    actor = require_workspace_actor(db, actor_id=actor_id, workspace_id=workspace_id)
    if actor.role != "admin":
        raise PermissionError("only an operator or admin can create dataset revisions")
    return actor


def _logical_dataset(
    db: Session,
    *,
    workspace_id: int,
    name: str,
    description: str,
    actor_id: int,
) -> Dataset:
    dataset = _locked_dataset_query(db, workspace_id=workspace_id, name=name).one_or_none()
    if dataset is not None:
        return dataset
    try:
        with db.begin_nested():
            dataset = Dataset(
                workspace_id=workspace_id,
                created_by_user_id=actor_id,
                name=name,
                description=str(description or ""),
            )
            db.add(dataset)
            db.flush()
    except IntegrityError:
        db.expire_all()
        dataset = _locked_dataset_query(db, workspace_id=workspace_id, name=name).one_or_none()
        if dataset is None:
            raise
    return dataset


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _locked_dataset_query(db: Session, *, workspace_id: int, name: str):
    return (
        db.query(Dataset)
        .filter(
            Dataset.workspace_id == workspace_id,
            func.lower(func.btrim(Dataset.name)) == normalized_key(name),
        )
        .with_for_update()
    )


def _new_revision_with_allocated_version(
    db: Session,
    *,
    dataset: Dataset,
    workspace_id: int,
    actor_id: int,
    filter_json: dict[str, object],
    episode_count: int,
    client_request_id: str | None,
    request_fingerprint: str | None,
) -> DatasetRevision:
    next_version = (
        int(
            db.query(func.coalesce(func.max(DatasetRevision.version), 0))
            .filter(DatasetRevision.dataset_id == dataset.id)
            .scalar()
            or 0
        )
        + 1
    )
    revision = DatasetRevision(
        dataset_id=dataset.id,
        workspace_id=workspace_id,
        version=next_version,
        created_by_user_id=actor_id,
        client_request_id=client_request_id,
        request_fingerprint=request_fingerprint,
        filter_json=filter_json,
        manifest_hash="",
        episode_count=episode_count,
    )
    db.add(revision)
    db.flush()
    return revision


def _published_workspace_episodes(
    db: Session, *, workspace_id: int, episode_ids: list[int]
) -> dict[int, PublishedEpisode]:
    """Resolve public Episode IDs to their immutable published counterparts."""
    found = {
        published.episode_id: published
        for published in db.query(PublishedEpisode)
        .join(Episode, PublishedEpisode.episode_id == Episode.id)
        .filter(PublishedEpisode.episode_id.in_(episode_ids), Episode.workspace_id == workspace_id)
        .all()
    }
    if len(found) != len(set(episode_ids)):
        raise DatasetRevisionCandidatesChanged(
            [
                {"episode_id": episode_id, "reason": "candidate_unavailable"}
                for episode_id in episode_ids
                if episode_id not in found
            ]
        )
    return found


def _get_revision(db: Session, revision_id: int) -> DatasetRevision:
    revision = (
        db.query(DatasetRevision)
        .options(joinedload(DatasetRevision.items))
        .filter(DatasetRevision.id == _positive_int(revision_id, "dataset revision"))
        .one_or_none()
    )
    if revision is None:
        raise WorkflowConflict("dataset revision does not exist")
    return revision


def _locked_revision(db: Session, revision_id: int) -> DatasetRevision:
    revision = (
        db.query(DatasetRevision)
        .filter(DatasetRevision.id == _positive_int(revision_id, "dataset revision"))
        .with_for_update()
        .one_or_none()
    )
    if revision is None:
        raise WorkflowConflict("dataset revision does not exist")
    return revision


def _coerce_item(value: DatasetItemInput | Mapping[str, object]) -> DatasetItemInput:
    if isinstance(value, DatasetItemInput):
        data = value
    elif isinstance(value, Mapping):
        data = DatasetItemInput(
            episode_id=_positive_int(value.get("episode_id"), "episode"),
            split="train",
            sample_id=value.get("sample_id") if value.get("sample_id") is not None else None,
            annotation_revision_id=value.get("annotation_revision_id"),
            annotation_segment_id=value.get("annotation_segment_id"),
            core_start_ns=value.get("core_start_ns"),
            core_end_ns=value.get("core_end_ns"),
            effective_start_ns=value.get("effective_start_ns"),
            effective_end_ns=value.get("effective_end_ns"),
            pre_roll_s=float(value.get("pre_roll_s", 0) or 0),
            post_roll_s=float(value.get("post_roll_s", 0) or 0),
            task=str(value.get("task", "") or ""),
            outcome=str(value.get("outcome", "") or ""),
        )
    else:
        raise WorkflowConflict("dataset revision item is invalid")
    if data.split not in _ALLOWED_SPLITS:
        raise WorkflowConflict("dataset revision split is invalid")
    data = DatasetItemInput(**{**data.__dict__, "split": "train"})
    if data.sample_id is not None and not str(data.sample_id).strip():
        raise WorkflowConflict("sample_id is invalid")
    if data.pre_roll_s < 0 or data.post_roll_s < 0:
        raise WorkflowConflict("sample context window is invalid")
    return data


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise WorkflowConflict(f"{label} is invalid")
    return value


def _canonical_json(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise WorkflowConflict("dataset revision filter is invalid")
    try:
        return json.loads(
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        )
    except (TypeError, ValueError) as exc:
        raise WorkflowConflict("dataset revision filter is invalid") from exc


def _request_id(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise WorkflowConflict("dataset revision request id is invalid") from exc


def _revision_request_fingerprint(
    *,
    dataset_id: int,
    actor_id: int,
    filter_json: dict[str, object],
    items: list[DatasetItemInput],
) -> str:
    return _snapshot_hash(
        {
            "dataset_id": dataset_id,
            "actor_id": actor_id,
            "filter_json": filter_json,
            "items": [item.__dict__ for item in items],
        }
    )


def _snapshot_hash(value: object) -> str:
    canonical = json.dumps(value or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _manifest_hash(manifest: dict[str, object]) -> str:
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _modality_signature(episode: Episode) -> str:
    metadata = episode.metadata_json if isinstance(episode.metadata_json, dict) else {}
    cameras = metadata.get("cameras") or metadata.get("sensors", {}).get("cameras", [])
    dimensions = []
    if isinstance(cameras, list):
        for camera in cameras:
            if isinstance(camera, dict):
                dimensions.append(
                    (
                        str(camera.get("topic") or camera.get("name") or ""),
                        int(camera.get("width") or 0),
                        int(camera.get("height") or 0),
                    )
                )
    return _snapshot_hash({"modality": episode.modality, "cameras": sorted(dimensions)})[:128]


def _episode_target_range(episode: Episode) -> tuple[int, int]:
    if (
        episode.kind == "derived"
        and episode.source_start_ns is not None
        and episode.source_end_ns is not None
    ):
        start, end = int(episode.source_start_ns), int(episode.source_end_ns)
    else:
        metadata = episode.metadata_json if isinstance(episode.metadata_json, dict) else {}
        timing = metadata.get("timing") if isinstance(metadata.get("timing"), dict) else {}
        start = timing.get("start_timestamp_ns")
        end = timing.get("end_timestamp_ns")
        if isinstance(start, bool) or isinstance(end, bool):
            raise WorkflowConflict("published Episode timing is invalid")
        try:
            start, end = int(start), int(end)
        except (TypeError, ValueError, OverflowError) as exc:
            raise WorkflowConflict("published Episode timing is invalid") from exc
    if start >= end:
        raise WorkflowConflict("published Episode timing is invalid")
    return start, end


def _first_checksum(*values: object) -> str:
    for value in values:
        normalized = str(value or "").lower()
        if _SHA256.fullmatch(normalized):
            return normalized
    raise WorkflowConflict("published Episode source fingerprint is invalid")
