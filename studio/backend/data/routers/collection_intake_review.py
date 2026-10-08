"""Data package intake review API."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from data.database import get_db
from data.models.data_package import PackageIntakeReview
from data.services.collection_access import require_collection_workspace
from data.services.collection_intake_review import (
    IntakeReviewConflictError,
    bulk_approve_data_package_intake,
    review_data_package_intake,
)
from data.services.intake_review_drafts import get_draft, save_draft
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/data-packages", tags=["数据包"])


class IntakeReviewRequest(BaseModel):
    """Request parameters for single data package intake review."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    verdict: Literal["approved", "rejected"]
    rejected_episode_ids: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list)
    reason: str = Field(default="", max_length=1000)
    base_version: int | None = Field(default=None, ge=1)
    source_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    episode_reasons: dict[str, str] | None = None

    @model_validator(mode="after")
    def validate_verdict_fields(self):
        self.reason = self.reason.strip()
        if self.verdict == "rejected" and not self.reason:
            raise ValueError("reason is required when verdict is rejected")
        supplied = (
            self.base_version is not None,
            self.source_fingerprint is not None,
            self.episode_reasons is not None,
        )
        if any(supplied) and not all(supplied):
            raise ValueError(
                "new intake review requires draft version, source fingerprint and episode reasons"
            )
        if self.episode_reasons is not None:
            self.episode_reasons = {
                key: value.strip() for key, value in self.episode_reasons.items()
            }
            if any(len(value) > 1000 for value in self.episode_reasons.values()):
                raise ValueError("episode reason must be at most 1000 characters")
        return self


class BulkIntakeApproveRequest(BaseModel):
    """Request parameters for bulk approving data package intake."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    data_package_ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_package_ids(self):
        if len(set(self.data_package_ids)) != len(self.data_package_ids):
            raise ValueError("data_package_ids must be unique")
        return self


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub"))
    except (TypeError, ValueError):
        return None


def _decimal(value: Decimal) -> str:
    return f"{Decimal(value):.2f}"


def _latest_review(db: Session, package_id: int) -> PackageIntakeReview:
    review = (
        db.query(PackageIntakeReview)
        .filter(PackageIntakeReview.data_package_id == package_id)
        .order_by(
            PackageIntakeReview.reviewed_at.desc(),
            PackageIntakeReview.id.desc(),
        )
        .first()
    )
    if review is None:
        raise LookupError("intake review does not exist")
    return review


@router.post("/intake-review/bulk-approve")
def bulk_approve_package_intake(
    body: BulkIntakeApproveRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Bulk approve intake for validated data packages."""
    require_permission(user, "workspace:write")
    actor_id = _actor_id(user)
    try:
        require_collection_workspace(
            db,
            actor_id=actor_id,
            workspace_id=body.workspace_id,
        )
        packages = bulk_approve_data_package_intake(
            db,
            workspace_id=body.workspace_id,
            data_package_ids=body.data_package_ids,
            reviewer_user_id=actor_id,
        )
        db.commit()
    except PermissionError as exc:
        db.rollback()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except IntakeReviewConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return success(
        {
            "approved_count": len(packages),
            "packages": [
                {
                    "data_package_id": package.id,
                    "accepted_episode_ids": list(
                        _latest_review(db, package.id).accepted_episode_ids_json or []
                    ),
                    "excluded_episodes": list(
                        _latest_review(db, package.id).excluded_episodes_json or []
                    ),
                }
                for package in packages
            ],
        }
    )


@router.post("/{data_package_id}/intake-review")
def review_package_intake(
    data_package_id: int,
    body: IntakeReviewRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Perform intake review for a single data package (approve or reject)."""
    require_permission(user, "workspace:write")
    actor_id = _actor_id(user)
    try:
        require_collection_workspace(
            db,
            actor_id=actor_id,
            workspace_id=body.workspace_id,
        )
        package, _review = review_data_package_intake(
            db,
            workspace_id=body.workspace_id,
            data_package_id=data_package_id,
            reviewer_user_id=actor_id,
            verdict=body.verdict,
            rejected_episode_ids=body.rejected_episode_ids,
            reason=body.reason,
            base_version=body.base_version,
            source_fingerprint=body.source_fingerprint,
            episode_reasons=body.episode_reasons,
        )
        db.commit()
        db.refresh(package)
    except PermissionError as exc:
        db.rollback()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except IntakeReviewConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return success(
        {
            "id": package.id,
            "status": package.status,
            "intake_valid_duration_hours": _decimal(package.intake_valid_duration_hours),
            "accepted_episode_ids": list(_review.accepted_episode_ids_json or []),
            "rejected_episode_ids": list(_review.rejected_episode_ids_json or []),
            "excluded_episodes": list(_review.excluded_episodes_json or []),
        }
    )


class IntakeDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: int = Field(gt=0)
    base_version: int = Field(ge=0)
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    draft: dict


def _draft_error(exc):
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, IntakeReviewConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@router.get("/{data_package_id}/intake-review-draft")
def get_intake_review_draft(
    data_package_id: int,
    workspace_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    actor = _actor_id(user)
    try:
        require_collection_workspace(db, actor_id=actor, workspace_id=workspace_id)
        result = get_draft(
            db, workspace_id=workspace_id, package_id=data_package_id, reviewer_id=actor
        )
        db.commit()
        return success(result)
    except (PermissionError, LookupError, ValueError) as exc:
        db.rollback()
        raise _draft_error(exc) from exc


@router.patch("/{data_package_id}/intake-review-draft")
def save_intake_review_draft(
    data_package_id: int,
    body: IntakeDraftRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    actor = _actor_id(user)
    try:
        require_collection_workspace(db, actor_id=actor, workspace_id=body.workspace_id)
        result = save_draft(
            db,
            workspace_id=body.workspace_id,
            package_id=data_package_id,
            reviewer_id=actor,
            base_version=body.base_version,
            source_fingerprint=body.source_fingerprint,
            draft=body.draft,
        )
        db.commit()
        return success(result)
    except (PermissionError, LookupError, ValueError) as exc:
        db.rollback()
        raise _draft_error(exc) from exc
