"""Read-only Episode-grain projection of the collection package lifecycle."""

from sqlalchemy import String, and_, case, cast, func

from data.database import Episode
from data.models.annotation_work import (
    AnnotationSubmission,
    AnnotationSubmissionReview,
    AnnotationWorkItem,
    ReviewWorkItem,
)
from data.models.data_asset import DataAsset
from data.models.data_batch import DataBatch, DataBatchEpisode
from data.models.data_package import DataPackage, PackageIntakeReview
from data.services.collection_duration import accepted_intake_episode_sql, latest_intake_review_ids


def context_updated_at():
    return func.greatest(
        Episode.updated_at,
        DataPackage.updated_at,
        PackageIntakeReview.reviewed_at,
        DataBatchEpisode.created_at,
        DataBatch.updated_at,
        AnnotationWorkItem.updated_at,
        AnnotationSubmission.created_at,
        ReviewWorkItem.updated_at,
        AnnotationSubmissionReview.created_at,
        DataAsset.created_at,
    )


def package_published_at():
    return case(
        (
            DataAsset.episode_ids_json.op("@>")(func.jsonb_build_array(Episode.id)),
            DataAsset.created_at,
        )
    )


def context_revision():
    """Cache invalidation token, independent of ordering between source clocks.

    A future timestamp on one dependency must not hide changes to another.
    IDs also detect replacement/removal, and immutable review/asset contents
    are bound by their row identity. This checksum is not an authorization key.
    """
    columns = (
        Episode.id,
        Episode.updated_at,
        Episode.validity_status,
        DataPackage.id,
        DataPackage.updated_at,
        DataPackage.status,
        PackageIntakeReview.id,
        PackageIntakeReview.reviewed_at,
        DataBatchEpisode.id,
        DataBatchEpisode.created_at,
        DataBatch.id,
        DataBatch.updated_at,
        DataBatch.status,
        AnnotationWorkItem.id,
        AnnotationWorkItem.updated_at,
        AnnotationWorkItem.status,
        AnnotationWorkItem.current_submission_id,
        AnnotationWorkItem.generation,
        AnnotationSubmission.id,
        AnnotationSubmission.created_at,
        ReviewWorkItem.id,
        ReviewWorkItem.updated_at,
        ReviewWorkItem.status,
        ReviewWorkItem.submission_id,
        ReviewWorkItem.generation,
        AnnotationSubmissionReview.id,
        AnnotationSubmissionReview.created_at,
        DataAsset.id,
        DataAsset.created_at,
    )
    return func.md5(
        func.concat_ws("|", *(func.coalesce(cast(column, String), "") for column in columns))
    )


def episode_context_query(db):
    """All joins have at most one row per Episode; never join by package alone to a batch."""
    latest_review = latest_intake_review_ids()
    return (
        db.query(
            Episode,
            DataPackage,
            PackageIntakeReview,
            DataBatchEpisode,
            DataBatch,
            AnnotationWorkItem,
            AnnotationSubmission,
            ReviewWorkItem,
            AnnotationSubmissionReview,
            DataAsset,
            context_updated_at().label("context_updated_at"),
            accepted_intake_episode_sql().label("intake_accepted"),
            context_revision().label("source_revision"),
        )
        .select_from(Episode)
        .outerjoin(DataPackage, DataPackage.id == Episode.data_package_id)
        .outerjoin(latest_review, latest_review.c.data_package_id == DataPackage.id)
        .outerjoin(PackageIntakeReview, PackageIntakeReview.id == latest_review.c.review_id)
        .outerjoin(DataBatchEpisode, DataBatchEpisode.episode_id == Episode.id)
        .outerjoin(DataBatch, DataBatch.id == DataBatchEpisode.data_batch_id)
        .outerjoin(
            AnnotationWorkItem,
            and_(
                AnnotationWorkItem.data_batch_id == DataBatch.id,
                AnnotationWorkItem.data_package_id == Episode.data_package_id,
            ),
        )
        .outerjoin(
            AnnotationSubmission,
            AnnotationSubmission.id == AnnotationWorkItem.current_submission_id,
        )
        .outerjoin(ReviewWorkItem, ReviewWorkItem.annotation_work_item_id == AnnotationWorkItem.id)
        .outerjoin(
            AnnotationSubmissionReview,
            AnnotationSubmissionReview.submission_id == AnnotationSubmission.id,
        )
        .outerjoin(DataAsset, DataAsset.data_batch_id == DataBatch.id)
        .populate_existing()
    )


def project_package_episode(context, *, duration_s):
    (
        episode,
        package,
        intake_review,
        member,
        batch,
        item,
        submission,
        review,
        decision,
        asset,
        updated_at,
        intake_accepted,
        source_revision,
    ) = context
    if package is None or episode.kind != "source":
        return {}
    result = {
        "source_updated_at": updated_at,
        "source_revision": source_revision,
        "intake_valid_duration_s": duration_s if intake_accepted else 0.0,
        "annotation_effective_duration_ns": None,
        "annotation_eligible": item is not None and member is not None,
        "is_qrdf_baseline": False,
        "published_at": None,
        "annotation_status": "pending",
        "review_status": "pending",
        "workflow_status": "intake_pending",
        "pipeline_stage": "collected",
        "queue_key": "collected",
    }

    def finish(workflow, stage, queue=None):
        result.update(workflow_status=workflow, pipeline_stage=stage, queue_key=queue)
        return result

    # A published asset is immutable, including its exact Episode membership
    # and effective ranges. A mutable draft can never replace this evidence.
    if asset is not None and episode.id in (asset.episode_ids_json or []):
        entry = next(
            (
                entry
                for entry in (asset.source_snapshot_json or {}).get("episodes", [])
                if entry.get("episode_id") == episode.id
            ),
            {},
        )
        result.update(is_qrdf_baseline=True, published_at=asset.created_at)
        if entry.get("annotation_submission_id") is not None:
            result.update(
                annotation_status="accepted",
                review_status="accepted",
                annotation_eligible=True,
                annotation_effective_duration_ns=int(entry["effective_duration_ns"]),
            )
        return finish("published", "stored")

    entry = (
        next(
            (
                entry
                for entry in (submission.episodes_json or [])
                if entry.get("episode_id") == episode.id
                and member is not None
                and entry.get("admission_attempt") == member.admission_attempt
            ),
            None,
        )
        if submission is not None
        else None
    )
    approved = (
        entry is not None
        and item.status == "done"
        and review is not None
        and review.status == "approved"
        and review.submission_id == submission.id
        and decision is not None
        and decision.decision == "approved"
        and submission.annotation_work_item_id == item.id
        and submission.workspace_id == episode.workspace_id
    )
    if approved:
        result.update(
            annotation_status="accepted",
            review_status="accepted",
            annotation_effective_duration_ns=int(entry["effective_duration_ns"]),
        )
        if entry["conclusion"] == "no_valid_segments":
            return finish("no_valid_segments", "annotated")
        return finish("annotation_approved", "annotated", "stored")

    if episode.validity_status in {"intake_rejected", "qc_dropped"} or package.status == "voided":
        result["annotation_eligible"] = False
        return finish(
            episode.validity_status if episode.validity_status != "valid" else "intake_rejected",
            "collected",
        )
    if item is not None:
        if item.status == "submitted":
            result["annotation_status"] = "submitted"
            return finish("review_pending", "annotated", "annotated")
        return finish("annotation_pending", "separated", "separated")
    if batch is not None:
        # A terminal batch without membership in its published asset has no
        # pending work. Its source still contributes captured/intake duration.
        if batch.status in {"published", "no_publishable_asset"}:
            return finish("no_publishable_asset", "collected")
        return finish("governing", "collected", "collected")
    if intake_accepted:
        return finish("intake_approved", "collected", "collected")
    if intake_review is not None and episode.id in (intake_review.rejected_episode_ids_json or []):
        return finish("intake_rejected", "collected")
    if intake_review is not None and any(
        entry.get("episode_id") == episode.id
        for entry in intake_review.excluded_episodes_json or []
    ):
        return finish("intake_excluded", "collected")
    return result
