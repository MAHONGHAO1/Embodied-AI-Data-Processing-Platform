"""Authenticated declaration API for direct native LeRobot raw uploads."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from data.database import get_db
from data.infra.object_storage import ObjectStorageError
from data.security.audit import emit_audit_event
from data.services.native_lerobot_direct_uploads import (
    NativeLerobotDirectUploadError,
    cancel_direct_source_upload,
    complete_direct_object_multipart,
    create_direct_source,
    direct_source_item,
    get_direct_source,
    queue_direct_source_validation,
    retry_direct_source_validation,
    sign_direct_object_part,
    start_direct_object_multipart,
)
from data.services.task_dispatcher import dispatch_media_job
from data.utils.helpers import get_current_user, success

router = APIRouter(
    prefix="/native-lerobot-direct-uploads",
    tags=["原生 LeRobot 直传"],
)
logger = logging.getLogger("quicdata.native_lerobot_direct_uploads")


class DirectObjectDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1024)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(min_length=64, max_length=64)


class CreateDirectSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    source_workspace_id: int | None = Field(default=None, gt=0)
    robot_type: str = Field(min_length=1, max_length=64)
    dataset_id: str = Field(min_length=1, max_length=256)
    objects: list[DirectObjectDeclaration] = Field(min_length=1, max_length=10_000)


class MultipartPartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part_number: int = Field(ge=1, le=10_000)
    etag: str = Field(min_length=1, max_length=256)


class MultipartCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parts: list[MultipartPartRequest] = Field(min_length=1, max_length=10_000)


class SignPartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part_number: int = Field(ge=1, le=10_000)


def _require_catalog_admin(user: dict) -> None:
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=403,
            detail="only an admin can manage native LeRobot sources",
        )


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub"))
    except (TypeError, ValueError):
        return None


@router.post("")
def create_native_lerobot_direct_upload(
    body: CreateDirectSourceRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_catalog_admin(user)
    try:
        source = create_direct_source(
            db,
            name=body.name,
            description=body.description,
            source_workspace_id=body.source_workspace_id,
            robot_type=body.robot_type,
            dataset_id=body.dataset_id,
            objects=[item.model_dump() for item in body.objects],
            created_by_user_id=_actor_id(user),
        )
        db.commit()
        db.refresh(source)
    except NativeLerobotDirectUploadError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    emit_audit_event(
        "native_lerobot_direct.declare",
        actor=str(user.get("email") or ""),
        resource=f"native_lerobot_direct_source:{source.id}",
        detail={"file_count": source.file_count, "total_size": source.total_size},
    )
    return success(direct_source_item(source))


@router.get("/{source_id}")
def get_native_lerobot_direct_upload(
    source_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_catalog_admin(user)
    try:
        source = get_direct_source(db, source_id=source_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return success(direct_source_item(source))


@router.post("/{source_id}/objects/{object_id}/multipart/init")
def initialize_native_lerobot_direct_object(
    source_id: str,
    object_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_catalog_admin(user)
    try:
        item = start_direct_object_multipart(db, source_id=source_id, object_id=object_id)
        db.commit()
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NativeLerobotDirectUploadError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ObjectStorageError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return success(item)


@router.post("/{source_id}/objects/{object_id}/multipart/sign-part")
def sign_native_lerobot_direct_object_part(
    source_id: str,
    object_id: str,
    body: SignPartRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_catalog_admin(user)
    try:
        item = sign_direct_object_part(
            db,
            source_id=source_id,
            object_id=object_id,
            part_number=body.part_number,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NativeLerobotDirectUploadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ObjectStorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return success(item)


@router.post("/{source_id}/objects/{object_id}/multipart/complete")
def complete_native_lerobot_direct_object(
    source_id: str,
    object_id: str,
    body: MultipartCompleteRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_catalog_admin(user)
    try:
        item = complete_direct_object_multipart(
            db,
            source_id=source_id,
            object_id=object_id,
            parts=[part.model_dump() for part in body.parts],
        )
        db.commit()
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NativeLerobotDirectUploadError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ObjectStorageError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return success(item)


@router.post("/{source_id}/cancel")
def cancel_native_lerobot_direct_source(
    source_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Cancel an in-progress browser upload and reclaim its raw objects."""
    _require_catalog_admin(user)
    try:
        source = cancel_direct_source_upload(db, source_id=source_id)
        db.commit()
        db.refresh(source)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NativeLerobotDirectUploadError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ObjectStorageError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    emit_audit_event(
        "native_lerobot_direct.cancel",
        actor=str(user.get("email") or ""),
        resource=f"native_lerobot_direct_source:{source.id}",
        detail={"status": source.status},
    )
    return success(direct_source_item(source))


@router.post("/{source_id}/complete")
def complete_native_lerobot_direct_source(
    source_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_catalog_admin(user)
    try:
        source, job = queue_direct_source_validation(
            db, source_id=source_id, actor_id=_actor_id(user)
        )
        db.commit()
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NativeLerobotDirectUploadError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        dispatch_media_job(job, worker_prechecked=True)
    except Exception as exc:
        # The durable queued JobRun is recoverable even when dispatch is not
        # available from this HTTP process.
        logger.warning(
            "native direct validation remains queued source_id=%s job_id=%s error_type=%s",
            source.id,
            job.id,
            type(exc).__name__,
        )
    return success({**direct_source_item(source), "validation_job_id": job.id})


@router.post("/{source_id}/retry")
def retry_native_lerobot_direct_source(
    source_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Retry a retryable direct-source validation without changing raw input."""
    _require_catalog_admin(user)
    try:
        result = retry_direct_source_validation(
            db,
            source_id=source_id,
            actor_id=_actor_id(user),
        )
        job = result.job
        db.commit()
        source = get_direct_source(db, source_id=source_id)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NativeLerobotDirectUploadError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        dispatch_media_job(job, worker_prechecked=True)
    except Exception as exc:
        logger.warning(
            "native direct retry remains queued source_id=%s job_id=%s error_type=%s",
            source.id,
            job.id,
            type(exc).__name__,
        )
    if result.created:
        emit_audit_event(
            "native_lerobot_direct.retry",
            actor=str(user.get("email") or ""),
            resource=f"native_lerobot_direct_source:{source.id}",
            detail={"retry_job_id": job.id},
        )
    return success({**direct_source_item(source), "validation_job_id": job.id})
