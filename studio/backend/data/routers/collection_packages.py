"""Workspace-scoped data package listing and status adjustment API."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from data.database import Episode, User, get_db
from data.models.data_package import DataPackage, PackageIntakeReview
from data.security.audit import emit_audit_event
from data.services.collection_access import require_collection_workspace
from data.services.collection_packages import (
    AssignmentLockedError,
    PackageStateConflictError,
    adjust_pending_packages,
    assign_data_package,
    create_supplement_package,
    get_data_package_detail,
    get_offline_manifest,
    void_data_package,
)
from data.services.collection_packages import (
    list_data_packages as list_data_package_records,
)
from data.services.collection_tasks import ArchivedCollectionProjectError
from data.services.episode_admission import (
    admission_eligibility,
    current_episode_admission_fact,
    package_admission_counts,
)
from data.services.package_list_projection import package_list_extras
from data.utils.formatting import format_api_datetime
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/data-packages", tags=["数据包"])

_MIN_DURATION = Decimal("0.01")
_MAX_DURATION = Decimal("99999999.99")


class AddPackageOperation(BaseModel):
    """Operation parameters for adding a data package."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["add"]
    target_duration_hours: Decimal = Field(
        ge=_MIN_DURATION,
        le=_MAX_DURATION,
        max_digits=10,
        decimal_places=2,
    )


class DeletePackageOperation(BaseModel):
    """Operation parameters for deleting a data package."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["delete"]
    data_package_id: int = Field(gt=0)


class ResizePackageOperation(BaseModel):
    """Operation parameters for resizing data package target duration."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["resize"]
    data_package_id: int = Field(gt=0)
    target_duration_hours: Decimal = Field(
        ge=_MIN_DURATION,
        le=_MAX_DURATION,
        max_digits=10,
        decimal_places=2,
    )


class SplitPackageOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["split"]
    data_package_id: int = Field(gt=0)
    durations: list[Decimal] = Field(min_length=2, max_length=200)


class MergePackageOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["merge"]
    data_package_ids: list[int] = Field(min_length=2, max_length=200)


PackageAdjustmentOperation = Annotated[
    AddPackageOperation
    | DeletePackageOperation
    | ResizePackageOperation
    | SplitPackageOperation
    | MergePackageOperation,
    Field(discriminator="op"),
]


class AdjustPackagesRequest(BaseModel):
    """Request parameters for adjusting pending data packages."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    collection_task_id: int = Field(gt=0)
    operations: list[PackageAdjustmentOperation] = Field(min_length=1)


class AssignPackageRequest(BaseModel):
    """Request parameters for assigning package owner and operator."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    collector_id: int | None = Field(default=None, gt=0)
    responsible_collector_id: int | None = Field(default=None, gt=0)
    operator_collector_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def single_collector(self):
        ids = {
            v
            for v in (self.collector_id, self.responsible_collector_id, self.operator_collector_id)
            if v is not None
        }
        if len(ids) != 1:
            raise ValueError("one_collector_per_package_required")
        self.collector_id = self.responsible_collector_id = self.operator_collector_id = ids.pop()
        return self


class VoidPackageRequest(BaseModel):
    """Request parameters for voiding a data package."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=1000)


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub"))
    except (TypeError, ValueError):
        return None


def _decimal(value: Decimal | None) -> str:
    return f"{Decimal(value or 0):.2f}"


def _require_workspace(db: Session, *, user: dict, workspace_id: int) -> None:
    try:
        require_collection_workspace(
            db,
            actor_id=_actor_id(user),
            workspace_id=workspace_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _package_item(
    package: DataPackage, *, admission_counts: dict[str, int] | None = None
) -> dict[str, object]:
    return {
        "id": package.id,
        "package_uid": package.package_uid,
        "workspace_id": package.workspace_id,
        "collection_project_id": package.collection_project_id,
        "collection_task_id": package.collection_task_id,
        "status": package.status,
        "target_duration_hours": _decimal(package.target_duration_hours),
        "captured_duration_hours": (
            _decimal(package.captured_duration_hours)
            if package.captured_duration_hours is not None
            else None
        ),
        "collector_id": package.operator_collector_id,
        "supplement_for_package_id": package.supplement_for_package_id,
        "supplement_reason": package.supplement_reason,
        "created_by_user_id": package.created_by_user_id,
        "created_by_name": package.created_by.email if package.created_by else None,
        "responsible_collector_id": package.responsible_collector_id,
        "operator_collector_id": package.operator_collector_id,
        "assigned_at": format_api_datetime(package.assigned_at),
        "captured_started_at": format_api_datetime(package.captured_started_at),
        "upload_completed_at": format_api_datetime(package.upload_completed_at),
        "created_at": format_api_datetime(package.created_at),
        "updated_at": format_api_datetime(package.updated_at),
        "intake_valid_duration_hours": (
            _decimal(package.intake_valid_duration_hours)
            if package.intake_valid_duration_hours is not None
            else None
        ),
        "admission_counts": admission_counts
        or {"ready": 0, "running": 0, "failed": 0, "reviewed": 0},
        "desensitization": _desensitization_view(package),
    }


def _desensitization_view(package: DataPackage) -> dict[str, object]:
    """Package level desensitization declaration; unknown when never declared."""

    facts = package.qrdf_facts_json or {}
    declaration = facts.get("desensitization") or {}
    return {
        "status": str(declaration.get("status") or "unknown"),
        "by": str(declaration.get("by") or ""),
        "tool": str(declaration.get("tool") or ""),
        "at": declaration.get("at"),
        "policy_version": str(declaration.get("policy_version") or ""),
    }


class DesensitizationDeclarationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    status: Literal["declared", "not_applicable"]
    by: str = Field(min_length=1, max_length=128)
    tool: str = Field(default="", max_length=128)
    policy_version: str = Field(default="", max_length=64)


@router.post("/{data_package_id}/desensitization-declaration")
def record_desensitization_declaration(
    data_package_id: int,
    body: DesensitizationDeclarationRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Record that the caller desensitized this package before uploading it."""

    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    package = (
        db.query(DataPackage)
        .filter(
            DataPackage.id == data_package_id,
            DataPackage.workspace_id == body.workspace_id,
        )
        .one_or_none()
    )
    if package is None:
        raise HTTPException(status_code=404, detail="data package does not exist")
    facts = dict(package.qrdf_facts_json or {})
    facts["desensitization"] = {
        "status": body.status,
        "by": body.by,
        "tool": body.tool,
        "at": datetime.utcnow().isoformat(),
        "policy_version": body.policy_version,
    }
    package.qrdf_facts_json = facts
    db.commit()
    db.refresh(package)
    return success(_package_item(package))


_OMIT = object()
_PRIVATE_QRDF_KEY_TOKENS = frozenset({"key", "path", "uri"})
_PRIVATE_QRDF_KEY_MARKERS = (
    "accesskey",
    "bucket",
    "fingerprint",
    "objectkey",
    "salt",
    "uploadid",
)
_URI_PATTERN = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_WINDOWS_PATH_PATTERN = re.compile(r"^[a-z]:[\\/]", re.IGNORECASE)


def _private_qrdf_key(key: str) -> bool:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    tokens = re.findall(r"[a-z0-9]+", separated.lower())
    compact = "".join(tokens)
    return bool(
        set(tokens) & _PRIVATE_QRDF_KEY_TOKENS
        or any(marker in compact for marker in _PRIVATE_QRDF_KEY_MARKERS)
    )


def _safe_qrdf_value(value: object, *, key: str = "") -> object:
    if _private_qrdf_key(key):
        return _OMIT
    if isinstance(value, dict):
        sanitized: dict[str, object] = {}
        for child_key, child_value in value.items():
            safe_value = _safe_qrdf_value(child_value, key=str(child_key))
            if safe_value is not _OMIT:
                sanitized[str(child_key)] = safe_value
        return sanitized
    if isinstance(value, list):
        sanitized_items = [_safe_qrdf_value(item) for item in value]
        return [item for item in sanitized_items if item is not _OMIT]
    if isinstance(value, str):
        stripped = value.strip()
        if (
            _URI_PATTERN.match(stripped)
            or stripped.startswith(("/", "\\\\"))
            or _WINDOWS_PATH_PATTERN.match(stripped)
        ):
            return _OMIT
    return value


def _episode_item(
    episode: Episode,
    *,
    admission_fact=None,
) -> dict[str, object]:
    metadata = episode.metadata_json if isinstance(episode.metadata_json, dict) else {}
    timing = metadata.get("timing")
    timing = timing if isinstance(timing, dict) else {}
    eligible, eligibility_reason = admission_eligibility(episode, admission_fact)
    if eligible:
        admission_status = "passed"
    elif admission_fact is None:
        admission_status = "missing"
    elif any(
        status in {"pending", "running"}
        for status in (
            admission_fact.integrity_status,
            admission_fact.preview_status,
            admission_fact.output_verification_status,
        )
    ):
        admission_status = "running"
    else:
        admission_status = "failed"
    item: dict[str, object] = {
        "id": episode.id,
        "episode_uid": episode.episode_uid,
        "external_episode_id": (
            metadata.get("collection_upload", {}).get("external_episode_id")
            if isinstance(metadata.get("collection_upload"), dict)
            else None
        ),
        "validity_status": episode.validity_status,
        "modality": episode.modality,
        "preview_available": bool(
            admission_fact is not None
            and admission_fact.is_current
            and admission_fact.preview_status == "ready"
        ),
        "admission_status": admission_status,
        "integrity_source": (
            admission_fact.integrity_source if admission_fact is not None else None
        ),
    }
    if eligibility_reason:
        item["admission_reason"] = eligibility_reason
    duration_seconds = timing.get("duration_s")
    if duration_seconds is not None:
        try:
            item["duration_hours"] = _decimal(Decimal(str(duration_seconds)) / Decimal(3600))
        except (ArithmeticError, ValueError):
            pass
    if "privacy_sensitive" in metadata:
        item["privacy_sensitive"] = bool(metadata["privacy_sensitive"])
    return item


def _intake_review_item(review: PackageIntakeReview) -> dict[str, object]:
    return {
        "id": review.id,
        "reviewer_user_id": review.reviewer_user_id,
        "verdict": review.verdict,
        "is_bulk": review.is_bulk,
        "accepted_episode_ids": review.accepted_episode_ids_json,
        "rejected_episode_ids": review.rejected_episode_ids_json,
        "excluded_episodes": review.excluded_episodes_json,
        "reason": review.reason,
        "episode_reasons": review.episode_reasons_json,
        "reviewed_at": format_api_datetime(review.reviewed_at),
    }


@router.get("")
def list_data_packages(
    workspace_id: int = Query(..., gt=0),
    collection_project_id: list[int] | None = Query(default=None),
    collection_task_id: int | None = Query(default=None, gt=0),
    status: str | None = Query(default=None),
    operator_collector_id: int | None = Query(default=None, gt=0),
    collection_device_id: int | None = Query(default=None, gt=0),
    purpose_label_id: int | None = Query(default=None, gt=0),
    scene_label_id: int | None = Query(default=None, gt=0),
    modality_label_id: int | None = Query(default=None, gt=0),
    training_label_id: int | None = Query(default=None, gt=0),
    created_by_user_id: int | None = Query(default=None, gt=0),
    upload_completed_from: datetime | None = Query(default=None),
    upload_completed_to: datetime | None = Query(default=None),
    page: int | None = Query(default=None, ge=1),
    size: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """List data packages within the workspace."""
    require_permission(user, "episode:read")
    _require_workspace(db, user=user, workspace_id=workspace_id)
    try:
        packages, total = list_data_package_records(
            db,
            workspace_id=workspace_id,
            collection_project_id=collection_project_id,
            collection_task_id=collection_task_id,
            status=status,
            operator_collector_id=operator_collector_id,
            collection_device_id=collection_device_id,
            purpose_label_id=purpose_label_id,
            scene_label_id=scene_label_id,
            modality_label_id=modality_label_id,
            training_label_id=training_label_id,
            created_by_user_id=created_by_user_id,
            upload_completed_from=upload_completed_from,
            upload_completed_to=upload_completed_to,
            page=page,
            size=size,
            with_total=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    extras = package_list_extras(db, packages)
    return success(
        {
            "items": [
                {
                    **_package_item(
                        package, admission_counts=extras[package.id]["admission_counts"]
                    ),
                    **extras[package.id],
                }
                for package in packages
            ],
            "total": total,
            "page": page,
            "size": size if page is not None else len(packages),
        }
    )


@router.get("/creators")
def list_data_package_creators(
    workspace_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """List users who created packages in the workspace for list filtering."""
    require_permission(user, "episode:read")
    _require_workspace(db, user=user, workspace_id=workspace_id)
    rows = (
        db.query(DataPackage.created_by_user_id, User.email)
        .join(User, User.id == DataPackage.created_by_user_id)
        .filter(
            DataPackage.workspace_id == workspace_id,
            DataPackage.created_by_user_id.isnot(None),
        )
        .distinct()
        .order_by(User.email.asc())
        .all()
    )
    return success(
        {"items": [{"id": user_id, "name": email, "email": email} for user_id, email in rows]}
    )


@router.get("/{data_package_id}/episodes/{episode_id}/preview-urls")
def collection_episode_preview_urls(
    data_package_id: int,
    episode_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[dict, Depends(get_current_user)],
    workspace_id: int = Query(..., gt=0),
):
    """Generate signed access URLs for preview streams of a verified episode."""
    from data.infra.storage_provider import get_storage_provider

    require_permission(user, "episode:read")
    _require_workspace(db, user=user, workspace_id=workspace_id)
    episode = (
        db.query(Episode)
        .filter_by(id=episode_id, data_package_id=data_package_id, workspace_id=workspace_id)
        .one_or_none()
    )
    if episode is None:
        raise HTTPException(status_code=404, detail="episode not found in package")
    fact = current_episode_admission_fact(db, episode_id=episode_id)
    if (
        fact is None
        or fact.source_fingerprint != episode.source_fingerprint
        or fact.preview_status != "ready"
        or fact.output_verification_status != "verified"
    ):
        raise HTTPException(status_code=409, detail="verified preview is unavailable")
    from data.services.episode_objects import EpisodeObjectsError, fact_objects, preview_streams

    try:
        stream_objects = preview_streams(fact_objects(fact))
    except EpisodeObjectsError as exc:
        raise HTTPException(status_code=409, detail="verified preview is unavailable") from exc
    if not stream_objects:
        raise HTTPException(status_code=409, detail="verified preview is unavailable")
    db.commit()
    provider = get_storage_provider()
    streams = []
    for stream in stream_objects:
        urls = {"topic": stream["topic"]}
        for kind in ("video", "timeline"):
            ref = stream[kind].storage_ref()
            if ref.bucket_role != "process" or not (ref.version_id or ref.etag):
                raise HTTPException(status_code=409, detail="verified preview is unavailable")
            urls[f"{kind}_url"] = provider.sign_get(ref, expires=300)
        streams.append(urls)
    return success({"streams": streams, "expires_in": 300})


@router.get("/{data_package_id}")
def data_package_detail(
    data_package_id: int,
    workspace_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Get data package details, including member episodes and intake review information."""
    require_permission(user, "episode:read")
    _require_workspace(db, user=user, workspace_id=workspace_id)
    try:
        package, episodes, intake_review = get_data_package_detail(
            db,
            workspace_id=workspace_id,
            data_package_id=data_package_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    safe_qrdf = _safe_qrdf_value(package.qrdf_facts_json)
    qrdf_facts = safe_qrdf if isinstance(safe_qrdf, dict) else {}
    return success(
        {
            **_package_item(package),
            "admission_counts": package_admission_counts(
                db,
                workspace_id=workspace_id,
                data_package_id=package.id,
            ),
            "captured_duration_hours": (
                _decimal(package.captured_duration_hours)
                if package.captured_duration_hours is not None
                else None
            ),
            "governed_valid_duration_hours": (
                _decimal(package.governed_valid_duration_hours)
                if package.governed_valid_duration_hours is not None
                else None
            ),
            "capture_mode": package.capture_mode,
            "upload_completed_at": format_api_datetime(package.upload_completed_at),
            "created_at": format_api_datetime(package.created_at),
            "updated_at": format_api_datetime(package.updated_at),
            "episodes": [
                _episode_item(
                    episode,
                    admission_fact=current_episode_admission_fact(db, episode_id=episode.id),
                )
                for episode in episodes
            ],
            "qrdf_facts": qrdf_facts,
            "intake_review": (
                _intake_review_item(intake_review) if intake_review is not None else None
            ),
        }
    )


@router.post("/adjust")
def adjust_data_packages(
    body: AdjustPackagesRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Batch adjust pending packages under a collection task (supports add, delete, and duration modification)."""
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    try:
        packages = adjust_pending_packages(
            db,
            workspace_id=body.workspace_id,
            collection_task_id=body.collection_task_id,
            operations=[operation.model_dump() for operation in body.operations],
        )
        db.commit()
    except AssignmentLockedError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ArchivedCollectionProjectError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    emit_audit_event(
        "collection.package.adjust",
        actor=str(user.get("email") or ""),
        resource=f"collection_task:{body.collection_task_id}",
        detail={
            "workspace_id": body.workspace_id,
            "operation_count": len(body.operations),
        },
    )
    return success({"packages": [_package_item(package) for package in packages]})


@router.post("/{data_package_id}/assign")
def assign_package(
    data_package_id: int,
    body: AssignPackageRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Assign owner and operator to a data package, generating collection manifest configuration."""
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    try:
        package, manifest = assign_data_package(
            db,
            workspace_id=body.workspace_id,
            data_package_id=data_package_id,
            responsible_collector_id=body.responsible_collector_id,
            operator_collector_id=body.operator_collector_id,
        )
        db.commit()
    except AssignmentLockedError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    emit_audit_event(
        "collection.package.assign",
        actor=str(user.get("email") or ""),
        resource=f"data_package:{data_package_id}",
        detail={
            "workspace_id": body.workspace_id,
            "responsible_collector_id": body.responsible_collector_id,
            "operator_collector_id": body.operator_collector_id,
        },
    )
    return success({**_package_item(package), "offline_manifest": manifest})


@router.get("/{data_package_id}/offline-manifest")
def offline_manifest(
    data_package_id: int,
    workspace_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Retrieve offline collection manifest for the specified data package."""
    require_permission(user, "episode:read")
    _require_workspace(db, user=user, workspace_id=workspace_id)
    try:
        manifest = get_offline_manifest(
            db,
            workspace_id=workspace_id,
            data_package_id=data_package_id,
        )
    except PackageStateConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return success(manifest)


@router.post("/{data_package_id}/void")
def void_package(
    data_package_id: int,
    body: VoidPackageRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Void the specified data package."""
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    try:
        package = void_data_package(
            db,
            workspace_id=body.workspace_id,
            data_package_id=data_package_id,
        )
        db.commit()
    except PackageStateConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    emit_audit_event(
        "collection.package.void",
        actor=str(user.get("email") or ""),
        resource=f"data_package:{data_package_id}",
        detail={"workspace_id": body.workspace_id, "reason": body.reason},
    )
    return success(_package_item(package))


class SupplementPackageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: int = Field(gt=0)
    target_duration_hours: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    reason: str = Field(min_length=1, max_length=1000)
    client_request_id: str = Field(min_length=1, max_length=128)


@router.post("/{data_package_id}/supplements")
def supplement_package(
    data_package_id: int,
    body: SupplementPackageRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    try:
        package = create_supplement_package(
            db,
            workspace_id=body.workspace_id,
            source_package_id=data_package_id,
            target_duration_hours=body.target_duration_hours,
            reason=body.reason,
            actor_id=_actor_id(user),
            client_request_id=body.client_request_id,
        )
        db.commit()
        return success(_package_item(package))
    except (PackageStateConflictError, ArchivedCollectionProjectError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class PackageCollectorAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_package_id: int = Field(gt=0)
    collector_id: int = Field(gt=0)


class BatchAssignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: int = Field(gt=0)
    assignments: list[PackageCollectorAssignment] = Field(min_length=1, max_length=200)


@router.post("/batch-assign")
def batch_assign_packages(
    body: BatchAssignRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    if len({a.data_package_id for a in body.assignments}) != len(body.assignments):
        raise HTTPException(status_code=422, detail="duplicate_package_id")
    try:
        results = []
        for assignment in sorted(body.assignments, key=lambda a: a.data_package_id):
            package, manifest = assign_data_package(
                db,
                workspace_id=body.workspace_id,
                data_package_id=assignment.data_package_id,
                responsible_collector_id=assignment.collector_id,
                operator_collector_id=assignment.collector_id,
            )
            results.append({**_package_item(package), "offline_manifest": manifest})
        db.commit()
        return success({"packages": results})
    except AssignmentLockedError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
