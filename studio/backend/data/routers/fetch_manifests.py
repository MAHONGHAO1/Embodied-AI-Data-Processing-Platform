"""Fetch manifests for external tools that need the underlying data."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from data.database import get_db
from data.infra.object_storage import ObjectStorageError
from data.security.audit import emit_audit_event
from data.services.fetch_manifests import build_fetch_manifest
from data.services.workspace_access import require_workspace_actor
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/fetch-manifests", tags=["取数清单"])


class FetchManifestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    scope: Literal["data_packages", "collection_task", "data_batch", "my_annotation_work_items"]
    ids: list[int] = Field(min_length=1, max_length=200)
    url_ttl_seconds: int = Field(default=3600, ge=1, le=604800, strict=True)
    oss_network: Literal["internal", "public"] = "internal"


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub"))
    except (TypeError, ValueError):
        return None


@router.post("")
def create_fetch_manifest(
    body: FetchManifestRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Return signed object entries for one batch of Episodes."""

    require_permission(user, "episode:read")
    actor_id = _actor_id(user)
    try:
        require_workspace_actor(db, actor_id=actor_id, workspace_id=body.workspace_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        manifest = build_fetch_manifest(
            db,
            workspace_id=body.workspace_id,
            scope=body.scope,
            ids=body.ids,
            actor_id=actor_id,
            url_ttl_seconds=body.url_ttl_seconds,
            oss_network=body.oss_network,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ObjectStorageError as exc:
        raise HTTPException(status_code=503, detail="fetch_storage_unavailable") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    emit_audit_event(
        "fetch_manifest.read",
        actor=str(user.get("email") or ""),
        resource=f"workspace:{body.workspace_id}",
        detail={
            "scope": body.scope,
            "requested": len(body.ids),
            "objects": len(manifest["objects"]),
        },
    )
    return success(manifest)
