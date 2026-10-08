"""Official QRDF materialization for the new Batch / Episode domain."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from qrdf.models.dataset import DatasetManifest
from qrdf.models.episode import AnnotationInfo, EpisodeMetadata, TaskInfo
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from data.database import Episode, EpisodeArtifact, JobRun, PublishedEpisode, Workspace
from data.infra import oss_client
from data.integrations.qrdf.ego import write_ego_interval_episode
from data.integrations.qrdf.paths import resolve_qrdf_episode_data_file
from data.integrations.qrdf.service import validate_dataset
from data.realtime.outbox import enqueue_resource_event, enqueue_work_queue_invalidated
from data.realtime.projections import episode_snapshot
from data.services.artifact_operations import (
    complete_artifact_operation,
    ensure_artifact_operation,
    materialize_artifact_operation,
)
from data.services.batch_paths import official_episode_publication_prefix
from data.services.publication_source_cache import (
    PublicationSourceFacts,
    acquire_publication_source,
)
from data.services.storage_mode import uses_cloud_uri_authority
from data.utils.checksums import tree_sha256
from data.utils.storage_paths import is_under_storage_root, storage_root_path


def publish_batch_episode(db: Session, job: JobRun) -> dict[str, object]:
    """Retired legacy publication worker.

    Historical rows remain readable, while new exports are produced by the
    catalog/export providers.  Keeping this guard at the worker boundary also
    prevents a stale queue message from writing an ``official`` object.
    """
    raise RuntimeError("episode_publish is retired; use catalog export artifacts")
    # pragma: no cover - retained below for historical migration readers.
    if job.kind != "episode_publish" or job.resource_type != "episode":
        raise ValueError("episode publication job resource is invalid")
    try:
        episode_id = int(job.resource_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("episode publication episode id is invalid") from exc
    episode = db.scalar(select(Episode).where(Episode.id == episode_id).with_for_update())
    if episode is None:
        raise ValueError("episode does not exist")
    if episode.review_status != "accepted":
        raise ValueError("only accepted episodes can be published")
    if job.actor_id is None:
        raise ValueError("publication actor is unavailable")

    existing = _published_episode_for_episode(db, episode_id=episode.id)
    if existing is not None:
        return {
            "episode_id": episode.id,
            "published_episode_id": existing.id,
            "official_artifact_id": existing.official_artifact_id,
        }

    raw_episode = episode
    if episode.kind == "derived":
        if episode.parent is None:
            raise ValueError("derived episode source is unavailable")
        raw_episode = episode.parent
    raw_artifact = db.scalar(
        select(EpisodeArtifact)
        .where(
            EpisodeArtifact.episode_id == raw_episode.id,
            EpisodeArtifact.artifact_type == "raw_source",
        )
        .order_by(EpisodeArtifact.id.asc())
    )
    if raw_artifact is None:
        raise ValueError("raw source artifact is unavailable")

    stage = _publication_stage(job.id)
    if stage.exists():
        _remove_stage(stage)
    stage.mkdir(parents=True, exist_ok=False)
    try:
        dataset_root = stage / "dataset"
        dataset_root.mkdir()
        output_id = "episode_000001"
        output_episode_dir = dataset_root / "episodes" / output_id
        facts = PublicationSourceFacts.from_artifact(raw_artifact)
        with acquire_publication_source(facts) as source_dir:
            _write_output_episode(episode, source_dir, output_episode_dir, output_id)

        manifest = DatasetManifest.create_default(
            dataset_root, dataset_name=f"episode-{episode.id}"
        )
        manifest.domains = [str(episode.modality or "qrdf")]
        manifest.tasks = [episode.task_language] if episode.task_language else []
        manifest.add_episode(output_id, split="train")
        manifest.save(dataset_root / "dataset.json")
        publication_id = _publication_id(episode.id)
        (dataset_root / "quicdata_episode_provenance.json").write_text(
            json.dumps(
                {
                    "schema": "quicdata.batch_episode_provenance.v1",
                    "episode_id": episode.id,
                    "episode_uid": episode.episode_uid,
                    "source_episode_id": raw_episode.id,
                    "publication_id": publication_id,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        report = validate_dataset(str(dataset_root))
        if int(report.get("error_count", 0)):
            raise ValueError("official QRDF validation failed")

        publication_prefix = official_episode_publication_prefix(
            workspace_id=episode.workspace_id,
            task_set_id=episode.task_set_id,
            episode_id=episode.id,
            publication_id=publication_id,
        )
        storage_uri = (
            f"oss://{oss_client.bucket_name('official')}/{publication_prefix}"
            if uses_cloud_uri_authority()
            else f"nas://{publication_prefix}"
        )
        artifact = db.scalar(
            select(EpisodeArtifact).where(
                EpisodeArtifact.episode_id == episode.id,
                EpisodeArtifact.artifact_type == "official_qrdf",
                EpisodeArtifact.storage_uri == storage_uri,
            )
        )
        if artifact is None:
            package_checksum = tree_sha256(dataset_root)
            artifact = EpisodeArtifact(
                episode_id=episode.id,
                artifact_type="official_qrdf",
                storage_role="official",
                storage_uri=storage_uri,
                checksum_sha256=package_checksum,
                size_bytes=_tree_size(dataset_root),
                manifest_hash=package_checksum,
                retention_policy="permanent",
                metadata_json={
                    "output_profile": "qrdf_episode_v1",
                    "publication_id": publication_id,
                },
            )
            db.add(artifact)
            try:
                db.flush()
            except IntegrityError as exc:
                db.rollback()
                existing = _published_episode_for_episode(db, episode_id=episode_id)
                if existing is not None:
                    return _published_result(existing)
                raise ValueError("publication artifact is already being materialized") from exc
        operation = ensure_artifact_operation(
            db,
            artifact=artifact,
            job=job,
            operation_kind="official_publish",
            source_path=dataset_root,
        )
        # The artifact and its write intent must survive a worker crash before
        # the external object operation begins.
        db.commit()
        materialize_artifact_operation(db, operation_id=operation.id, source_path=dataset_root)
        complete_artifact_operation(db, operation_id=operation.id)
        published = PublishedEpisode(
            episode_id=episode.id,
            official_artifact_id=artifact.id,
            publisher_user_id=job.actor_id,
            output_profile="qrdf_episode_v1",
            manifest_hash=artifact.manifest_hash,
        )
        db.add(published)
        episode.workflow_status = "published"
        workspace = db.get(Workspace, episode.workspace_id)
        if workspace is None:
            raise ValueError("publication workspace is unavailable")
        enqueue_resource_event(
            db,
            resource=episode,
            resource_type="episode",
            event_name="episode.updated",
            resource_snapshot=episode_snapshot(episode),
        )
        enqueue_work_queue_invalidated(db, workspace=workspace)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            existing = _published_episode_for_episode(db, episode_id=episode_id)
            if existing is not None:
                return _published_result(existing)
            raise ValueError("publication finalization conflicted") from exc
        return _published_result(published)
    finally:
        _remove_stage(stage)


def _publication_id(episode_id: int) -> str:
    return f"episode-{episode_id}"


def _published_episode_for_episode(db: Session, *, episode_id: int) -> PublishedEpisode | None:
    return db.scalar(
        select(PublishedEpisode)
        .where(PublishedEpisode.episode_id == episode_id)
        .order_by(PublishedEpisode.id.desc())
    )


def _published_result(published: PublishedEpisode) -> dict[str, object]:
    return {
        "episode_id": published.episode_id,
        "published_episode_id": published.id,
        "official_artifact_id": published.official_artifact_id,
    }


def _write_output_episode(
    episode: Episode, source_dir: Path, destination: Path, output_id: str
) -> None:
    metadata = EpisodeMetadata.load(source_dir / "metadata.json")
    if episode.kind == "derived":
        if episode.source_start_ns is None or episode.source_end_ns is None:
            raise ValueError("derived episode interval is unavailable")
        write_ego_interval_episode(
            source_dir,
            destination,
            output_episode_id=output_id,
            start_ns=int(episode.source_start_ns),
            end_ns=int(episode.source_end_ns),
            task_language=episode.task_language or None,
        )
        metadata = EpisodeMetadata.load(destination / "metadata.json")
    else:
        source_data = resolve_qrdf_episode_data_file(metadata, source_dir)
        destination.mkdir(parents=True, exist_ok=False)
        target_data = destination / metadata.data_file
        target_data.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_data, target_data)
    _apply_publication_metadata(
        metadata,
        output_id=output_id,
        task_language=str(episode.task_language or "").strip(),
    )
    metadata.save(destination / "metadata.json")
    annotation = _latest_annotation_payload(episode)
    (destination / "annotation.json").write_text(
        json.dumps(
            {
                "schema": "quicdata.batch_annotation.v1",
                "version": annotation[0],
                "task_language": episode.task_language,
                "payload": annotation[1],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _apply_publication_metadata(
    metadata: EpisodeMetadata,
    *,
    output_id: str,
    task_language: str,
) -> None:
    """Apply reviewed Batch/Episode labels to the published QRDF metadata."""
    metadata.episode_id = output_id
    if metadata.task is None and task_language:
        metadata.task = TaskInfo(name="batch_episode", language=task_language)
    elif metadata.task is not None:
        metadata.task.language = task_language or None
    metadata.annotation = AnnotationInfo(language=task_language or None)


def _latest_annotation_payload(episode: Episode) -> tuple[int, dict[str, object]]:
    annotation = max(
        episode.annotations or [], key=lambda item: int(item.version or 0), default=None
    )
    if annotation is None or not isinstance(annotation.payload_json, dict):
        return 0, {}
    return int(annotation.version or 0), dict(annotation.payload_json)


def _publication_stage(job_id: str) -> Path:
    if not job_id.isalnum() or len(job_id) > 64:
        raise ValueError("publication job id is invalid")
    stage = storage_root_path() / "process" / "publication" / ".tmp" / job_id
    if not is_under_storage_root(stage):
        raise ValueError("publication stage is outside storage root")
    return stage


def _remove_stage(stage: Path) -> None:
    if stage.exists() or stage.is_symlink():
        if stage.is_symlink() or stage.parent.name != ".tmp":
            raise ValueError("publication cleanup target is invalid")
        shutil.rmtree(stage)


def _tree_size(root: Path) -> int:
    return sum(
        item.stat().st_size for item in root.rglob("*") if item.is_file() and not item.is_symlink()
    )
