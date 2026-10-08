"""Workspace-scoped collection upload-session API."""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from data.database import JobRun, get_db
from data.models.collection_upload import (
    CollectionUploadSession,
    CollectionUploadSessionPackage,
)
from data.schemas.client_admission import CONTENT_MD5_PATTERN, ClientAdmissionRequest
from data.security.audit import emit_audit_event
from data.services.client_admission import client_admission_capabilities
from data.services.collection_access import require_collection_workspace
from data.services.collection_packages import PackageStateConflictError
from data.services.collection_tasks import ArchivedCollectionProjectError
from data.services.collection_upload_intake import (
    complete_chunked_upload,
    complete_oss_multipart,
    complete_preview_multipart,
    declare_package_sources,
    sign_part,
    sign_preview_part,
    start_chunked_upload,
    start_oss_multipart,
    start_preview_multipart,
    write_chunk,
)
from data.services.collection_upload_sessions import (
    cancel_upload_session as cancel_upload_session_record,
)
from data.services.collection_upload_sessions import (
    create_upload_session as create_upload_session_record,
)
from data.services.collection_upload_sessions import (
    get_upload_session as get_upload_session_record,
)
from data.services.collection_upload_sessions import (
    list_upload_sessions as list_upload_session_records,
)
from data.services.duance_imports import DuanceImportValidationError
from data.services.import_intake import MAX_IMPORT_CHUNK_BYTES, MAX_IMPORT_CHUNKS
from data.services.task_dispatcher import dispatch_media_job
from data.utils.formatting import format_api_datetime
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/upload-sessions", tags=["采集上传会话"])
logger = logging.getLogger("quicdata.collection_upload_sessions")

UploadMode = Literal[
    "duance_sdk",
    "chunked",
    "oss_multipart",
]


class UploadSessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    collection_project_id: int = Field(gt=0)
    package_uids: list[str] = Field(min_length=1)
    upload_mode: UploadMode


class UploadSessionCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)


class DuanceSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode_id: str = Field(min_length=1, max_length=256)
    start_ns: str = Field(min_length=1, max_length=32)
    end_ns: str = Field(min_length=1, max_length=32)
    metadata_sha256: str = Field(min_length=64, max_length=64)
    data_mcap_sha256: str = Field(min_length=64, max_length=64)


class DuanceDataFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(min_length=64, max_length=64)


class PackageSourceDeclarationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_uid: str = Field(min_length=1, max_length=64)
    source: DuanceSourceRequest
    metadata_text: str = Field(min_length=1, max_length=1024 * 1024)
    data_file: DuanceDataFileRequest
    client_admission: ClientAdmissionRequest | None = None


class PackageDeclarationsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    items: list[PackageSourceDeclarationRequest] = Field(min_length=1)


class ChunkedInitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    total_chunks: int = Field(ge=1, le=MAX_IMPORT_CHUNKS)
    source_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class WorkspaceUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)


class OssTargetRequest(BaseModel):
    """One multipart target: an episode data file (source_id) or a preview file (file_id)."""

    model_config = ConfigDict(extra="forbid")

    source_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    file_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def _single_target(self):
        if self.source_id is not None and self.file_id is not None:
            raise ValueError("multipart target must be either source_id or file_id")
        return self


class OssMultipartInitRequest(OssTargetRequest):
    workspace_id: int = Field(gt=0)
    total_size_bytes: int = Field(gt=0)
    content_type: str | None = Field(default=None, min_length=1, max_length=256)


class OssSignPartRequest(OssTargetRequest):
    workspace_id: int = Field(gt=0)
    part_number: int = Field(ge=1, le=10_000)
    content_md5: str | None = Field(default=None, pattern=CONTENT_MD5_PATTERN)


class OssMultipartPartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part_number: int = Field(ge=1, le=10_000)
    etag: str = Field(min_length=1, max_length=256)


class OssMultipartCompleteRequest(OssTargetRequest):
    workspace_id: int = Field(gt=0)
    parts: list[OssMultipartPartRequest] = Field(min_length=1, max_length=10_000)


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub"))
    except (TypeError, ValueError):
        return None


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


def _workspace_id_for_session(db: Session, upload_session_id: str) -> int:
    upload_session = db.get(CollectionUploadSession, upload_session_id)
    if upload_session is None:
        raise HTTPException(status_code=404, detail="upload session does not exist")
    return int(upload_session.workspace_id)


async def _read_bounded_chunk(request: Request) -> bytes:
    declared_length = request.headers.get("content-length")
    if declared_length:
        try:
            if int(declared_length) > MAX_IMPORT_CHUNK_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="collection upload chunk exceeds the chunk size limit",
                )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid content-length") from exc
    blocks: list[bytes] = []
    total = 0
    async for block in request.stream():
        total += len(block)
        if total > MAX_IMPORT_CHUNK_BYTES:
            raise HTTPException(
                status_code=413,
                detail="collection upload chunk exceeds the chunk size limit",
            )
        blocks.append(block)
    return b"".join(blocks)


def _package_item(link: CollectionUploadSessionPackage) -> dict[str, object]:
    return {
        "data_package_id": link.data_package_id,
        "package_uid": link.package_uid,
        "status": link.package.status if link.package is not None else None,
    }


def _session_item(upload_session: CollectionUploadSession) -> dict[str, object]:
    return {
        "id": upload_session.id,
        "workspace_id": upload_session.workspace_id,
        "collection_project_id": upload_session.collection_project_id,
        "status": upload_session.status,
        "upload_mode": upload_session.upload_mode,
        "created_by_user_id": upload_session.created_by_user_id,
        "error_code": upload_session.error_code,
        "error_message": upload_session.error_message,
        "created_at": format_api_datetime(upload_session.created_at),
        "updated_at": format_api_datetime(upload_session.updated_at),
        "packages": [
            _package_item(link)
            for link in sorted(
                upload_session.package_links,
                key=lambda item: item.data_package_id,
            )
        ],
    }


@router.post("")
def create_upload_session(
    body: UploadSessionCreateRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    try:
        upload_session = create_upload_session_record(
            db,
            workspace_id=body.workspace_id,
            collection_project_id=body.collection_project_id,
            package_uids=body.package_uids,
            upload_mode=body.upload_mode,
            actor_id=_actor_id(user),
        )
        db.commit()
        upload_session = get_upload_session_record(
            db,
            workspace_id=body.workspace_id,
            upload_session_id=upload_session.id,
        )
    except ArchivedCollectionProjectError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PackageStateConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    emit_audit_event(
        "collection.upload.create",
        actor=str(user.get("email") or ""),
        resource=f"collection_upload_session:{upload_session.id}",
        detail={
            "workspace_id": body.workspace_id,
            "collection_project_id": body.collection_project_id,
            "package_count": len(body.package_uids),
        },
    )
    return success(_session_item(upload_session))


@router.get("")
def list_upload_sessions(
    workspace_id: int = Query(..., gt=0),
    collection_project_id: int | None = Query(default=None, gt=0),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:read")
    _require_workspace(db, user=user, workspace_id=workspace_id)
    try:
        upload_sessions = list_upload_session_records(
            db,
            workspace_id=workspace_id,
            collection_project_id=collection_project_id,
            status=status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return success({"items": [_session_item(item) for item in upload_sessions]})


@router.get("/capabilities")
def upload_capabilities(
    workspace_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Advertise what this deployment accepts from upload clients."""
    require_permission(user, "episode:read")
    _require_workspace(db, user=user, workspace_id=workspace_id)
    return success({"client_admission": client_admission_capabilities()})


@router.get("/{upload_session_id}")
def get_upload_session(
    upload_session_id: str,
    workspace_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:read")
    _require_workspace(db, user=user, workspace_id=workspace_id)
    try:
        upload_session = get_upload_session_record(
            db,
            workspace_id=workspace_id,
            upload_session_id=upload_session_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return success(_session_item(upload_session))


@router.post("/{upload_session_id}/cancel")
def cancel_upload_session(
    upload_session_id: str,
    body: UploadSessionCancelRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    try:
        upload_session = cancel_upload_session_record(
            db,
            workspace_id=body.workspace_id,
            upload_session_id=upload_session_id,
        )
        db.commit()
        upload_session = get_upload_session_record(
            db,
            workspace_id=body.workspace_id,
            upload_session_id=upload_session_id,
        )
    except PackageStateConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    emit_audit_event(
        "collection.upload.cancel",
        actor=str(user.get("email") or ""),
        resource=f"collection_upload_session:{upload_session.id}",
        detail={"workspace_id": body.workspace_id},
    )
    return success(_session_item(upload_session))


@router.post("/{upload_session_id}/declarations")
def declare_upload_session_packages(
    upload_session_id: str,
    body: PackageDeclarationsRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    try:
        result = declare_package_sources(
            db,
            workspace_id=body.workspace_id,
            upload_session_id=upload_session_id,
            declarations=[item.model_dump() for item in body.items],
        )
        db.commit()
    except PackageStateConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (DuanceImportValidationError, ValueError) as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return success(result)


@router.post("/{upload_session_id}/chunked/init")
def initialize_chunked_upload(
    upload_session_id: str,
    body: ChunkedInitRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    try:
        result = start_chunked_upload(
            db,
            workspace_id=body.workspace_id,
            upload_session_id=upload_session_id,
            total_chunks=body.total_chunks,
            source_id=body.source_id,
        )
        db.commit()
    except PackageStateConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return success(result)


@router.put("/{upload_session_id}/chunked/{chunk_index}")
async def upload_chunk(
    upload_session_id: str,
    chunk_index: int,
    request: Request,
    source_id: str | None = Query(default=None, pattern=r"^[a-f0-9]{64}$"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    workspace_id = _workspace_id_for_session(db, upload_session_id)
    _require_workspace(db, user=user, workspace_id=workspace_id)
    content = await _read_bounded_chunk(request)
    try:
        result = write_chunk(
            db,
            workspace_id=workspace_id,
            upload_session_id=upload_session_id,
            chunk_index=chunk_index,
            content=content,
            source_id=source_id,
        )
        db.commit()
    except PackageStateConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return success(result)


@router.post("/{upload_session_id}/chunked/complete")
def finish_chunked_upload(
    upload_session_id: str,
    body: WorkspaceUploadRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    try:
        complete_chunked_upload(
            db,
            workspace_id=body.workspace_id,
            upload_session_id=upload_session_id,
        )
        db.commit()
        _dispatch_parse_job(db, upload_session_id)
        upload_session = get_upload_session_record(
            db,
            workspace_id=body.workspace_id,
            upload_session_id=upload_session_id,
        )
    except PackageStateConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return success(_session_item(upload_session))


@router.post("/{upload_session_id}/oss/init")
def initialize_oss_multipart_upload(
    upload_session_id: str,
    body: OssMultipartInitRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    try:
        if body.file_id is not None:
            result = start_preview_multipart(
                db,
                workspace_id=body.workspace_id,
                upload_session_id=upload_session_id,
                file_id=body.file_id,
                total_size_bytes=body.total_size_bytes,
                content_type=body.content_type,
            )
        else:
            result = start_oss_multipart(
                db,
                workspace_id=body.workspace_id,
                upload_session_id=upload_session_id,
                total_size_bytes=body.total_size_bytes,
                content_type=body.content_type,
                source_id=body.source_id,
            )
        db.commit()
    except PackageStateConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return success(result)


@router.post("/{upload_session_id}/oss/sign-part")
def sign_oss_multipart_part(
    upload_session_id: str,
    body: OssSignPartRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    try:
        if body.file_id is not None:
            result = sign_preview_part(
                db,
                workspace_id=body.workspace_id,
                upload_session_id=upload_session_id,
                file_id=body.file_id,
                part_number=body.part_number,
                content_md5=body.content_md5,
            )
        else:
            result = sign_part(
                db,
                workspace_id=body.workspace_id,
                upload_session_id=upload_session_id,
                part_number=body.part_number,
                source_id=body.source_id,
                content_md5=body.content_md5,
            )
    except PackageStateConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return success(result)


@router.post("/{upload_session_id}/oss/complete")
def finish_oss_multipart_upload(
    upload_session_id: str,
    body: OssMultipartCompleteRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    try:
        parts = [part.model_dump() for part in body.parts]
        if body.file_id is not None:
            completed_session = complete_preview_multipart(
                db,
                workspace_id=body.workspace_id,
                upload_session_id=upload_session_id,
                file_id=body.file_id,
                parts=parts,
            )
        else:
            completed_session = complete_oss_multipart(
                db,
                workspace_id=body.workspace_id,
                upload_session_id=upload_session_id,
                parts=parts,
                source_id=body.source_id,
            )
        db.commit()
        if completed_session.status == "uploaded":
            _dispatch_parse_job(db, upload_session_id)
        upload_session = get_upload_session_record(
            db,
            workspace_id=body.workspace_id,
            upload_session_id=upload_session_id,
        )
    except PackageStateConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return success(_session_item(upload_session))


def _dispatch_parse_job(db: Session, upload_session_id: str) -> None:
    job = (
        db.query(JobRun)
        .filter(
            JobRun.kind == "collection_upload_parse",
            JobRun.resource_id == upload_session_id,
        )
        .one_or_none()
    )
    if job is None:
        raise ValueError("collection upload parse job is unavailable")
    try:
        dispatch_media_job(job, worker_prechecked=True)
    except Exception as exc:
        logger.warning(
            "collection upload parse job remains queued job_id=%s error_type=%s",
            job.id,
            type(exc).__name__,
        )
