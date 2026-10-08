"""Self-service API tokens for external tools."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from data.database import get_db
from data.models.api_token import ApiToken
from data.security.audit import emit_audit_event
from data.services.api_tokens import TokenError, issue_token, revoke_token, rotate_token
from data.utils.helpers import get_current_user, success

router = APIRouter(prefix="/tokens", tags=["外部凭据"])


class IssueTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    expires_in_days: int | None = Field(default=90, ge=1, le=3650)


def _actor_id(user: dict) -> int:
    try:
        actor_id = int(user.get("sub"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="invalid session") from exc
    return actor_id


def _token_view(row: ApiToken) -> dict[str, object]:
    return {
        "id": row.id,
        "name": row.name,
        "key_id": row.key_id,
        "expires_at": row.expires_at,
        "rotated_at": row.rotated_at,
        "last_used_at": row.last_used_at,
        "revoked_at": row.revoked_at,
        "created_at": row.created_at,
    }


def _own_token(db: Session, *, token_id: int, actor_id: int) -> ApiToken:
    row = db.get(ApiToken, token_id)
    # Tokens never cross users: hide other people's tokens behind a 404.
    if row is None or int(row.user_id) != int(actor_id):
        raise HTTPException(status_code=404, detail="token does not exist")
    return row


@router.post("")
def issue_token_endpoint(
    body: IssueTokenRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Issue a token for the caller; the plaintext secret is returned once."""

    actor_id = _actor_id(user)
    try:
        issued = issue_token(
            db,
            user_id=actor_id,
            name=body.name,
            expires_in_days=body.expires_in_days,
            created_by=actor_id,
        )
    except Exception as exc:  # unique (user_id, name) or storage errors
        db.rollback()
        raise HTTPException(status_code=409, detail="token name already exists") from exc
    db.commit()
    emit_audit_event(
        "api_token.issue",
        actor=str(user.get("email") or ""),
        resource=f"api_token:{issued['id']}",
        detail={"name": issued["name"]},
    )
    return success(issued)


@router.get("")
def list_tokens_endpoint(
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    actor_id = _actor_id(user)
    rows = (
        db.query(ApiToken)
        .filter(ApiToken.user_id == actor_id)
        .order_by(ApiToken.created_at.desc(), ApiToken.id.desc())
        .all()
    )
    return success({"items": [_token_view(row) for row in rows]})


@router.post("/{token_id}/rotate")
def rotate_token_endpoint(
    token_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    actor_id = _actor_id(user)
    row = _own_token(db, token_id=token_id, actor_id=actor_id)
    try:
        rotated = rotate_token(db, token_id=row.id)
    except TokenError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=exc.code) from exc
    db.commit()
    emit_audit_event(
        "api_token.rotate",
        actor=str(user.get("email") or ""),
        resource=f"api_token:{row.id}",
        detail={"name": row.name, "rotated_at": datetime.utcnow().isoformat()},
    )
    return success(rotated)


@router.delete("/{token_id}")
def revoke_token_endpoint(
    token_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    actor_id = _actor_id(user)
    row = _own_token(db, token_id=token_id, actor_id=actor_id)
    revoke_token(db, token_id=row.id)
    db.commit()
    emit_audit_event(
        "api_token.revoke",
        actor=str(user.get("email") or ""),
        resource=f"api_token:{row.id}",
        detail={"name": row.name},
    )
    return success(_token_view(row))
