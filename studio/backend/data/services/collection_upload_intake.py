"""Validated byte intake for collection upload sessions."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from filelock import FileLock
from sqlalchemy import select
from sqlalchemy.orm import Session

from data.config import settings
from data.models.collection_upload import (
    CollectionUploadSession,
    CollectionUploadSessionPackage,
)
from data.services.client_admission import (
    ClientAdmissionDeclarationError,
    normalize_client_admission,
    preview_file_id,
    preview_object_key,
)
from data.services.collection_packages import PackageStateConflictError
from data.services.collection_upload_parse import ensure_collection_upload_parse_job
from data.services.collection_upload_sessions import mark_upload_session_uploaded
from data.services.duance_imports import build_duance_import_manifest
from data.services.import_intake import (
    MAX_IMPORT_CHUNK_BYTES,
    MAX_IMPORT_CHUNKS,
    MAX_IMPORT_TOTAL_BYTES,
)
from data.utils.storage_paths import is_under_storage_root, storage_root_path


def collection_upload_staging_dir(upload_session_id: str) -> Path:
    """Resolve one server-generated session directory below storage_root."""
    try:
        session_id = str(UUID(upload_session_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("invalid collection upload session id") from exc
    root = storage_root_path()
    parent = (root / "collection-uploads").resolve()
    staging = (parent / session_id).resolve()
    if staging.parent != parent or not is_under_storage_root(staging, root=root):
        raise ValueError("collection upload staging path is outside storage root")
    return staging


def _locked_session(
    db: Session,
    *,
    workspace_id: int,
    upload_session_id: str,
) -> CollectionUploadSession:
    upload_session = db.scalar(
        select(CollectionUploadSession)
        .where(
            CollectionUploadSession.id == upload_session_id,
            CollectionUploadSession.workspace_id == workspace_id,
        )
        .with_for_update()
    )
    if upload_session is None:
        raise LookupError("upload session does not exist in this workspace")
    if upload_session.status in {"succeeded", "failed", "cancelled"}:
        raise PackageStateConflictError("upload_session_terminal")
    return upload_session


def _session_package_uids(db: Session, upload_session_id: str) -> set[str]:
    return set(
        db.scalars(
            select(CollectionUploadSessionPackage.package_uid).where(
                CollectionUploadSessionPackage.upload_session_id == upload_session_id
            )
        ).all()
    )


def _require_declarations(
    db: Session, upload_session: CollectionUploadSession
) -> dict[str, object]:
    result = dict(upload_session.result_json or {})
    declarations = result.get("declarations")
    if not isinstance(declarations, dict):
        raise ValueError("package declarations are required")
    if set(declarations) != _session_package_uids(db, upload_session.id):
        raise ValueError("every session package must have a declaration")
    return result


def _declared_sources(result: dict) -> dict[str, dict]:
    """Opaque source IDs bind byte uploads to a server-validated declaration."""
    return {
        hashlib.sha256(f"{package_uid}:{source['source_key']}".encode()).hexdigest(): {
            **source,
            "package_uid": package_uid,
        }
        for package_uid, sources in result["declarations"].items()
        for source in sources
    }


def _chunk_target(result: dict, source_id: str | None) -> tuple[str, dict | None]:
    sources = _declared_sources(result)
    if source_id is None:
        if len(sources) != 1:
            raise ValueError("multi-source uploads require a source_id for each file")
        return "chunked", None
    if source_id not in sources:
        raise ValueError("source_id does not belong to this upload session")
    return f"chunked:{source_id}", sources[source_id]


def declare_package_sources(
    db: Session,
    *,
    workspace_id: int,
    upload_session_id: str,
    declarations: list[dict[str, object]],
) -> dict[str, object]:
    """Bind validated Duance episode manifests to session-owned packages."""
    if not declarations:
        raise ValueError("declaration items must not be empty")
    upload_session = _locked_session(
        db, workspace_id=workspace_id, upload_session_id=upload_session_id
    )
    if upload_session.status not in {"init", "uploading"}:
        raise PackageStateConflictError("upload_session_not_declarable")
    package_uids = _session_package_uids(db, upload_session.id)
    stored: dict[str, list[dict[str, object]]] = {}
    staged_paths: set[str] = set()
    total_bytes = 0
    for declaration in declarations:
        package_uid = declaration.get("package_uid")
        if not isinstance(package_uid, str) or package_uid not in package_uids:
            raise PackageStateConflictError("package_not_in_session")
        manifest = build_duance_import_manifest(
            source=declaration.get("source", {}),
            metadata_text=declaration.get("metadata_text", ""),
            data_file=declaration.get("data_file", {}),
        )
        stored_manifest = manifest.to_storage()
        source_id = hashlib.sha256(f"{package_uid}:{manifest.source_key}".encode()).hexdigest()
        stored_manifest["staging_layout_version"] = 2
        stored_manifest["staging_path"] = f"sources/{package_uid}/{source_id}/{manifest.data_path}"
        # The client digest is an integrity hint only.  Object identity is
        # generated by the service and is never derived from client supplied
        # hashes or source IDs.
        stored_manifest["raw_object_key"] = (
            f"raw/v2/workspaces/{workspace_id}/collection-uploads/"
            f"{upload_session.id}/{uuid4().hex}/upload.bin"
        )
        if stored_manifest["staging_path"] in staged_paths:
            raise ValueError("duplicate data file path in package declarations")
        staged_paths.add(stored_manifest["staging_path"])
        total_bytes += manifest.data_size_bytes
        client_admission = declaration.get("client_admission")
        if client_admission is not None:
            if upload_session.upload_mode != "oss_multipart":
                raise ClientAdmissionDeclarationError("client_admission_requires_oss_multipart")
            normalized = normalize_client_admission(client_admission)
            for item in normalized["files"]:
                item["file_id"] = preview_file_id(source_id, item["path"])
                item["object_key"] = preview_object_key(
                    workspace_id=workspace_id,
                    upload_session_id=upload_session.id,
                    path=item["path"],
                )
                total_bytes += item["size_bytes"]
            stored_manifest["client_admission"] = normalized
        if total_bytes > MAX_IMPORT_TOTAL_BYTES:
            raise ValueError("collection upload exceeds the total size limit")
        stored.setdefault(package_uid, []).append(stored_manifest)
    if set(stored) != package_uids:
        raise ValueError("every session package must have a declaration")
    if len(_declared_sources({"declarations": stored})) != sum(
        len(items) for items in stored.values()
    ):
        raise ValueError("duplicate source identity in package declarations")
    result = dict(upload_session.result_json or {})
    existing = result.get("declarations")
    if isinstance(existing, dict):
        # Replays keep the service generated object identities.  Compare the
        # declaration payload while ignoring those identities, which are
        # intentionally not client controlled.
        def _without_object_key(value: object) -> object:
            if isinstance(value, dict):
                return {
                    key: _without_object_key(item)
                    for key, item in value.items()
                    if key
                    not in {
                        "raw_object_key",
                        "staging_path",
                        "staging_layout_version",
                        "object_key",
                    }
                }
            if isinstance(value, list):
                return [_without_object_key(item) for item in value]
            return value

        if _without_object_key(existing) != _without_object_key(stored):
            raise PackageStateConflictError("package_declarations_conflict")
        for package_uid, items in existing.items():
            for item, old in zip(stored.get(package_uid, []), items, strict=False):
                # A replay of an older session must keep its existing byte layout.
                for key in ("raw_object_key", "staging_path", "staging_layout_version"):
                    if key in old:
                        item[key] = old[key]
                    else:
                        item.pop(key, None)
                _keep_preview_object_keys(item, old)
    result["declarations"] = stored
    upload_session.result_json = result
    upload_session.updated_at = datetime.utcnow()
    db.flush()
    return {
        "id": upload_session.id,
        "status": upload_session.status,
        "declared_packages": len(stored),
        "declared_sources": sum(len(items) for items in stored.values()),
        "sources": [
            _declared_source_item(source_id, source)
            for source_id, source in _declared_sources(result).items()
        ],
    }


def _keep_preview_object_keys(item: dict, old: dict) -> None:
    """A replayed declaration keeps the preview object keys generated first."""
    old_keys = {
        entry["path"]: entry["object_key"]
        for entry in (old.get("client_admission") or {}).get("files", [])
        if entry.get("object_key")
    }
    for entry in (item.get("client_admission") or {}).get("files", []):
        if entry["path"] in old_keys:
            entry["object_key"] = old_keys[entry["path"]]


def _declared_source_item(source_id: str, source: dict) -> dict[str, object]:
    item: dict[str, object] = {
        "source_id": source_id,
        "package_uid": source["package_uid"],
        "episode_id": source["source"]["episode_id"],
    }
    declared = source.get("client_admission")
    if isinstance(declared, dict):
        item["preview_files"] = [
            {"file_id": entry["file_id"], "path": entry["path"], "size_bytes": entry["size_bytes"]}
            for entry in declared["files"]
        ]
    return item


def start_chunked_upload(
    db: Session,
    *,
    workspace_id: int,
    upload_session_id: str,
    total_chunks: int,
    source_id: str | None = None,
) -> dict[str, object]:
    if (
        isinstance(total_chunks, bool)
        or not isinstance(total_chunks, int)
        or not 1 <= total_chunks <= MAX_IMPORT_CHUNKS
    ):
        raise ValueError("invalid collection upload chunk count")
    upload_session = _locked_session(
        db, workspace_id=workspace_id, upload_session_id=upload_session_id
    )
    if upload_session.upload_mode not in {"chunked", "duance_sdk"}:
        raise PackageStateConflictError("upload_mode_mismatch")
    if upload_session.status not in {"init", "uploading"}:
        raise PackageStateConflictError("upload_session_not_uploading")
    result = _require_declarations(db, upload_session)
    target_key, source = _chunk_target(result, source_id)
    if (source is None and any(key.startswith("chunked:") for key in result)) or (
        source is not None and "chunked" in result
    ):
        raise PackageStateConflictError("chunk_upload_layout_conflict")
    current = result.get(target_key)
    declaration = {
        "total_chunks": total_chunks,
        "uploaded_chunks": [],
        "uploaded_bytes": 0,
        "chunk_sizes": {},
    }
    if isinstance(current, dict):
        if current.get("total_chunks") != total_chunks:
            raise PackageStateConflictError("chunk_declaration_conflict")
        declaration = current
    result[target_key] = declaration
    upload_session.result_json = result
    upload_session.status = "uploading"
    upload_session.updated_at = datetime.utcnow()
    db.flush()
    return _chunk_progress(upload_session, declaration)


def write_chunk(
    db: Session,
    *,
    workspace_id: int,
    upload_session_id: str,
    chunk_index: int,
    content: bytes,
    source_id: str | None = None,
) -> dict[str, object]:
    if isinstance(chunk_index, bool) or not isinstance(chunk_index, int) or chunk_index < 0:
        raise ValueError("invalid collection upload chunk index")
    if not isinstance(content, bytes) or not content:
        raise ValueError("invalid collection upload chunk content")
    if len(content) > MAX_IMPORT_CHUNK_BYTES:
        raise ValueError("collection upload chunk exceeds the chunk size limit")
    upload_session = _locked_session(
        db, workspace_id=workspace_id, upload_session_id=upload_session_id
    )
    if upload_session.status != "uploading" or upload_session.upload_mode not in {
        "chunked",
        "duance_sdk",
    }:
        raise PackageStateConflictError("upload_session_not_uploading")
    result = _require_declarations(db, upload_session)
    target_key, source = _chunk_target(result, source_id)
    declaration = dict(result.get(target_key) or {})
    total_chunks = int(declaration.get("total_chunks") or 0)
    if chunk_index >= total_chunks:
        raise ValueError("collection upload chunk index is outside declared range")

    staging = collection_upload_staging_dir(upload_session.id)
    if source is not None:
        staging = _safe_child(staging, f"file-{source_id}")
    chunks_dir = _safe_child(staging, "chunks")
    chunks_dir.mkdir(parents=True, exist_ok=True)
    chunk_path = _safe_child(chunks_dir, f"{chunk_index:08d}.part")
    lock = FileLock(str(_safe_child(staging, ".upload.lock")))
    with lock:
        uploaded = sorted({int(index) for index in declaration.get("uploaded_chunks") or []})
        sizes = {
            str(key): int(value)
            for key, value in dict(declaration.get("chunk_sizes") or {}).items()
        }
        previous_size = sizes.get(str(chunk_index), 0)
        total_bytes = int(declaration.get("uploaded_bytes") or 0) - previous_size + len(content)
        if total_bytes > MAX_IMPORT_TOTAL_BYTES:
            raise ValueError("collection upload exceeds the total size limit")
        expected_bytes = (
            source["data_file"]["size_bytes"]
            if source is not None
            else next(iter(_declared_sources(result).values()))["data_file"]["size_bytes"]
        )
        if total_bytes > expected_bytes:
            raise ValueError("collection upload exceeds the declared file size")
        temp_path = _safe_child(chunks_dir, f".{chunk_index:08d}.{uuid4().hex}.tmp")
        try:
            with temp_path.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, chunk_path)
        finally:
            temp_path.unlink(missing_ok=True)
        if chunk_index not in uploaded:
            uploaded.append(chunk_index)
        sizes[str(chunk_index)] = len(content)
        declaration.update(
            {
                "uploaded_chunks": sorted(uploaded),
                "uploaded_bytes": total_bytes,
                "chunk_sizes": sizes,
            }
        )
        result[target_key] = declaration
        upload_session.result_json = result
        upload_session.updated_at = datetime.utcnow()
        db.flush()
    return _chunk_progress(upload_session, declaration)


def complete_chunked_upload(
    db: Session,
    *,
    workspace_id: int,
    upload_session_id: str,
) -> CollectionUploadSession:
    upload_session = _locked_session(
        db, workspace_id=workspace_id, upload_session_id=upload_session_id
    )
    if upload_session.status == "uploaded":
        return upload_session
    if upload_session.upload_mode not in {"chunked", "duance_sdk"}:
        raise PackageStateConflictError("upload_mode_mismatch")
    if upload_session.status != "uploading":
        raise PackageStateConflictError("upload_session_not_uploading")
    result = _require_declarations(db, upload_session)
    sources = _declared_sources(result)
    staging = collection_upload_staging_dir(upload_session.id)
    if "chunked" in result:
        _chunk_target(result, None)
        targets = [
            (
                result["chunked"],
                staging,
                _safe_child(staging, "upload.bin"),
                next(iter(sources.values())),
            )
        ]
    else:
        targets = [
            (
                result.get(f"chunked:{source_id}") or {},
                _safe_child(staging, f"file-{source_id}"),
                _safe_relative(staging, source["staging_path"]),
                source,
            )
            for source_id, source in sources.items()
        ]
    for declaration, chunk_root, destination, source in targets:
        _assemble_source(declaration, chunk_root, destination, source)
    upload_session = mark_upload_session_uploaded(
        db,
        workspace_id=workspace_id,
        upload_session_id=upload_session.id,
    )
    ensure_collection_upload_parse_job(db, upload_session)
    return upload_session


def _assemble_source(declaration: dict, staging: Path, destination: Path, source: dict) -> None:
    total_chunks = int(declaration.get("total_chunks") or 0)
    uploaded = sorted({int(index) for index in declaration.get("uploaded_chunks") or []})
    if total_chunks <= 0 or uploaded != list(range(total_chunks)):
        raise ValueError("collection upload is incomplete")
    chunks_dir = _safe_child(staging, "chunks")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _safe_child(staging, f".assembled-{uuid4().hex}.tmp")
    expected_size = int(source["data_file"]["size_bytes"])
    with FileLock(str(_safe_child(staging, ".upload.lock"))):
        try:
            written = 0
            digest = hashlib.sha256()
            with temporary.open("xb") as output:
                for chunk_index in uploaded:
                    chunk_path = _safe_child(chunks_dir, f"{chunk_index:08d}.part")
                    if not chunk_path.is_file() or chunk_path.is_symlink():
                        raise ValueError("collection upload is incomplete")
                    with chunk_path.open("rb") as stream:
                        while block := stream.read(1024 * 1024):
                            written += len(block)
                            if written > expected_size or written > MAX_IMPORT_TOTAL_BYTES:
                                raise ValueError("collection upload exceeds the declared file size")
                            digest.update(block)
                            output.write(block)
                output.flush()
                os.fsync(output.fileno())
            if written != expected_size or digest.hexdigest() != source["data_file"]["sha256"]:
                raise ValueError("uploaded_data_checksum_mismatch")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def start_oss_multipart(
    db: Session,
    *,
    workspace_id: int,
    upload_session_id: str,
    total_size_bytes: int,
    content_type: str | None,
    source_id: str | None = None,
) -> dict[str, object]:
    from data.infra import oss_client

    if (
        isinstance(total_size_bytes, bool)
        or not isinstance(total_size_bytes, int)
        or not 1 <= total_size_bytes <= MAX_IMPORT_TOTAL_BYTES
    ):
        raise ValueError("collection upload size is invalid")
    upload_session = _locked_session(
        db, workspace_id=workspace_id, upload_session_id=upload_session_id
    )
    if upload_session.upload_mode != "oss_multipart":
        raise PackageStateConflictError("upload_mode_mismatch")
    if upload_session.status not in {"init", "uploading"}:
        raise PackageStateConflictError("upload_session_not_uploading")
    result = _require_declarations(db, upload_session)
    sources = _declared_sources(result)
    selected_source_id = _selected_multipart_source_id(sources, source_id)
    source = sources[selected_source_id]
    if total_size_bytes != source["data_file"]["size_bytes"]:
        raise ValueError("collection upload size differs from the declared file size")
    part_size = settings.import_direct_upload_part_bytes
    total_parts = (total_size_bytes + part_size - 1) // part_size
    if not 1 <= total_parts <= 10_000:
        raise ValueError("collection upload exceeds the multipart part limit")
    declaration, declaration_key = _load_multipart_declaration(result, selected_source_id, source)

    def _save(result: dict[str, object], decl: dict[str, object]) -> None:
        _store_multipart_declaration(result, declaration_key, decl)

    if declaration is not None:
        if (
            declaration.get("total_size_bytes") != total_size_bytes
            or declaration.get("content_type") != content_type
        ):
            raise PackageStateConflictError("oss_declaration_conflict")
    else:
        declaration = {
            "bucket": oss_client.bucket_name("raw"),
            "source_id": selected_source_id,
            "package_uid": source["package_uid"],
            "data_path": source["data_file"]["path"],
            "object_key": str(
                source.get("raw_object_key")
                or (
                    f"raw/v2/workspaces/{workspace_id}/collection-uploads/"
                    f"{upload_session.id}/{uuid4().hex}/upload.bin"
                )
            ),
            "upload_id": "",
            "total_size_bytes": total_size_bytes,
            "content_type": content_type,
            "part_size": part_size,
            "total_parts": total_parts,
        }
        upload_session, result = _persist_new_declaration_and_relock(
            db,
            workspace_id=workspace_id,
            upload_session_id=upload_session_id,
            upload_session=upload_session,
            result=result,
            declaration=declaration,
            save=_save,
        )
        declaration, _ = _load_multipart_declaration(result, selected_source_id, source)
        if declaration is None:
            raise ValueError("multipart upload declaration is unavailable")
    declaration = _ensure_multipart_upload_id(db, upload_session, result, declaration, save=_save)
    return _oss_progress(upload_session, declaration)


def sign_part(
    db: Session,
    *,
    workspace_id: int,
    upload_session_id: str,
    part_number: int,
    source_id: str | None = None,
    content_md5: str | None = None,
) -> dict[str, object]:
    from data.infra import oss_client

    upload_session = _locked_session(
        db, workspace_id=workspace_id, upload_session_id=upload_session_id
    )
    if upload_session.upload_mode != "oss_multipart" or upload_session.status != "uploading":
        raise PackageStateConflictError("upload_session_not_uploading")
    result = dict(upload_session.result_json or {})
    sources = _declared_sources(result)
    selected_source_id = _selected_multipart_source_id(sources, source_id)
    if not content_md5 and isinstance(sources[selected_source_id].get("client_admission"), dict):
        raise ValueError("content_md5_required")
    declaration, _declaration_key = _load_multipart_declaration(
        result, selected_source_id, sources[selected_source_id]
    )
    if declaration is None:
        raise ValueError("multipart upload is not initialized")
    total_parts = int(declaration.get("total_parts") or 0)
    if not declaration.get("upload_id"):
        raise ValueError("multipart upload is not initialized")
    if isinstance(part_number, bool) or not 1 <= part_number <= total_parts:
        raise ValueError("multipart part number is invalid")
    expected_size = _expected_part_size(declaration, part_number)
    sign_args: tuple[object, ...] = (
        str(declaration["bucket"]),
        str(declaration["object_key"]),
        str(declaration["upload_id"]),
        part_number,
    )
    if content_md5:
        sign_args = (*sign_args, content_md5)
    url, ttl, headers = oss_client.sign_browser_upload_part(*sign_args)
    return {
        "id": upload_session.id,
        "status": upload_session.status,
        "part_number": part_number,
        "content_length": expected_size,
        "method": "PUT",
        "url": url,
        "expires_in": ttl,
        "headers": headers,
    }


def complete_oss_multipart(
    db: Session,
    *,
    workspace_id: int,
    upload_session_id: str,
    parts: list[dict[str, object]],
    source_id: str | None = None,
) -> CollectionUploadSession:
    upload_session = _locked_session(
        db, workspace_id=workspace_id, upload_session_id=upload_session_id
    )
    if upload_session.status == "uploaded":
        return upload_session
    if upload_session.upload_mode != "oss_multipart" or upload_session.status != "uploading":
        raise PackageStateConflictError("upload_session_not_uploading")
    result = dict(upload_session.result_json or {})
    sources = _declared_sources(result)
    selected_source_id = _selected_multipart_source_id(sources, source_id)
    declaration, declaration_key = _load_multipart_declaration(
        result, selected_source_id, sources[selected_source_id]
    )
    if declaration is None:
        raise ValueError("multipart upload is not initialized")
    if not declaration.get("upload_id"):
        raise ValueError("multipart upload is not initialized")

    def _load(result: dict[str, object]) -> dict[str, object] | None:
        found, _ = _load_multipart_declaration(
            result, selected_source_id, sources[selected_source_id]
        )
        return found

    def _save(result: dict[str, object], decl: dict[str, object]) -> None:
        _store_multipart_declaration(result, declaration_key, decl)

    upload_session, result = _complete_declared_multipart(
        db,
        workspace_id=workspace_id,
        upload_session_id=upload_session_id,
        upload_session=upload_session,
        result=result,
        declaration=declaration,
        parts=parts,
        load=_load,
        save=_save,
        bucket_role="raw",
        require_crc64=True,
    )
    if _all_uploads_completed(result, sources):
        upload_session = mark_upload_session_uploaded(
            db,
            workspace_id=workspace_id,
            upload_session_id=upload_session.id,
        )
        ensure_collection_upload_parse_job(db, upload_session)
    return upload_session


def _selected_multipart_source_id(sources: dict[str, dict], source_id: str | None) -> str:
    if source_id is not None:
        if source_id not in sources:
            raise ValueError("source_id does not belong to this upload session")
        return source_id
    if len(sources) != 1:
        raise ValueError("multi-source uploads require a source_id for each file")
    return next(iter(sources))


def _load_multipart_declaration(
    result: dict[str, object], source_id: str, source: dict
) -> tuple[dict[str, object] | None, str]:
    declarations = result.get("oss_multipart_sources")
    if isinstance(declarations, dict):
        declaration = declarations.get(source_id)
        return (
            dict(declaration) if isinstance(declaration, dict) else None,
            f"oss_multipart_sources:{source_id}",
        )
    if len(_declared_sources(result)) == 1:
        declaration = result.get("oss_multipart")
        return (
            dict(declaration) if isinstance(declaration, dict) else None,
            "oss_multipart",
        )
    return None, f"oss_multipart_sources:{source_id}"


def _store_multipart_declaration(
    result: dict[str, object], declaration_key: str, declaration: dict[str, object]
) -> None:
    if declaration_key == "oss_multipart":
        result[declaration_key] = declaration
        return
    source_id = declaration_key.split(":", 1)[1]
    declarations = dict(result.get("oss_multipart_sources") or {})
    declarations[source_id] = declaration
    result["oss_multipart_sources"] = declarations


def _persist_new_declaration_and_relock(
    db: Session,
    *,
    workspace_id: int,
    upload_session_id: str,
    upload_session: CollectionUploadSession,
    result: dict[str, object],
    declaration: dict[str, object],
    save: Callable[[dict[str, object], dict[str, object]], None],
) -> tuple[CollectionUploadSession, dict[str, object]]:
    """Persist a brand-new multipart declaration, commit it, then re-lock.

    Shared by the MCAP and preview ``start_*`` paths: committing before the
    provider creates an upload ID means a crashed request leaves a durable
    declaration a retry can find, instead of orphaning a provider upload.
    """
    save(result, declaration)
    upload_session.result_json = result
    upload_session.status = "uploading"
    upload_session.updated_at = datetime.utcnow()
    db.flush()
    # Persist the declaration before the provider creates an upload ID.
    db.commit()
    upload_session = _locked_session(
        db, workspace_id=workspace_id, upload_session_id=upload_session_id
    )
    result = dict(upload_session.result_json or {})
    return upload_session, result


def _ensure_multipart_upload_id(
    db: Session,
    upload_session: CollectionUploadSession,
    result: dict[str, object],
    declaration: dict[str, object],
    *,
    save: Callable[[dict[str, object], dict[str, object]], None],
) -> dict[str, object]:
    """Lazily create the provider upload ID exactly once for a declaration.

    Shared by the MCAP and preview ``start_*`` paths, so a repeated ``init``
    call with the same identity is resumable and never opens a second
    provider multipart upload for the same object.
    """
    from data.infra import oss_client

    if not declaration.get("upload_id"):
        declaration = dict(declaration)
        declaration["upload_id"] = oss_client.init_browser_multipart_upload(
            str(declaration["bucket"]), str(declaration["object_key"])
        )
        save(result, declaration)
        upload_session.result_json = result
        db.flush()
    return declaration


def _complete_declared_multipart(
    db: Session,
    *,
    workspace_id: int,
    upload_session_id: str,
    upload_session: CollectionUploadSession,
    result: dict[str, object],
    declaration: dict[str, object],
    parts: list[dict[str, object]],
    load: Callable[[dict[str, object]], dict[str, object] | None],
    save: Callable[[dict[str, object], dict[str, object]], None],
    bucket_role: str,
    require_crc64: bool,
) -> tuple[CollectionUploadSession, dict[str, object]]:
    """Verify and persist one provider-confirmed multipart completion.

    Shared by the MCAP (raw bucket) and preview (process bucket) paths: both
    normalize the client manifest, HEAD the object first so a replay is
    idempotent, else list the provider's part manifest and check it against
    the client manifest and each part's expected size, persist a
    "completing" intent before consuming the upload ID (a lost
    CompleteMultipartUpload response is ambiguous; only a later provider HEAD
    proving the final object lets the transition continue), then re-lock to
    persist the final provider identity.  Only the identity's ``bucket_role``
    and whether CRC64 is mandatory differ between the two callers.
    """
    from data.infra import oss_client

    total_parts = int(declaration.get("total_parts") or 0)
    client_manifest = _normalize_client_parts(parts, total_parts=total_parts)
    bucket = str(declaration["bucket"])
    object_key = str(declaration["object_key"])
    upload_id = str(declaration["upload_id"])
    stored_parts = (
        _normalize_client_parts(
            list(declaration.get("completion_parts") or []),
            total_parts=total_parts,
        )
        if declaration.get("completion_parts")
        else []
    )
    info = oss_client.object_info(bucket, object_key)
    if info is None:
        provider_parts = oss_client.list_browser_multipart_parts(bucket, object_key, upload_id)
        if len(provider_parts) != total_parts:
            raise ValueError("multipart upload is incomplete")
        provider_manifest = [
            {"part_number": part.number, "etag": str(part.etag).strip('"')}
            for part in provider_parts
        ]
        if client_manifest != provider_manifest:
            raise ValueError("multipart part manifest does not match the provider")
        if any(
            part.size != _expected_part_size(declaration, part.number) for part in provider_parts
        ):
            raise ValueError("multipart part size does not match its declaration")
        declaration = dict(declaration)
        declaration["completion_parts"] = provider_manifest
        declaration["completion_state"] = "completing"
        save(result, declaration)
        upload_session.result_json = result
        db.flush()
        # Persist the provider-verified intent before the upload ID is consumed.
        db.commit()
        try:
            oss_client.complete_browser_multipart_upload(
                bucket, object_key, upload_id, provider_parts
            )
        except Exception:
            # A lost CompleteMultipartUpload response is ambiguous. Only a
            # provider HEAD proving the final object lets the transition continue.
            info = oss_client.object_info(bucket, object_key)
            if info is None:
                raise
        else:
            info = oss_client.object_info(bucket, object_key)
    elif not stored_parts or client_manifest != stored_parts:
        raise ValueError("multipart part manifest does not match the persisted completion intent")
    if info is None or int(info.size) != int(declaration["total_size_bytes"]):
        raise ValueError("completed multipart object size does not match its declaration")
    crc64: str | None = None
    if require_crc64:
        crc64 = str(info.crc64 or "")
        # S3-compatible MinIO does not expose Aliyun's CRC64 header.  The
        # provider HEAD still proves immutable size/ETag here; the worker computes
        # the authoritative SHA-256 before admission.  Keep CRC64 mandatory for
        # Aliyun, where it is part of that provider's completion contract.
        provider_name = str(getattr(settings, "storage_provider", "") or "").lower()
        if provider_name not in {"minio", "s3", "s3-compatible"} and (
            not crc64.isdigit() or len(crc64) > 32
        ):
            raise ValueError("completed multipart object is missing CRC64 integrity")
    upload_session = _locked_session(
        db, workspace_id=workspace_id, upload_session_id=upload_session_id
    )
    result = dict(upload_session.result_json or {})
    current = load(result)
    if current is None:
        raise ValueError("multipart upload declaration is unavailable")
    current = dict(current)
    current["completion_state"] = "completed"
    if crc64 is not None:
        current["crc64"] = crc64
    # Persist only provider HEAD facts.  Any client supplied SHA-256 in the
    # declaration remains an integrity hint and never becomes object identity.
    # Server-mode admission records the SHA-256 it measures after downloading
    # the pinned identity; client-precheck admission records the declared
    # SHA-256 only after HEAD re-confirms this identity (and falls back to
    # server mode if the provider reports a different digest).
    current["provider_identity"] = {
        "bucket_role": bucket_role,
        "object_key": object_key,
        "version_id": info.version_id,
        "etag": str(info.etag).strip('"'),
        "size_bytes": int(info.size),
        "sha256": None,
    }
    save(result, current)
    upload_session.result_json = result
    return upload_session, result


def _all_multipart_sources_completed(result: dict[str, object], sources: dict[str, dict]) -> bool:
    declarations = result.get("oss_multipart_sources")
    if isinstance(declarations, dict):
        return len(declarations) == len(sources) and all(
            isinstance(item, dict) and item.get("completion_state") == "completed"
            for item in declarations.values()
        )
    return dict(result.get("oss_multipart") or {}).get("completion_state") == "completed"


def _declared_preview_files(result: dict) -> dict[str, dict]:
    """Every declared preview file keyed by its opaque file_id."""
    files: dict[str, dict] = {}
    for source_id, source in _declared_sources(result).items():
        for entry in (source.get("client_admission") or {}).get("files") or []:
            files[entry["file_id"]] = {
                **entry,
                "source_id": source_id,
                "package_uid": source["package_uid"],
            }
    return files


def _preview_target(result: dict, file_id: str) -> dict:
    files = _declared_preview_files(result)
    if file_id not in files:
        raise ValueError("file_id does not belong to this upload session")
    return files[file_id]


def _all_uploads_completed(result: dict[str, object], sources: dict[str, dict]) -> bool:
    """A session is uploaded only when every data file and every preview file completed."""
    if not _all_multipart_sources_completed(result, sources):
        return False
    uploads = result.get("oss_multipart_files") or {}
    return all(
        isinstance(uploads.get(file_id), dict)
        and uploads[file_id].get("completion_state") == "completed"
        for file_id in _declared_preview_files(result)
    )


def start_preview_multipart(
    db: Session,
    *,
    workspace_id: int,
    upload_session_id: str,
    file_id: str,
    total_size_bytes: int,
    content_type: str | None,
) -> dict[str, object]:
    from data.infra import oss_client

    # Same total-size guard as the MCAP path (start_oss_multipart), applied
    # before the declared size comparison below for defense in depth.
    if (
        isinstance(total_size_bytes, bool)
        or not isinstance(total_size_bytes, int)
        or not 1 <= total_size_bytes <= MAX_IMPORT_TOTAL_BYTES
    ):
        raise ValueError("collection upload size is invalid")
    upload_session = _locked_session(
        db, workspace_id=workspace_id, upload_session_id=upload_session_id
    )
    if upload_session.upload_mode != "oss_multipart":
        raise PackageStateConflictError("upload_mode_mismatch")
    if upload_session.status not in {"init", "uploading"}:
        raise PackageStateConflictError("upload_session_not_uploading")
    result = _require_declarations(db, upload_session)
    target = _preview_target(result, file_id)
    if total_size_bytes != target["size_bytes"]:
        raise ValueError("collection upload size differs from the declared file size")
    part_size = settings.import_direct_upload_part_bytes
    total_parts = (total_size_bytes + part_size - 1) // part_size
    if not 1 <= total_parts <= 10_000:
        raise ValueError("collection upload exceeds the multipart part limit")
    uploads = dict(result.get("oss_multipart_files") or {})
    declaration = uploads.get(file_id)

    def _save(result: dict[str, object], decl: dict[str, object]) -> None:
        uploads = dict(result.get("oss_multipart_files") or {})
        uploads[file_id] = decl
        result["oss_multipart_files"] = uploads

    if isinstance(declaration, dict):
        if (
            declaration.get("total_size_bytes") != total_size_bytes
            or declaration.get("content_type") != content_type
        ):
            raise PackageStateConflictError("oss_declaration_conflict")
        declaration = dict(declaration)
    else:
        declaration = {
            "bucket": oss_client.bucket_name("process"),
            "file_id": file_id,
            "source_id": target["source_id"],
            "package_uid": target["package_uid"],
            "path": target["path"],
            "object_key": target["object_key"],
            "upload_id": "",
            "total_size_bytes": total_size_bytes,
            "content_type": content_type,
            "part_size": part_size,
            "total_parts": total_parts,
        }
        upload_session, result = _persist_new_declaration_and_relock(
            db,
            workspace_id=workspace_id,
            upload_session_id=upload_session_id,
            upload_session=upload_session,
            result=result,
            declaration=declaration,
            save=_save,
        )
        uploads = dict(result.get("oss_multipart_files") or {})
        declaration = dict(uploads[file_id])
    declaration = _ensure_multipart_upload_id(db, upload_session, result, declaration, save=_save)
    return _oss_progress(upload_session, declaration)


def sign_preview_part(
    db: Session,
    *,
    workspace_id: int,
    upload_session_id: str,
    file_id: str,
    part_number: int,
    content_md5: str | None,
) -> dict[str, object]:
    from data.infra import oss_client

    if not content_md5:
        raise ValueError("content_md5_required")
    upload_session = _locked_session(
        db, workspace_id=workspace_id, upload_session_id=upload_session_id
    )
    if upload_session.upload_mode != "oss_multipart" or upload_session.status != "uploading":
        raise PackageStateConflictError("upload_session_not_uploading")
    result = dict(upload_session.result_json or {})
    _preview_target(result, file_id)
    declaration = (result.get("oss_multipart_files") or {}).get(file_id)
    if not isinstance(declaration, dict) or not declaration.get("upload_id"):
        raise ValueError("multipart upload is not initialized")
    total_parts = int(declaration.get("total_parts") or 0)
    if isinstance(part_number, bool) or not 1 <= part_number <= total_parts:
        raise ValueError("multipart part number is invalid")
    url, ttl, headers = oss_client.sign_browser_upload_part(
        str(declaration["bucket"]),
        str(declaration["object_key"]),
        str(declaration["upload_id"]),
        part_number,
        content_md5,
    )
    return {
        "id": upload_session.id,
        "status": upload_session.status,
        "part_number": part_number,
        "content_length": _expected_part_size(declaration, part_number),
        "method": "PUT",
        "url": url,
        "expires_in": ttl,
        "headers": headers,
    }


def complete_preview_multipart(
    db: Session,
    *,
    workspace_id: int,
    upload_session_id: str,
    file_id: str,
    parts: list[dict[str, object]],
) -> CollectionUploadSession:
    upload_session = _locked_session(
        db, workspace_id=workspace_id, upload_session_id=upload_session_id
    )
    if upload_session.status == "uploaded":
        return upload_session
    if upload_session.upload_mode != "oss_multipart" or upload_session.status != "uploading":
        raise PackageStateConflictError("upload_session_not_uploading")
    result = dict(upload_session.result_json or {})
    _preview_target(result, file_id)
    uploads = dict(result.get("oss_multipart_files") or {})
    declaration = uploads.get(file_id)
    if not isinstance(declaration, dict) or not declaration.get("upload_id"):
        raise ValueError("multipart upload is not initialized")
    declaration = dict(declaration)

    def _load(result: dict[str, object]) -> dict[str, object] | None:
        uploads = dict(result.get("oss_multipart_files") or {})
        return uploads.get(file_id)

    def _save(result: dict[str, object], decl: dict[str, object]) -> None:
        uploads = dict(result.get("oss_multipart_files") or {})
        uploads[file_id] = decl
        result["oss_multipart_files"] = uploads

    # Preview bytes are downloaded and SHA-256 verified by the admission
    # worker, so provider CRC64 is not required here (require_crc64=False).
    upload_session, result = _complete_declared_multipart(
        db,
        workspace_id=workspace_id,
        upload_session_id=upload_session_id,
        upload_session=upload_session,
        result=result,
        declaration=declaration,
        parts=parts,
        load=_load,
        save=_save,
        bucket_role="process",
        require_crc64=False,
    )
    if _all_uploads_completed(result, _declared_sources(result)):
        upload_session = mark_upload_session_uploaded(
            db,
            workspace_id=workspace_id,
            upload_session_id=upload_session.id,
        )
        ensure_collection_upload_parse_job(db, upload_session)
    return upload_session


def _chunk_progress(
    upload_session: CollectionUploadSession, declaration: dict[str, object]
) -> dict[str, object]:
    return {
        "id": upload_session.id,
        "status": upload_session.status,
        "uploaded_chunks": len(declaration.get("uploaded_chunks") or []),
        "uploaded_chunk_indices": sorted(declaration.get("uploaded_chunks") or []),
        "total_chunks": int(declaration["total_chunks"]),
        "uploaded_bytes": int(declaration.get("uploaded_bytes") or 0),
    }


def _oss_progress(
    upload_session: CollectionUploadSession, declaration: dict[str, object]
) -> dict[str, object]:
    return {
        "id": upload_session.id,
        "status": upload_session.status,
        "total_size_bytes": int(declaration["total_size_bytes"]),
        "part_size_bytes": int(declaration["part_size"]),
        "total_parts": int(declaration["total_parts"]),
    }


def _expected_part_size(declaration: dict[str, object], part_number: int) -> int:
    total_parts = int(declaration["total_parts"])
    part_size = int(declaration["part_size"])
    total_size = int(declaration["total_size_bytes"])
    return total_size - part_size * (total_parts - 1) if part_number == total_parts else part_size


def _normalize_client_parts(
    parts: list[dict[str, object]], *, total_parts: int
) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for item in parts:
        number = item.get("part_number")
        etag = str(item.get("etag") or "").strip().strip('"')
        if isinstance(number, bool) or not isinstance(number, int) or not etag:
            raise ValueError("multipart part manifest is invalid")
        normalized.append({"part_number": number, "etag": etag})
    normalized.sort(key=lambda item: int(item["part_number"]))
    if [item["part_number"] for item in normalized] != list(range(1, total_parts + 1)):
        raise ValueError("multipart part manifest is invalid")
    return normalized


def _safe_child(parent: Path, name: str) -> Path:
    candidate = (parent / name).resolve()
    if candidate.parent != parent.resolve():
        raise ValueError("collection upload path is outside staging root")
    return candidate


def _safe_relative(parent: Path, relative: str) -> Path:
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("collection upload path is outside staging root")
    candidate = parent / relative
    resolved = candidate.resolve()
    if not resolved.is_relative_to(parent.resolve()) or resolved == parent.resolve():
        raise ValueError("collection upload path is outside staging root")
    # Do not allow an existing symlink to alias another declared source.
    cursor = candidate
    while cursor != parent:
        if cursor.is_symlink():
            raise ValueError("collection upload path must not contain symlinks")
        cursor = cursor.parent
    return resolved
