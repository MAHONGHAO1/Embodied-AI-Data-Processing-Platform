"""Upload-session lifecycle and package binding."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.orm import Session, selectinload

from data.models.collection_core import CollectionProject
from data.models.collection_upload import (
    COLLECTION_UPLOAD_SESSION_STATUSES,
    CollectionUploadSession,
    CollectionUploadSessionPackage,
)
from data.models.data_package import DataPackage, PackageIntakeReview
from data.services.collection_packages import PackageStateConflictError
from data.services.collection_tasks import ArchivedCollectionProjectError

UPLOADABLE_PACKAGE_STATUSES = frozenset({"assigned", "parse_failed", "pending_intake_review"})
ACTIVE_SESSION_STATUSES = frozenset({"init", "uploading", "uploaded", "parsing"})
TERMINAL_SESSION_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


def _lock_enabled_project(
    db: Session,
    *,
    workspace_id: int,
    collection_project_id: int,
) -> CollectionProject:
    project = (
        db.query(CollectionProject)
        .filter(
            CollectionProject.id == collection_project_id,
            CollectionProject.workspace_id == workspace_id,
        )
        .with_for_update()
        .one_or_none()
    )
    if project is None:
        raise LookupError("collection project does not exist in this workspace")
    if project.status != "enabled":
        raise ArchivedCollectionProjectError(
            "archived collection project cannot accept upload sessions"
        )
    return project


def _load_packages_for_update(
    db: Session,
    *,
    workspace_id: int,
    package_uids: list[str],
) -> list[DataPackage]:
    packages = (
        db.query(DataPackage)
        .filter(
            DataPackage.workspace_id == workspace_id,
            DataPackage.package_uid.in_(package_uids),
        )
        .order_by(DataPackage.id)
        .with_for_update()
        .all()
    )
    if len(packages) != len(package_uids):
        raise LookupError("data package does not exist in this workspace")
    return packages


def _has_active_session(db: Session, *, data_package_id: int) -> bool:
    return (
        db.query(CollectionUploadSessionPackage.id)
        .join(
            CollectionUploadSession,
            CollectionUploadSession.id == CollectionUploadSessionPackage.upload_session_id,
        )
        .filter(
            CollectionUploadSessionPackage.data_package_id == data_package_id,
            CollectionUploadSession.status.in_(tuple(ACTIVE_SESSION_STATUSES)),
        )
        .first()
        is not None
    )


def create_upload_session(
    db: Session,
    *,
    workspace_id: int,
    collection_project_id: int,
    package_uids: list[str],
    upload_mode: str,
    actor_id: int | None,
) -> CollectionUploadSession:
    """Create one session and atomically reserve all requested packages."""
    if upload_mode not in {"duance_sdk", "chunked", "oss_multipart"}:
        raise ValueError("upload_mode is not supported by collection intake")
    if not package_uids:
        raise ValueError("package_uids must not be empty")
    if len(set(package_uids)) != len(package_uids):
        raise ValueError("package_uids must not contain duplicates")

    project = _lock_enabled_project(
        db,
        workspace_id=workspace_id,
        collection_project_id=collection_project_id,
    )
    packages = _load_packages_for_update(
        db,
        workspace_id=workspace_id,
        package_uids=package_uids,
    )
    prior_statuses: dict[str, str] = {}
    for package in packages:
        if package.collection_project_id != project.id:
            raise LookupError("package_project_mismatch")
        if _has_active_session(db, data_package_id=package.id):
            raise PackageStateConflictError("package_session_busy")
        if (
            db.query(PackageIntakeReview.id)
            .filter(PackageIntakeReview.data_package_id == package.id)
            .first()
        ):
            raise PackageStateConflictError("package_intake_finalized")
        if package.status not in UPLOADABLE_PACKAGE_STATUSES:
            raise PackageStateConflictError("package_not_uploadable")
        prior_statuses[package.package_uid] = package.status

    upload_session = CollectionUploadSession(
        id=str(uuid4()),
        workspace_id=workspace_id,
        collection_project_id=project.id,
        status="init",
        upload_mode=upload_mode,
        created_by_user_id=actor_id,
        result_json={"package_prior_status": prior_statuses},
    )
    db.add(upload_session)
    db.flush()

    now = datetime.utcnow()
    for package in packages:
        updated_id = db.execute(
            update(DataPackage)
            .where(
                DataPackage.id == package.id,
                DataPackage.workspace_id == workspace_id,
                DataPackage.collection_project_id == project.id,
                DataPackage.status.in_(tuple(UPLOADABLE_PACKAGE_STATUSES)),
            )
            .values(status="pending_upload", updated_at=now)
            .returning(DataPackage.id)
        ).scalar_one_or_none()
        if updated_id is None:
            raise PackageStateConflictError("package_not_uploadable")
        db.add(
            CollectionUploadSessionPackage(
                upload_session_id=upload_session.id,
                data_package_id=package.id,
                package_uid=package.package_uid,
            )
        )
    db.flush()
    return get_upload_session(
        db,
        workspace_id=workspace_id,
        upload_session_id=upload_session.id,
    )


def get_upload_session(
    db: Session,
    *,
    workspace_id: int,
    upload_session_id: str,
) -> CollectionUploadSession:
    upload_session = (
        db.query(CollectionUploadSession)
        .options(
            selectinload(CollectionUploadSession.package_links).selectinload(
                CollectionUploadSessionPackage.package
            )
        )
        .filter(
            CollectionUploadSession.id == upload_session_id,
            CollectionUploadSession.workspace_id == workspace_id,
        )
        .one_or_none()
    )
    if upload_session is None:
        raise LookupError("upload session does not exist in this workspace")
    return upload_session


def list_upload_sessions(
    db: Session,
    *,
    workspace_id: int,
    collection_project_id: int | None = None,
    status: str | None = None,
) -> list[CollectionUploadSession]:
    if status is not None and status not in COLLECTION_UPLOAD_SESSION_STATUSES:
        raise ValueError(f"unsupported upload session status: {status}")
    query = db.query(CollectionUploadSession).options(
        selectinload(CollectionUploadSession.package_links).selectinload(
            CollectionUploadSessionPackage.package
        )
    )
    query = query.filter(CollectionUploadSession.workspace_id == workspace_id)
    if collection_project_id is not None:
        query = query.filter(CollectionUploadSession.collection_project_id == collection_project_id)
    if status is not None:
        query = query.filter(CollectionUploadSession.status == status)
    return query.order_by(
        CollectionUploadSession.created_at.desc(),
        CollectionUploadSession.id.desc(),
    ).all()


def cancel_upload_session(
    db: Session,
    *,
    workspace_id: int,
    upload_session_id: str,
) -> CollectionUploadSession:
    """Cancel a reserved upload and restore its packages for a fresh attempt."""
    upload_session = (
        db.query(CollectionUploadSession)
        .filter(
            CollectionUploadSession.id == upload_session_id,
            CollectionUploadSession.workspace_id == workspace_id,
        )
        .with_for_update()
        .one_or_none()
    )
    if upload_session is None:
        raise LookupError("upload session does not exist in this workspace")
    if upload_session.status in TERMINAL_SESSION_STATUSES:
        raise PackageStateConflictError("upload_session_terminal")
    if upload_session.status == "parsing":
        raise PackageStateConflictError("upload_session_parsing")

    # Cancel any provider multipart uploads recorded for this exact session
    # before releasing the package reservation.  Prefix cleanup is forbidden:
    # each declaration carries its own bucket/key/upload identity.
    result_json = upload_session.result_json or {}
    multipart_declarations: list[dict] = []
    one = result_json.get("oss_multipart")
    if isinstance(one, dict):
        multipart_declarations.append(one)
    many = result_json.get("oss_multipart_sources")
    if isinstance(many, dict):
        multipart_declarations.extend(item for item in many.values() if isinstance(item, dict))
    previews = result_json.get("oss_multipart_files")
    if isinstance(previews, dict):
        multipart_declarations.extend(item for item in previews.values() if isinstance(item, dict))
    if multipart_declarations:
        from data.infra import oss_client

        for declaration in multipart_declarations:
            upload_id = str(declaration.get("upload_id") or "").strip()
            if upload_id and declaration.get("completion_state") not in {"completed", "completing"}:
                oss_client.abort_browser_multipart_upload(
                    str(declaration.get("bucket") or oss_client.bucket_name("raw")),
                    str(declaration.get("object_key") or ""),
                    upload_id,
                )

    links = (
        db.query(CollectionUploadSessionPackage)
        .filter(CollectionUploadSessionPackage.upload_session_id == upload_session.id)
        .order_by(CollectionUploadSessionPackage.data_package_id)
        .with_for_update()
        .all()
    )
    package_ids = [link.data_package_id for link in links]
    packages = (
        db.query(DataPackage)
        .filter(DataPackage.id.in_(package_ids))
        .order_by(DataPackage.id)
        .with_for_update()
        .all()
        if package_ids
        else []
    )
    prior_statuses = dict((upload_session.result_json or {}).get("package_prior_status") or {})
    now = datetime.utcnow()
    for package in packages:
        prior_status = prior_statuses.get(package.package_uid, "assigned")
        if prior_status not in UPLOADABLE_PACKAGE_STATUSES:
            prior_status = "assigned"
        db.execute(
            update(DataPackage)
            .where(
                DataPackage.id == package.id,
                DataPackage.status.in_(("pending_upload", "uploading")),
            )
            .values(status=prior_status, upload_completed_at=None, updated_at=now)
        )
    upload_session.status = "cancelled"
    upload_session.updated_at = now
    db.flush()
    return get_upload_session(
        db,
        workspace_id=workspace_id,
        upload_session_id=upload_session.id,
    )


def mark_upload_session_uploaded(
    db: Session,
    *,
    workspace_id: int,
    upload_session_id: str,
) -> CollectionUploadSession:
    """Publish upload completion and move reserved packages into validation."""
    upload_session = (
        db.query(CollectionUploadSession)
        .filter(
            CollectionUploadSession.id == upload_session_id,
            CollectionUploadSession.workspace_id == workspace_id,
        )
        .with_for_update()
        .one_or_none()
    )
    if upload_session is None:
        raise LookupError("upload session does not exist in this workspace")
    if upload_session.status == "uploaded":
        return upload_session
    if upload_session.status != "uploading":
        raise PackageStateConflictError("upload_session_not_uploading")

    links = (
        db.query(CollectionUploadSessionPackage)
        .filter(CollectionUploadSessionPackage.upload_session_id == upload_session.id)
        .order_by(CollectionUploadSessionPackage.data_package_id)
        .with_for_update()
        .all()
    )
    package_ids = [link.data_package_id for link in links]
    packages = (
        db.query(DataPackage)
        .filter(DataPackage.id.in_(package_ids))
        .order_by(DataPackage.id)
        .with_for_update()
        .all()
    )
    if len(packages) != len(package_ids) or any(
        package.status != "pending_upload" for package in packages
    ):
        raise PackageStateConflictError("package_not_pending_upload")

    now = datetime.utcnow()
    for package in packages:
        package.status = "uploading"
        package.upload_completed_at = now
        package.updated_at = now
    upload_session.status = "uploaded"
    upload_session.updated_at = now
    db.flush()
    return upload_session
