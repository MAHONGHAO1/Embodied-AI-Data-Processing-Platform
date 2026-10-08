"""Authorized, complete manifests with frozen batch/work-item source identities."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath

from sqlalchemy.orm import Session

from data.config import settings
from data.database import Episode
from data.infra.object_storage import StorageNotReady, StorageObjectRef
from data.infra.storage_provider import get_storage_provider
from data.models.annotation_work import AnnotationWorkItem
from data.models.collection_core import CollectionTask
from data.models.data_batch import DataBatch, DataBatchEpisode
from data.models.data_package import DataPackage
from data.models.episode_admission import EpisodeAdmissionFact
from data.utils.storage_paths import cloud_path_is_safe
from data.utils.storage_uri import parse_storage_uri

SUPPORTED_SCOPES = frozenset(
    {"data_packages", "collection_task", "data_batch", "my_annotation_work_items"}
)

_MIME_BY_KIND = {
    "data": "application/octet-stream",
    "metadata": "application/json",
    "admission_report": "application/json",
    "preview_manifest": "application/json",
    "preview_timeline": "application/json",
    "preview_video": "video/mp4",
}


def _scope_members(db, *, workspace_id, scope, ids, actor_id):
    model = {
        "data_packages": DataPackage,
        "collection_task": CollectionTask,
        "data_batch": DataBatch,
        "my_annotation_work_items": AnnotationWorkItem,
    }[scope]
    rows = db.query(model).filter(model.workspace_id == workspace_id, model.id.in_(ids)).all()
    if {r.id for r in rows} != set(ids):
        raise LookupError("fetch_resource_not_found")
    if scope == "my_annotation_work_items":
        if any(r.assignee_user_id != actor_id for r in rows):
            raise PermissionError("work_item_not_assigned_to_actor")
        return [
            (int(m["episode_id"]), m.get("admission_attempt"))
            for r in rows
            for m in r.episode_members_json or []
        ], True
    if scope == "data_batch":
        members = db.query(DataBatchEpisode).filter(DataBatchEpisode.data_batch_id.in_(ids)).all()
        return [(m.episode_id, m.admission_attempt) for m in members], True
    query = db.query(Episode.id).filter(Episode.workspace_id == workspace_id)
    if scope == "data_packages":
        query = query.filter(Episode.data_package_id.in_(ids))
    else:
        packages = db.query(DataPackage.id).filter(
            DataPackage.workspace_id == workspace_id, DataPackage.collection_task_id.in_(ids)
        )
        query = query.filter(Episode.data_package_id.in_(packages))
    return [(r[0], None) for r in query.all()], False


def _provider(network):
    endpoint = (
        settings.storage_endpoint
        if network == "internal"
        else (settings.storage_browser_endpoint or settings.oss_browser_endpoint)
    )
    if not endpoint or not endpoint.strip():
        raise StorageNotReady(f"oss_{network}_endpoint_not_configured")
    # Build the signing client against the selected endpoint, never rewrite signed URLs.
    cfg = settings.model_copy(update={"storage_browser_endpoint": endpoint})
    return get_storage_provider(cfg)


def build_fetch_manifest(
    db: Session,
    *,
    workspace_id: int,
    scope: str,
    ids: list[int],
    actor_id: int | None,
    url_ttl_seconds: int = 3600,
    oss_network: str = "internal",
) -> dict[str, object]:
    if scope not in SUPPORTED_SCOPES or oss_network not in {"internal", "public"}:
        raise ValueError("unsupported_fetch_scope_or_network")
    if not 1 <= url_ttl_seconds <= 604800:
        raise ValueError("url_ttl_seconds_out_of_range")
    members, frozen = _scope_members(
        db, workspace_id=workspace_id, scope=scope, ids=ids, actor_id=actor_id
    )
    provider = _provider(oss_network)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=url_ttl_seconds)).isoformat()
    episodes = {
        e.id: e
        for e in db.query(Episode)
        .filter(Episode.workspace_id == workspace_id, Episode.id.in_([m[0] for m in members]))
        .all()
    }
    objects = []
    seen = set()
    for episode_id, attempt in sorted(set(members), key=lambda m: (m[0], m[1] or 0)):
        episode = episodes.get(episode_id)
        if episode is None:
            raise LookupError("frozen_episode_not_found")
        facts = db.query(EpisodeAdmissionFact).filter(EpisodeAdmissionFact.episode_id == episode_id)
        fact = (
            facts.filter(EpisodeAdmissionFact.attempt == attempt)
            if frozen
            else facts.filter(EpisodeAdmissionFact.is_current.is_(True))
        ).one_or_none()
        from data.services.episode_objects import (
            EpisodeObjectsError,
            fact_objects,
            safe_episode_path,
        )

        try:
            episode_objects = fact_objects(fact) if fact else []
        except EpisodeObjectsError as exc:
            raise ValueError("frozen_source_identity_missing") from exc
        if frozen and (attempt is None or not episode_objects):
            raise ValueError("frozen_source_identity_missing")
        sources = [
            (
                item.storage_ref(),
                _MIME_BY_KIND.get(item.kind, "application/octet-stream"),
                item.path,
            )
            for item in episode_objects
        ]
        if not sources and not frozen:
            # Legacy/unreviewed packages use every registered source artifact, not the first artifact.
            seen_relative_paths: set[str] = set()
            for artifact in episode.artifacts:
                if artifact.storage_role != "raw":
                    continue
                bucket, key = parse_storage_uri(artifact.storage_uri)
                if bucket != settings.oss_bucket_raw or not cloud_path_is_safe(bucket, key):
                    raise ValueError("source_object_out_of_scope")
                meta = artifact.metadata_json or {}
                try:
                    relative_path = safe_episode_path(
                        str(meta.get("relative_path") or PurePosixPath(key).name)
                    )
                except EpisodeObjectsError as exc:
                    raise ValueError("episode_object_path_unsafe") from exc
                if relative_path in seen_relative_paths:
                    raise ValueError("episode_object_path_duplicate")
                seen_relative_paths.add(relative_path)
                sources.append(
                    (
                        StorageObjectRef(
                            "raw",
                            key,
                            meta.get("version_id"),
                            str(meta.get("etag") or ""),
                            int(artifact.size_bytes or 0),
                            artifact.checksum_sha256,
                        ),
                        meta.get("media_type", "application/octet-stream"),
                        relative_path,
                    )
                )
        if not sources:
            raise ValueError("episode_source_files_missing")
        package = db.get(DataPackage, episode.data_package_id)
        task = package.task if package else None
        metadata = episode.metadata_json or {}
        for ref, mime, path in sources:
            identity = (episode_id, ref.bucket_role, ref.object_key, ref.version_id, ref.etag)
            if identity in seen:
                continue
            seen.add(identity)
            url = provider.sign_get(ref, expires=url_ttl_seconds)
            headers = {"If-Match": ref.etag} if ref.etag and not ref.version_id else {}
            objects.append(
                {
                    "episode_uid": episode.episode_uid,
                    "key": provider.object_uri(ref),
                    "size": ref.size_bytes,
                    "sha256": ref.sha256 or "",
                    "mime": mime,
                    "url": url,
                    "headers": headers,
                    "available": True,
                    "expires_at": expires_at,
                    "version_id": ref.version_id,
                    "path": path,
                    "episode": {
                        "id": episode.id,
                        "kind": episode.kind,
                        "modality": episode.modality,
                        "data_package_id": episode.data_package_id,
                        "timing": metadata.get("timing", {}),
                        "metadata": metadata,
                        "topics": metadata.get("topics", []),
                        "task_label": task.name if task else None,
                        "collector_id": package.operator_collector_id if package else None,
                        "device_id": package.collection_device_id if package else None,
                    },
                }
            )
    fingerprint = hashlib.sha256(
        json.dumps(
            [{k: v for k, v in o.items() if k not in {"url", "expires_at"}} for o in objects],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return {
        "manifest_version": fingerprint,
        "expires_at": expires_at,
        "oss_network": oss_network,
        "objects": objects,
    }
