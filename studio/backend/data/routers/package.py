"""Data package multidimensional query (4.2 package/query)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from data.database import get_db
from data.schemas.common import PackageQuery
from data.services.query_service import search_packages
from data.services.workspace_access import require_project_actor, require_workspace_actor
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/package", tags=["数据包"])


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub") or user.get("id"))
    except (TypeError, ValueError):
        return None


def _authorize_filters(db: Session, user: dict, filters: dict) -> None:
    try:
        if filters.get("workspace_id"):
            require_workspace_actor(
                db, actor_id=_actor_id(user), workspace_id=int(filters["workspace_id"])
            )
        if filters.get("project_id"):
            require_project_actor(
                db, actor_id=_actor_id(user), project_id=int(filters["project_id"])
            )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="workspace access denied") from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="workspace does not exist") from exc


@router.post("/query")
def query_packages(
    body: PackageQuery,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "qrdf:read")
    filters = body.filters.model_dump(exclude_none=True)
    _authorize_filters(db, user, filters)
    data = search_packages(db, filters, body.page, body.size, actor_id=_actor_id(user))
    return success(data)
