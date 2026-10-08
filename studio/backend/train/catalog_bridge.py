"""Catalog export record to QuicTrain dataset registration bridge module.

Registration to the training control plane is allowed only when a stable oss:// URI exists.
This module does not import data.services directly; routes query export records persisted
by the public Catalog API and invoke QuicTrain's dataset registration service.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

_TRAIN_SRC = Path(__file__).resolve().parent / "src"
if _TRAIN_SRC.is_dir() and str(_TRAIN_SRC) not in sys.path:
    sys.path.insert(0, str(_TRAIN_SRC))

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

router = APIRouter(prefix="/train", tags=["train"])
_bearer = HTTPBearer(auto_error=False)

_PLACEHOLDER_MESSAGE = "导出尚未产生稳定的 oss:// URI，不能登记到训练。"
_FORMAT_MESSAGE = "只能登记成功的 LeRobot 3.0 导出。"
_STATUS_MESSAGE = "导出完成后才能登记到训练。"
_METADATA_MESSAGE = "导出缺少已验证的 LeRobot 元数据，请重新导出后登记。"


class CatalogRegistrationRequest(BaseModel):
    """Catalog export dataset registration request parameters."""

    version_id: int = Field(ge=1)
    export_id: int | None = Field(default=None, ge=1)


def stable_oss_uri(export: dict[str, Any] | None) -> str | None:
    """Extract valid stable oss:// URI from export record."""
    if not isinstance(export, dict):
        return None
    detail = export.get("detail_json") if isinstance(export.get("detail_json"), dict) else {}
    raw = export.get("oss_uri") or detail.get("oss_uri")
    if not isinstance(raw, str):
        return None
    uri = raw.strip()
    if not uri.startswith("oss://") or uri == "oss://":
        return None
    return uri


def registration_block_reason(export: dict[str, Any] | None) -> str | None:
    """Check whether an export is a successful, stable LeRobot registration source."""
    if not isinstance(export, dict):
        return _PLACEHOLDER_MESSAGE
    if str(export.get("format") or "") != "lerobot_3_0":
        return _FORMAT_MESSAGE
    if str(export.get("status") or "").lower() != "succeeded":
        return _STATUS_MESSAGE
    if stable_oss_uri(export) is None:
        return _PLACEHOLDER_MESSAGE
    try:
        export_dataset_metadata(export)
    except ValueError as exc:
        return str(exc)
    return None


def export_dataset_metadata(export: dict[str, Any]) -> dict[str, Any]:
    """Read worker-verified info.json, bound to this exact immutable archive."""
    detail = export.get("detail_json") or {}
    metadata = detail.get("lerobot_metadata") if isinstance(detail, dict) else None
    if not isinstance(metadata, dict):
        raise ValueError(_METADATA_MESSAGE)
    checksum = str(export.get("checksum") or "").removeprefix("sha256:")
    raw = metadata.get("info_json")
    if (
        metadata.get("schema") != "quicstudio.lerobot-export-metadata.v1"
        or re.fullmatch(r"[0-9a-f]{64}", checksum) is None
        or metadata.get("archive_sha256") != checksum
        or not isinstance(raw, str)
        or len(raw.encode("utf-8")) > 1024 * 1024
        or hashlib.sha256(raw.encode("utf-8")).hexdigest() != metadata.get("info_sha256")
    ):
        raise ValueError("LeRobot 元数据与导出归档的不可变身份不一致。")
    try:
        info = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("LeRobot info.json 无效。") from exc
    if not isinstance(info, dict) or info.get("codebase_version") not in {"3.0", "v3.0"}:
        raise ValueError("LeRobot info.json 不是受支持的 3.0 元数据。")

    def count(name: str, *, required: bool = True) -> int | None:
        value = info.get(name)
        if value is None and not required:
            return None
        if type(value) is not int or value < 0:
            raise ValueError(f"LeRobot info.json 缺少有效的 {name}。")
        return value

    episodes, frames = count("total_episodes"), count("total_frames")
    fps = info.get("fps")
    if (
        isinstance(fps, bool)
        or not isinstance(fps, (int, float))
        or not math.isfinite(fps)
        or fps <= 0
    ):
        raise ValueError("LeRobot info.json 缺少有效的 fps。")
    features = info.get("features")
    if not isinstance(features, dict):
        raise ValueError("LeRobot info.json 缺少 features。")
    camera_keys = sorted(
        name
        for name, feature in features.items()
        if name.startswith("observation.")
        and isinstance(feature, dict)
        and feature.get("dtype") in {"image", "video"}
    )

    def dimension(name: str) -> int:
        if name not in features:
            return 0
        feature = features[name]
        shape = feature.get("shape") if isinstance(feature, dict) else None
        if (
            not isinstance(shape, list)
            or len(shape) != 1
            or type(shape[0]) is not int
            or shape[0] <= 0
        ):
            raise ValueError(f"LeRobot {name} 不是有效的一维向量特征。")
        return shape[0]

    task_count = count("total_tasks", required=False)
    robot_type = info.get("robot_type")
    if robot_type is not None and (not isinstance(robot_type, str) or not robot_type.strip()):
        raise ValueError("LeRobot robot_type 无效。")
    return {
        "checksum": f"sha256:{checksum}",
        "format": "lerobot",
        "format_version": "3.0",
        "episodes": episodes,
        "frames": frames,
        "fps": fps,
        "duration_hours": frames / fps / 3600,
        "robot_type": robot_type,
        "camera_keys": camera_keys,
        "action_dim": dimension("action"),
        "state_dim": dimension("observation.state"),
        "language_tasks": task_count > 0 if task_count is not None else None,
    }


def export_payload(export: Any) -> dict[str, Any]:
    """Project a CatalogDatasetExport ORM row into the bridge's safe input shape."""
    detail = export.detail_json if isinstance(export.detail_json, dict) else {}
    return {
        "id": export.id,
        "version_id": export.version_id,
        "format": export.format,
        "status": export.status,
        "checksum": export.checksum,
        "detail_json": detail,
        "oss_uri": export.oss_uri or detail.get("oss_uri"),
    }


def register_stable_export(
    *,
    export: dict[str, Any],
    actor_id: str,
    display_name: str,
    dataset_id: str,
    version: str,
) -> dict[str, Any]:
    """Register export record with generated stable OSS URI into QuicTrain dataset version table."""
    reason = registration_block_reason(export)
    if reason:
        raise ValueError(reason)
    uri = stable_oss_uri(export)
    assert uri is not None
    metadata = export_dataset_metadata(export)

    from quictrain_api.db import SessionLocal
    from quictrain_api.schemas import DatasetRegisterRequest
    from quictrain_api.service import register_dataset_version
    from quictrain_api.settings import get_settings

    client_request_id = f"catalog-{dataset_id}-{version}"[:128]
    if len(client_request_id) < 8:
        client_request_id = f"catalog-{client_request_id}"
    request = DatasetRegisterRequest(
        client_request_id=client_request_id,
        display_name=display_name[:240] or dataset_id,
        uri=uri,
        version=version[:128],
        dataset_id=dataset_id[:240],
        status="REGISTERED",
        request_materialization=False,
        **metadata,
    )
    with SessionLocal() as session:
        record, created = register_dataset_version(
            session,
            project_id=get_settings().default_project_id,
            actor_id=actor_id,
            request=request,
        )
        session.commit()
        return {
            "dataset_version_id": record.id,
            "created": created,
            "uri": record.uri,
            "checksum": record.checksum,
            "status": record.status,
        }


def _data_db():
    from data.database import get_db

    yield from get_db()


def _data_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(_data_db),
) -> dict:
    from data.utils.helpers import validate_access_token

    if credentials is None:
        raise HTTPException(status_code=401, detail="未登录")
    return validate_access_token(credentials.credentials, db)


@router.post("/catalog-registrations")
def register_catalog_export(
    body: CatalogRegistrationRequest,
    db: Session = Depends(_data_db),
    user: dict = Depends(_data_user),
) -> dict[str, Any]:
    """Register export artifacts of specified Catalog dataset version into QuicTrain system."""
    from data.models.catalog_dataset import (
        CatalogDataset,
        CatalogDatasetExport,
        CatalogDatasetVersion,
    )
    from data.utils.helpers import require_permission, success

    require_permission(user, "train:write")
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="权限不足，仅管理员可将导出数据集登记至训练集")
    version = (
        db.query(CatalogDatasetVersion)
        .filter(CatalogDatasetVersion.id == body.version_id)
        .one_or_none()
    )
    if version is None:
        raise HTTPException(status_code=404, detail="catalog dataset version does not exist")
    if body.export_id is not None:
        # Resolve an explicit export by identity first so a cross-version id
        # is reported as such instead of becoming an ambiguous placeholder.
        export = (
            db.query(CatalogDatasetExport)
            .filter(CatalogDatasetExport.id == body.export_id)
            .one_or_none()
        )
    else:
        # Do not guess from the newest attempt: QRDF, failed and running
        # exports may be newer than the stable LeRobot artifact.
        export = None
        export_query = db.query(CatalogDatasetExport).filter(
            CatalogDatasetExport.version_id == version.id,
            CatalogDatasetExport.format == "lerobot_3_0",
            CatalogDatasetExport.status == "succeeded",
        )
        for candidate in export_query.order_by(CatalogDatasetExport.id.desc()):
            candidate_payload = export_payload(candidate)
            if (
                candidate_payload["format"] == "lerobot_3_0"
                and candidate_payload["status"] == "succeeded"
                and stable_oss_uri(candidate_payload)
            ):
                export = candidate
                break
    if export is None:
        raise HTTPException(status_code=409, detail=_PLACEHOLDER_MESSAGE)
    if int(export.version_id) != int(version.id):
        raise HTTPException(
            status_code=404,
            detail="catalog export does not belong to dataset version",
        )
    payload = export_payload(export)
    reason = registration_block_reason(payload)
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    dataset = db.get(CatalogDataset, version.dataset_id)
    display_name = dataset.name if dataset is not None else f"catalog-{version.dataset_id}"
    actor_id = str(user.get("sub") or user.get("id") or "1")
    try:
        try:
            from train.mount import startup_train
        except ImportError:
            from mount import startup_train

        startup_train()
        registered = register_stable_export(
            export=payload,
            actor_id=actor_id,
            display_name=display_name,
            dataset_id=f"catalog-{version.dataset_id}",
            version=f"v{version.version}-export-{export.id}",
        )
    except HTTPException:
        raise
    except Exception as exc:
        service_error = None
        try:
            from quictrain_api.errors import ServiceError as service_error
        except Exception:
            service_error = None
        if service_error is not None and isinstance(exc, service_error):
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        raise HTTPException(status_code=503, detail="训练控制面不可用") from exc
    return success(registered)
