"""Catalog datasets: creation, versioning, export, archival, and direct LeRobot export."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from data.config import settings
from data.database import NativeLerobotDataset
from data.infra.object_storage import StorageObjectRef
from data.infra.storage_provider import get_storage_provider
from data.models.catalog_dataset import (
    CATALOG_EXPORT_FORMATS,
    CatalogDataset,
    CatalogDatasetExport,
    CatalogDatasetVersion,
    CatalogDatasetVersionAsset,
)
from data.models.data_asset import DataAsset
from data.models.native_lerobot_direct import NativeLerobotDirectSource
from data.services.resource_names import (
    is_constraint_conflict,
    normalized_name,
)

_NAME_CONSTRAINT = "uq_catalog_datasets_normalized_name"
_VERSION_PAIR_CONSTRAINT = "uq_catalog_dataset_versions_dataset_version"


class CatalogDatasetError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)

    def as_detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


class CatalogDatasetConflict(CatalogDatasetError):
    pass


class CatalogDatasetNotSupported(CatalogDatasetError):
    pass


def validate_lerobot_dataset(dataset_path: str | Path) -> bool:
    """Run the vendored QRDF LeRobot validator against materialized files."""
    try:
        from qrdf.converters.validate_lerobot import (
            validate_lerobot_dataset as qrdf_validate,
        )

        report = qrdf_validate(Path(dataset_path))
        return bool(report.ok)
    except Exception:
        return False


def create_catalog_dataset(
    db: Session,
    *,
    name: str,
    description: str = "",
    source_kind: str = "qrdf_assets",
    created_by_user_id: int | None = None,
    native_lerobot_dataset_id: int | None = None,
) -> CatalogDataset:
    if source_kind not in {"qrdf_assets", "lerobot_direct"}:
        raise CatalogDatasetError("invalid_source_kind", "source_kind is invalid")
    display_name = normalized_name(name)
    dataset = CatalogDataset(
        name=display_name,
        description=str(description or ""),
        status="active",
        source_kind=source_kind,
        native_lerobot_dataset_id=native_lerobot_dataset_id,
        created_by_user_id=created_by_user_id,
    )
    try:
        with db.begin_nested():
            db.add(dataset)
            db.flush()
    except IntegrityError as exc:
        if is_constraint_conflict(exc, _NAME_CONSTRAINT):
            raise CatalogDatasetConflict(
                "catalog_dataset_name_exists",
                "数据集名称不可重复",
            ) from exc
        raise
    db.refresh(dataset)
    return dataset


def _catalog_list_query(db: Session):
    return db.query(CatalogDataset).order_by(
        CatalogDataset.created_at.desc(), CatalogDataset.id.desc()
    )


def list_catalog_datasets(db: Session) -> list[CatalogDataset]:
    return _catalog_list_query(db).all()


def list_catalog_datasets_page(db: Session, *, limit: int | None, offset: int | None) -> dict:
    from data.services.list_pagination import paginate_list

    return paginate_list(_catalog_list_query(db), limit=limit, offset=offset)


def get_catalog_dataset(db: Session, *, dataset_id: int) -> CatalogDataset:
    dataset = db.query(CatalogDataset).filter(CatalogDataset.id == dataset_id).one_or_none()
    if dataset is None:
        raise LookupError("catalog dataset does not exist")
    return dataset


def create_catalog_version(
    db: Session,
    *,
    dataset_id: int,
    data_asset_ids: list[int],
    created_by_user_id: int | None = None,
) -> CatalogDatasetVersion:
    dataset = (
        db.query(CatalogDataset)
        .filter(CatalogDataset.id == dataset_id)
        .with_for_update()
        .one_or_none()
    )
    if dataset is None:
        raise LookupError("catalog dataset does not exist")
    if dataset.source_kind != "qrdf_assets":
        raise CatalogDatasetError(
            "assets_not_supported",
            "only qrdf_assets datasets accept asset version lists",
        )
    if dataset.status != "active":
        raise CatalogDatasetError("dataset_archived", "dataset is archived")

    ordered_ids = _dedupe_preserve_order(data_asset_ids)
    if not ordered_ids:
        raise CatalogDatasetError("empty_asset_list", "data_asset_ids must not be empty")

    assets = db.query(DataAsset).filter(DataAsset.id.in_(ordered_ids)).all()
    found = {asset.id for asset in assets}
    missing = [asset_id for asset_id in ordered_ids if asset_id not in found]
    if missing:
        raise LookupError(f"data asset does not exist: {missing[0]}")

    from data.integrations.qrdf.annotated_segment_export import approved_segments

    for asset in assets:
        snapshot = asset.source_snapshot_json or {}
        if (asset.stage_snapshot_json or {}).get("annotation") == "passed" and snapshot.get(
            "annotation_enabled"
        ) is not True:
            raise CatalogDatasetError(
                "asset_annotation_unavailable", "asset has no frozen approved annotation ranges"
            )
        if not snapshot.get("episodes") or not asset.source_snapshot_id:
            raise CatalogDatasetError(
                "asset_snapshot_unavailable", "asset has no frozen effective Episode sources"
            )
        if _snapshot_id(snapshot) != asset.source_snapshot_id:
            raise CatalogDatasetError(
                "asset_snapshot_changed", "asset source snapshot hash does not match"
            )
        try:
            for episode in snapshot["episodes"]:
                if snapshot.get("annotation_enabled") or "effective_segments" in episode:
                    approved_segments(episode)
        except (ValueError, KeyError, TypeError) as exc:
            raise CatalogDatasetError(
                "asset_annotation_unavailable", "asset has no valid approved annotation ranges"
            ) from exc

    next_version = _next_version_number(db, dataset_id=dataset.id)
    version = CatalogDatasetVersion(
        dataset_id=dataset.id,
        version=next_version,
        status="active",
        created_by_user_id=created_by_user_id,
    )
    frozen_assets = [
        {
            "asset_id": asset.id,
            "snapshot_id": asset.source_snapshot_id,
            "position": position,
            "source_snapshot": deepcopy(asset.source_snapshot_json or {}),
        }
        for position, asset_id in enumerate(ordered_ids, start=1)
        for asset in [next(item for item in assets if item.id == asset_id)]
    ]
    version.source_snapshot_json = {
        "schema": "quicstudio.catalog-source.v1",
        "assets": frozen_assets,
    }
    version.source_snapshot_id = _snapshot_id(version.source_snapshot_json)
    try:
        with db.begin_nested():
            db.add(version)
            db.flush()
            for position, asset_id in enumerate(ordered_ids, start=1):
                db.add(
                    CatalogDatasetVersionAsset(
                        version_id=version.id,
                        data_asset_id=asset_id,
                        position=position,
                        asset_snapshot_id=next(
                            item for item in assets if item.id == asset_id
                        ).source_snapshot_id,
                    )
                )
            db.flush()
    except IntegrityError as exc:
        if is_constraint_conflict(exc, _VERSION_PAIR_CONSTRAINT):
            raise CatalogDatasetConflict(
                "version_conflict",
                "catalog dataset version conflict",
            ) from exc
        raise

    db.refresh(version)
    return (
        db.query(CatalogDatasetVersion)
        .options(joinedload(CatalogDatasetVersion.assets))
        .filter(CatalogDatasetVersion.id == version.id)
        .one()
    )


def _catalog_version_list_query(db: Session, *, dataset_id: int):
    get_catalog_dataset(db, dataset_id=dataset_id)
    return (
        db.query(CatalogDatasetVersion)
        .options(joinedload(CatalogDatasetVersion.assets))
        .filter(CatalogDatasetVersion.dataset_id == dataset_id)
        .order_by(CatalogDatasetVersion.version.asc())
    )


def list_catalog_versions(db: Session, *, dataset_id: int) -> list[CatalogDatasetVersion]:
    return _catalog_version_list_query(db, dataset_id=dataset_id).all()


def list_catalog_versions_page(
    db: Session, *, dataset_id: int, limit: int | None, offset: int | None
) -> dict:
    from data.services.list_pagination import paginate_list

    return paginate_list(
        _catalog_version_list_query(db, dataset_id=dataset_id), limit=limit, offset=offset
    )


def get_catalog_version(db: Session, *, version_id: int) -> CatalogDatasetVersion:
    version = (
        db.query(CatalogDatasetVersion)
        .options(
            joinedload(CatalogDatasetVersion.assets),
            joinedload(CatalogDatasetVersion.dataset),
            joinedload(CatalogDatasetVersion.exports),
        )
        .filter(CatalogDatasetVersion.id == version_id)
        .one_or_none()
    )
    if version is None:
        raise LookupError("catalog dataset version does not exist")
    return version


def export_catalog_version(
    db: Session,
    *,
    version_id: int,
    export_format: str,
    idempotency_key: str | None = None,
    actor_id: int | None = None,
) -> CatalogDatasetExport:
    if export_format not in CATALOG_EXPORT_FORMATS:
        raise CatalogDatasetError("invalid_format", "export format is invalid")

    locked = (
        db.query(CatalogDatasetVersion)
        .filter(CatalogDatasetVersion.id == version_id)
        .with_for_update()
        .one_or_none()
    )
    if locked is None:
        raise LookupError("catalog dataset version does not exist")

    # Load relationships after the row lock — FOR UPDATE cannot target the
    # nullable side of outer joins produced by joinedload.
    version = (
        db.query(CatalogDatasetVersion)
        .options(
            joinedload(CatalogDatasetVersion.dataset),
            joinedload(CatalogDatasetVersion.assets),
        )
        .filter(CatalogDatasetVersion.id == version_id)
        .one()
    )
    dataset = version.dataset
    if dataset.source_kind == "lerobot_direct" and export_format == "qrdf_0_2":
        raise CatalogDatasetNotSupported(
            "not_supported",
            "LeRobot direct datasets cannot export QRDF",
        )

    from data.services.catalog_export_jobs import enqueue_catalog_export

    key = str(idempotency_key or f"catalog:{version.id}:{export_format}")
    export, _job, _created = enqueue_catalog_export(
        db,
        version_id=version.id,
        export_format=export_format,
        idempotency_key=key,
        actor_id=actor_id,
    )
    db.flush()
    db.refresh(export)
    return export


def archive_catalog_version(db: Session, *, version_id: int) -> CatalogDatasetVersion:
    version = (
        db.query(CatalogDatasetVersion)
        .filter(CatalogDatasetVersion.id == version_id)
        .with_for_update()
        .one_or_none()
    )
    if version is None:
        raise LookupError("catalog dataset version does not exist")
    version.status = "archived"
    db.flush()
    db.refresh(version)
    return version


def delete_catalog_version(db: Session, *, version_id: int) -> None:
    version = (
        db.query(CatalogDatasetVersion)
        .filter(CatalogDatasetVersion.id == version_id)
        .with_for_update()
        .one_or_none()
    )
    if version is None:
        raise LookupError("catalog dataset version does not exist")
    export_count = (
        db.query(CatalogDatasetExport).filter(CatalogDatasetExport.version_id == version.id).count()
    )
    if export_count > 0:
        raise CatalogDatasetConflict(
            "version_referenced",
            "exported versions cannot be deleted",
        )
    direct_source_count = (
        db.query(NativeLerobotDirectSource)
        .filter(NativeLerobotDirectSource.catalog_dataset_version_id == version.id)
        .count()
    )
    if direct_source_count > 0:
        raise CatalogDatasetConflict(
            "version_referenced",
            "versions with a native LeRobot source cannot be deleted; archive them instead",
        )
    db.query(CatalogDatasetVersionAsset).filter(
        CatalogDatasetVersionAsset.version_id == version.id
    ).delete(synchronize_session=False)
    db.delete(version)
    db.flush()


def import_lerobot_direct(
    db: Session,
    *,
    name: str,
    native_lerobot_dataset_id: int,
    description: str = "",
    created_by_user_id: int | None = None,
) -> tuple[CatalogDataset, CatalogDatasetVersion]:
    """Attach a validated Native LeRobot dataset as catalog source_kind=lerobot_direct."""
    native = (
        db.query(NativeLerobotDataset)
        .filter(NativeLerobotDataset.id == native_lerobot_dataset_id)
        .with_for_update()
        .one_or_none()
    )
    if native is None:
        raise LookupError("native lerobot dataset does not exist")
    if native.status != "active":
        raise CatalogDatasetError(
            "native_lerobot_inactive",
            "native lerobot dataset is not active",
        )
    objects = native.objects_json if isinstance(native.objects_json, list) else []
    if not objects:
        raise CatalogDatasetNotSupported(
            "native_lerobot_objects_missing",
            "native LeRobot object manifest is missing",
        )
    provider, raw_bucket, root_key = _native_raw_source(native)
    source_objects, marker_ref, marker_payload = _materialize_native_lerobot(
        provider,
        raw_bucket=raw_bucket,
        root_key=root_key,
        objects=objects,
        native=native,
    )

    dataset = create_catalog_dataset(
        db,
        name=name,
        description=description,
        source_kind="lerobot_direct",
        created_by_user_id=created_by_user_id,
        native_lerobot_dataset_id=native.id,
    )
    version = CatalogDatasetVersion(
        dataset_id=dataset.id,
        version=1,
        status="active",
        created_by_user_id=created_by_user_id,
    )
    version.source_snapshot_json = {
        "schema": "quicstudio.lerobot-source.v1",
        "native_lerobot_dataset_id": native.id,
        "raw_bucket_role": "raw",
        "marker": {
            "path": "complete.json",
            "ref": marker_ref,
            "marker_sha256": native.marker_sha256,
        },
        "manifest_sha256": native.manifest_sha256,
        "objects": source_objects,
        "marker_summary": {
            "file_count": marker_payload["file_count"],
            "total_size": marker_payload["total_size"],
        },
    }
    version.source_snapshot_id = _snapshot_id(version.source_snapshot_json)
    db.add(version)
    db.flush()
    db.refresh(version)
    return dataset, version


def version_asset_ids(version: CatalogDatasetVersion) -> list[int]:
    ordered = sorted(version.assets, key=lambda row: row.position)
    return [row.data_asset_id for row in ordered]


def _native_raw_source(native: NativeLerobotDataset) -> tuple[Any, str, str]:
    """Resolve a native source only when it is already in the configured raw bucket."""
    parsed = urlsplit(str(native.source_oss_uri or ""))
    if parsed.scheme != "oss" or not parsed.netloc or not parsed.path:
        raise CatalogDatasetNotSupported(
            "native_lerobot_source_invalid",
            "native LeRobot source URI is invalid",
        )
    try:
        provider = get_storage_provider()
        raw_bucket = str(provider.bucket_config.buckets["raw"])
    except Exception as exc:
        raise CatalogDatasetNotSupported(
            "native_lerobot_provider_unavailable",
            "raw object provider is unavailable; re-import the source into raw",
        ) from exc
    if parsed.netloc != raw_bucket:
        raise CatalogDatasetNotSupported(
            "native_lerobot_raw_bucket_required",
            "native LeRobot source is not in the configured raw bucket; re-import into raw",
        )
    root = parsed.path.lstrip("/").rstrip("/")
    if (
        not root
        or "//" in root
        or "\\" in root
        or any(part in {"", ".", ".."} for part in PurePosixPath(root).parts)
    ):
        raise CatalogDatasetNotSupported(
            "native_lerobot_source_invalid",
            "native LeRobot source path is invalid",
        )
    return provider, raw_bucket, root


def _materialize_native_lerobot(
    provider: Any,
    *,
    raw_bucket: str,
    root_key: str,
    objects: list[object],
    native: NativeLerobotDataset,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """HEAD and download every source object into bounded scratch, then validate."""
    normalized: list[dict[str, Any]] = []
    object_specs: list[dict[str, Any]] = []
    previous = ""
    total_size = 0
    manifest_rows: list[dict[str, Any]] = []
    for item in objects:
        if not isinstance(item, dict):
            raise CatalogDatasetNotSupported(
                "native_lerobot_manifest_invalid", "native LeRobot object manifest is invalid"
            )
        path = item.get("path")
        size = item.get("size")
        sha256 = item.get("sha256")
        if (
            not isinstance(path, str)
            or not path.startswith(("data/", "meta/", "videos/"))
            or any(part in {"", ".", ".."} for part in PurePosixPath(path).parts)
            or (previous and path <= previous)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        ):
            raise CatalogDatasetNotSupported(
                "native_lerobot_manifest_invalid", "native LeRobot object manifest is invalid"
            )
        previous = path
        total_size += size
        manifest_rows.append({"path": path, "size": size, "sha256": sha256})
        object_specs.append({"path": path, "key": f"{root_key}/{path}"})
    if len(manifest_rows) != int(native.file_count) or total_size != int(native.total_size):
        raise CatalogDatasetNotSupported(
            "native_lerobot_marker_summary_mismatch",
            "native LeRobot marker summary does not match its manifest",
        )
    expected_manifest = hashlib.sha256(
        json.dumps(
            {
                "robot_type": native.robot_type,
                "dataset_id": native.dataset_id,
                "objects": manifest_rows,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if expected_manifest != native.manifest_sha256:
        raise CatalogDatasetNotSupported(
            "native_lerobot_manifest_hash_mismatch", "native LeRobot manifest hash does not match"
        )

    scratch_config = Path(str(getattr(settings, "scratch_root", ""))).expanduser()
    if not scratch_config.is_absolute():
        scratch_config = Path.cwd() / scratch_config
    # Check the configured scratch tree itself, while allowing the host's
    # conventional /tmp and /var aliases (macOS exposes these as symlinks to
    # /private).  Walking every filesystem ancestor treats that harmless OS
    # alias as an unsafe scratch escape and makes a valid deployment fail.
    # Any symlink at or below the configured path remains rejected, including
    # a symlinked configured root and a symlinked parent created by an attacker.
    probe = scratch_config.absolute()
    host_aliases = {Path("/tmp"), Path("/var")}  # nosec B108 - trusted OS path aliases
    while probe != probe.parent:
        if probe not in host_aliases and probe.is_symlink():
            raise CatalogDatasetNotSupported(
                "native_lerobot_scratch_unavailable",
                "native LeRobot scratch path contains a symlink",
            )
        probe = probe.parent
    scratch_root = scratch_config.absolute()
    try:
        scratch_budget = int(getattr(settings, "scratch_max_bytes", 0))
    except (TypeError, ValueError):
        scratch_budget = 0
    if scratch_budget <= 0 or total_size > scratch_budget:
        raise CatalogDatasetNotSupported(
            "native_lerobot_scratch_limit", "native LeRobot source exceeds scratch limit"
        )
    try:
        scratch_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CatalogDatasetNotSupported(
            "native_lerobot_scratch_unavailable", "native LeRobot scratch directory is unavailable"
        ) from exc
    try:
        with tempfile.TemporaryDirectory(
            prefix="lerobot-intake-", dir=str(scratch_root)
        ) as scratch:
            dataset_path = Path(scratch)
            marker_key = f"{root_key}/complete.json"
            marker_head = _head_only(provider, marker_key)
            if total_size + marker_head.size_bytes > scratch_budget:
                raise CatalogDatasetNotSupported(
                    "native_lerobot_scratch_limit", "native LeRobot source exceeds scratch limit"
                )
            for spec in object_specs:
                ref = _head_and_download(
                    provider, raw_bucket, spec["key"], dataset_path / spec["path"]
                )
                expected = next(row for row in manifest_rows if row["path"] == spec["path"])
                if ref.size_bytes != expected["size"] or ref.sha256 != expected["sha256"]:
                    raise CatalogDatasetNotSupported(
                        "native_lerobot_object_changed",
                        "native LeRobot object changed during intake",
                    )
                normalized.append({"path": spec["path"], "ref": _ref_json(ref)})
            marker_ref_obj = _download_ref(provider, marker_head, dataset_path / "complete.json")
            marker_path = dataset_path / "complete.json"
            try:
                marker_payload = json.loads(marker_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise CatalogDatasetNotSupported(
                    "native_lerobot_marker_invalid", "native LeRobot complete marker is invalid"
                ) from exc
            if not isinstance(marker_payload, dict):
                raise CatalogDatasetNotSupported(
                    "native_lerobot_marker_invalid", "native LeRobot complete marker is invalid"
                )
            marker_digest = hashlib.sha256(
                json.dumps(
                    marker_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            if marker_digest != native.marker_sha256:
                raise CatalogDatasetNotSupported(
                    "native_lerobot_marker_changed",
                    "native LeRobot complete marker changed during intake",
                )
            if (
                marker_payload.get("file_count") != native.file_count
                or marker_payload.get("total_size") != native.total_size
                or marker_payload.get("manifest_sha256") != native.manifest_sha256
                or marker_payload.get("objects") != manifest_rows
            ):
                raise CatalogDatasetNotSupported(
                    "native_lerobot_marker_mismatch",
                    "native LeRobot complete marker does not match persisted identity",
                )
            if not validate_lerobot_dataset(dataset_path):
                raise CatalogDatasetNotSupported(
                    "native_lerobot_validation_failed", "native LeRobot SDK validation failed"
                )
            return normalized, _ref_json(marker_ref_obj), marker_payload
    except CatalogDatasetError:
        raise
    except Exception as exc:
        raise CatalogDatasetNotSupported(
            "native_lerobot_intake_failed", "native LeRobot source intake failed"
        ) from exc


def _head_and_download(provider: Any, bucket: str, key: str, destination: Path) -> StorageObjectRef:
    headed = _head_only(provider, key)
    return _download_ref(provider, headed, destination)


def _head_only(provider: Any, key: str) -> StorageObjectRef:
    if (
        not key
        or key.startswith("/")
        or "//" in key
        or "\\" in key
        or any(part in {"", ".", ".."} for part in PurePosixPath(key).parts)
    ):
        raise CatalogDatasetNotSupported(
            "native_lerobot_path_invalid", "native LeRobot object path is invalid"
        )
    initial = StorageObjectRef("raw", key, None, "", 0, None)
    try:
        headed = provider.head(initial)
    except Exception as exc:
        raise CatalogDatasetNotSupported(
            "native_lerobot_object_unavailable", "native LeRobot object HEAD failed"
        ) from exc
    if not isinstance(headed, StorageObjectRef):
        raise CatalogDatasetNotSupported(
            "native_lerobot_object_identity_invalid", "provider returned an invalid object identity"
        )
    if (
        headed.bucket_role != "raw"
        or headed.object_key != key
        or not headed.etag
        or headed.size_bytes < 0
    ):
        raise CatalogDatasetNotSupported(
            "native_lerobot_object_identity_invalid",
            "provider returned an unverifiable object identity",
        )
    return headed


def _download_ref(provider: Any, headed: StorageObjectRef, destination: Path) -> StorageObjectRef:
    try:
        downloaded = provider.download_file(headed, str(destination))
    except Exception as exc:
        raise CatalogDatasetNotSupported(
            "native_lerobot_object_unavailable", "native LeRobot object download failed"
        ) from exc
    if not isinstance(downloaded, StorageObjectRef):
        raise CatalogDatasetNotSupported(
            "native_lerobot_object_identity_invalid", "provider returned an invalid object identity"
        )
    if (
        downloaded.bucket_role != headed.bucket_role
        or downloaded.object_key != headed.object_key
        or downloaded.version_id != headed.version_id
        or downloaded.etag != headed.etag
        or not downloaded.etag
        or downloaded.size_bytes < 0
        or re.fullmatch(r"[0-9a-f]{64}", str(downloaded.sha256 or "")) is None
    ):
        raise CatalogDatasetNotSupported(
            "native_lerobot_object_identity_invalid",
            "provider returned an unverifiable object identity",
        )
    if downloaded.size_bytes != headed.size_bytes:
        raise CatalogDatasetNotSupported(
            "native_lerobot_object_changed", "native LeRobot object changed during download"
        )
    if headed.sha256 and downloaded.sha256 != headed.sha256:
        raise CatalogDatasetNotSupported(
            "native_lerobot_object_changed", "native LeRobot object SHA changed during download"
        )
    return downloaded


def _ref_json(ref: StorageObjectRef) -> dict[str, Any]:
    return {
        "bucket_role": ref.bucket_role,
        "object_key": ref.object_key,
        "version_id": ref.version_id,
        "etag": ref.etag,
        "size_bytes": ref.size_bytes,
        "sha256": ref.sha256,
    }


def _dedupe_preserve_order(ids: list[int]) -> list[int]:
    seen: set[int] = set()
    ordered: list[int] = []
    for raw in ids:
        asset_id = int(raw)
        if asset_id in seen:
            continue
        seen.add(asset_id)
        ordered.append(asset_id)
    return ordered


def _next_version_number(db: Session, *, dataset_id: int) -> int:
    current = db.scalar(
        select(func.coalesce(func.max(CatalogDatasetVersion.version), 0)).where(
            CatalogDatasetVersion.dataset_id == dataset_id
        )
    )
    return int(current or 0) + 1


def _snapshot_id(snapshot: dict[str, Any]) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def serialize_export(export: CatalogDatasetExport) -> dict[str, Any]:
    detail = export.detail_json if isinstance(export.detail_json, dict) else {}
    candidates: list[str] = []
    if isinstance(export.oss_uri, str) and export.oss_uri.strip():
        candidates.append(export.oss_uri.strip())
    detail_uri = detail.get("oss_uri")
    if isinstance(detail_uri, str) and detail_uri.strip():
        candidates.append(detail_uri.strip())
    oss_uri = next(
        (uri for uri in candidates if uri.startswith("oss://") and uri != "oss://"),
        None,
    )
    return {
        "id": export.id,
        "version_id": export.version_id,
        "format": export.format,
        "status": export.status,
        "checksum": export.checksum,
        "detail_json": detail,
        "oss_uri": oss_uri,
        "size_bytes": export.size_bytes,
        "sha256": export.sha256,
        "manifest_summary": detail.get("manifest_summary", {}),
        "media_type": detail.get("media_type"),
        "extension": detail.get("extension"),
        "attempt": export.attempt,
        "error_code": export.error_code or None,
        "error_message": export.error_message or None,
    }
