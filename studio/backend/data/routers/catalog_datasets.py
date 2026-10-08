"""Catalog datasets API (global scope, does not reuse legacy /datasets)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from data.database import JobRun, get_db
from data.infra.object_storage import ObjectStorageError, StorageObjectIntegrityError
from data.models.catalog_dataset import (
    CatalogDataset,
    CatalogDatasetExport,
    CatalogDatasetVersion,
)
from data.security.audit import emit_audit_event
from data.services.catalog_datasets import (
    CatalogDatasetConflict,
    CatalogDatasetError,
    CatalogDatasetNotSupported,
    archive_catalog_version,
    create_catalog_dataset,
    create_catalog_version,
    delete_catalog_version,
    export_catalog_version,
    get_catalog_dataset,
    get_catalog_version,
    import_lerobot_direct,
    list_catalog_datasets_page,
    list_catalog_versions_page,
    serialize_export,
    version_asset_ids,
)
from data.services.catalog_export_jobs import (
    CatalogExportError,
    catalog_export_download,
    recover_catalog_export_delivery,
    retry_catalog_export,
)
from data.utils.formatting import format_api_datetime
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/catalog-datasets", tags=["目录数据集"])
_LEGACY_NATIVE_LEROBOT_RETIRED_DETAIL = (
    "legacy native LeRobot catalog import is retired; use /native-lerobot-direct-uploads"
)


class CreateCatalogDatasetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    source_kind: Literal["qrdf_assets"] = "qrdf_assets"


class CreateCatalogVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_asset_ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1)


class ExportCatalogVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["qrdf_0_2", "lerobot_3_0"]
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)


class LerobotImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    native_lerobot_dataset_id: int = Field(gt=0)


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub"))
    except (TypeError, ValueError):
        return None


def _require_catalog_admin(user: dict) -> None:
    """Write operations on assets/catalog datasets are restricted to administrators (matrix: CRUD/export = admin only)."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="only an admin can manage catalog datasets")


def _raise_legacy_native_lerobot_retired() -> None:
    """New global native versions only originate from direct raw uploads."""
    raise HTTPException(status_code=410, detail=_LEGACY_NATIVE_LEROBOT_RETIRED_DETAIL)


def _dataset_item(dataset: CatalogDataset) -> dict[str, object]:
    return {
        "id": dataset.id,
        "name": dataset.name,
        "description": dataset.description or "",
        "status": dataset.status,
        "source_kind": dataset.source_kind,
        "native_lerobot_dataset_id": dataset.native_lerobot_dataset_id,
        "created_by_user_id": dataset.created_by_user_id,
        "created_at": format_api_datetime(dataset.created_at),
        "updated_at": format_api_datetime(dataset.updated_at),
    }


def _version_item(version: CatalogDatasetVersion) -> dict[str, object]:
    return {
        "id": version.id,
        "dataset_id": version.dataset_id,
        "version": version.version,
        "status": version.status,
        "data_asset_ids": version_asset_ids(version),
        "source_snapshot_json": version.source_snapshot_json or {},
        "source_snapshot_id": version.source_snapshot_id or "",
        "asset_snapshots": [
            {
                "asset_id": item.data_asset_id,
                "snapshot_id": item.asset_snapshot_id,
                "position": item.position,
            }
            for item in sorted(version.assets, key=lambda row: row.position)
        ],
        "created_by_user_id": version.created_by_user_id,
        "created_at": format_api_datetime(version.created_at),
    }


@router.post("")
def create_catalog_dataset_api(
    body: CreateCatalogDatasetRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_catalog_admin(user)
    try:
        dataset = create_catalog_dataset(
            db,
            name=body.name,
            description=body.description,
            source_kind=body.source_kind,
            created_by_user_id=_actor_id(user),
        )
        db.commit()
    except CatalogDatasetConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc
    except CatalogDatasetError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=exc.as_detail()) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    emit_audit_event(
        "catalog.dataset.create",
        actor=str(user.get("email") or ""),
        resource=f"catalog_dataset:{dataset.id}",
        detail={"source_kind": dataset.source_kind},
    )
    return success(_dataset_item(dataset))


@router.get("")
def list_catalog_datasets_api(
    limit: int | None = Query(default=None, ge=1, le=100),
    offset: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:read")
    page = list_catalog_datasets_page(db, limit=limit, offset=offset)
    return success({**page, "items": [_dataset_item(item) for item in page["items"]]})


@router.post("/lerobot-imports")
def import_lerobot_direct_api(
    body: LerobotImportRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_catalog_admin(user)
    _raise_legacy_native_lerobot_retired()
    try:
        dataset, version = import_lerobot_direct(
            db,
            name=body.name,
            description=body.description,
            native_lerobot_dataset_id=body.native_lerobot_dataset_id,
            created_by_user_id=_actor_id(user),
        )
        db.commit()
        db.refresh(version)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CatalogDatasetConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc
    except CatalogDatasetError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=exc.as_detail()) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    emit_audit_event(
        "catalog.dataset.create",
        actor=str(user.get("email") or ""),
        resource=f"catalog_dataset:{dataset.id}",
        detail={
            "source_kind": dataset.source_kind,
            "native_lerobot_dataset_id": body.native_lerobot_dataset_id,
        },
    )
    return success(
        {
            **_dataset_item(dataset),
            "version": _version_item(version),
        }
    )


@router.post("/{dataset_id}/versions")
def create_catalog_version_api(
    dataset_id: int,
    body: CreateCatalogVersionRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_catalog_admin(user)
    try:
        version = create_catalog_version(
            db,
            dataset_id=dataset_id,
            data_asset_ids=list(body.data_asset_ids),
            created_by_user_id=_actor_id(user),
        )
        db.commit()
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CatalogDatasetConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc
    except CatalogDatasetError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=exc.as_detail()) from exc

    emit_audit_event(
        "catalog.version.create",
        actor=str(user.get("email") or ""),
        resource=f"catalog_dataset_version:{version.id}",
        detail={
            "dataset_id": dataset_id,
            "version": version.version,
            "asset_count": len(version.assets),
        },
    )
    return success(_version_item(version))


@router.get("/{dataset_id}/versions")
def list_catalog_versions_api(
    dataset_id: int,
    limit: int | None = Query(default=None, ge=1, le=100),
    offset: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:read")
    try:
        page = list_catalog_versions_page(db, dataset_id=dataset_id, limit=limit, offset=offset)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return success({**page, "items": [_version_item(item) for item in page["items"]]})


@router.get("/{dataset_id}")
def get_catalog_dataset_api(
    dataset_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:read")
    try:
        dataset = get_catalog_dataset(db, dataset_id=dataset_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return success(_dataset_item(dataset))


@router.post("/versions/{version_id}/export")
def export_catalog_version_api(
    version_id: int,
    body: ExportCatalogVersionRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_catalog_admin(user)
    try:
        export = export_catalog_version(
            db,
            version_id=version_id,
            export_format=body.format,
            idempotency_key=body.idempotency_key,
            actor_id=_actor_id(user),
        )
        db.commit()
        from data.services.task_dispatcher import dispatch_media_job

        job = db.scalar(
            select(JobRun).where(
                JobRun.resource_type == "artifact",
                JobRun.resource_id == str(export.id),
                JobRun.kind == "catalog_export",
            )
        )
        if job is not None and export.status == "queued":
            # The delivery fence in dispatch_media_job makes a post-commit
            # broker failure recoverable by the periodic recovery task.
            dispatch_media_job(job, worker_prechecked=True)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CatalogDatasetNotSupported as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=exc.as_detail()) from exc
    except CatalogDatasetError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=exc.as_detail()) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        from data.services.task_dispatcher import JobDispatchUnavailable

        if isinstance(exc, JobDispatchUnavailable):
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        raise

    emit_audit_event(
        "catalog.version.export",
        actor=str(user.get("email") or ""),
        resource=f"catalog_dataset_version:{version_id}",
        detail={"format": body.format, "export_id": export.id},
    )
    return success(serialize_export(export))


@router.post("/exports/{export_id}/retry")
def retry_catalog_export_api(
    export_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_catalog_admin(user)
    try:
        export, job, _created = retry_catalog_export(db, export_id, actor_id=_actor_id(user))
        db.commit()
        if job is not None and export.status == "queued":
            from data.services.task_dispatcher import dispatch_media_job

            dispatch_media_job(job, worker_prechecked=True)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        from data.services.task_dispatcher import JobDispatchUnavailable

        if isinstance(exc, JobDispatchUnavailable):
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        raise
    return success(serialize_export(export))


@router.get("/exports/{export_id}")
def get_catalog_export_api(
    export_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Return the credential-free durable delivery descriptor for one export."""
    require_permission(user, "dataset:read")
    export = db.get(CatalogDatasetExport, export_id)
    if export is None:
        raise HTTPException(status_code=404, detail="catalog export does not exist")
    return success(serialize_export(export))


@router.get("/versions/{version_id}")
def get_catalog_version_api(
    version_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:read")
    try:
        version = get_catalog_version(db, version_id=version_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return success(
        {
            **_version_item(version),
            "exports": [
                serialize_export(export)
                for export in sorted(version.exports, key=lambda row: row.id, reverse=True)
            ],
        }
    )


@router.get("/exports/{export_id}/download")
def get_catalog_export_download_api(
    export_id: int,
    url_ttl_seconds: int = Query(default=3600, ge=1, le=604800),
    oss_network: Literal["internal", "public"] = "internal",
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:read")
    try:
        download = catalog_export_download(
            db, export_id=export_id, url_ttl_seconds=url_ttl_seconds, oss_network=oss_network
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CatalogExportError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StorageObjectIntegrityError as exc:
        raise HTTPException(status_code=409, detail="catalog_export_artifact_changed") from exc
    except ObjectStorageError as exc:
        raise HTTPException(status_code=503, detail="catalog_export_storage_unavailable") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return success(download)


@router.post("/exports/{export_id}/recover")
def recover_catalog_export_api(
    export_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_catalog_admin(user)
    try:
        export = recover_catalog_export_delivery(db, export_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return success(serialize_export(export))


@router.post("/versions/{version_id}/archive")
def archive_catalog_version_api(
    version_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_catalog_admin(user)
    try:
        version = archive_catalog_version(db, version_id=version_id)
        db.commit()
        version = get_catalog_version(db, version_id=version.id)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    emit_audit_event(
        "catalog.version.archive",
        actor=str(user.get("email") or ""),
        resource=f"catalog_dataset_version:{version_id}",
        detail={"status": version.status},
    )
    return success(_version_item(version))


@router.delete("/versions/{version_id}")
def delete_catalog_version_api(
    version_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_catalog_admin(user)
    try:
        delete_catalog_version(db, version_id=version_id)
        db.commit()
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CatalogDatasetConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc
    return success({"deleted": True, "version_id": version_id})
