"""Workspace-scoped operations and quality aggregates."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from data.database import get_db
from data.services.operations_projection import build_operations_summary
from data.services.workspace_access import require_workspace_actor
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/operations", tags=["operations"])


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub") or user.get("id"))
    except (TypeError, ValueError):
        return None


@router.get("/summary")
def get_operations_summary(
    workspace_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Return safe, aggregate facts for one authorized workspace."""
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=403, detail="operations summary requires admin or operator role"
        )
    require_permission(user, "workspace:read")
    try:
        require_workspace_actor(db, actor_id=_actor_id(user), workspace_id=workspace_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return success(build_operations_summary(db, workspace_id=workspace_id))
