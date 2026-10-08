"""Workspace-scoped collection attribution and immutable provenance snapshots."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import chain

from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from data.database import (
    CollectionRecord,
    CollectionSource,
    EgoEpisode,
    PersonnelProfile,
    Task,
    User,
    Workspace,
)
from data.services.workspace_access import require_task_actor
from data.services.workspace_scope import resolve_task_workspace_id, task_workspace_filter

LEGACY_PROFILE_KEY = "legacy_import_needs_attribution"
LEGACY_SOURCE_KEY = "legacy_import_needs_attribution"
LEGACY_DISPLAY_NAME = "legacy import / needs attribution"
COLLECTOR_PROFILE_KEY_WIDTH = 4
COLLECTOR_PROFILE_KEY_CAPACITY = 10**COLLECTOR_PROFILE_KEY_WIDTH
COLLECTOR_PROFILE_KEY_RETRIES = 64


@dataclass(frozen=True)
class LegacyAttributionResult:
    profile: PersonnelProfile
    source: CollectionSource
    affected_task_ids: tuple[int, ...]

    @property
    def affected_count(self) -> int:
        return len(self.affected_task_ids)


def is_legacy_collector(profile: PersonnelProfile | None) -> bool:
    return bool(profile and profile.profile_key == LEGACY_PROFILE_KEY)


def collector_display_label(profile: PersonnelProfile | None) -> str:
    if profile is None:
        return ""
    key = str(profile.profile_key or "")
    if len(key) == COLLECTOR_PROFILE_KEY_WIDTH and key.isascii() and key.isdecimal():
        return f"{profile.display_name}#{key}"
    return profile.display_name


def collection_attribution_snapshot(
    collector: PersonnelProfile | None,
    source: CollectionSource | None,
) -> dict[str, dict[str, object] | None]:
    """Build the display-safe collection attribution captured by every consumer."""
    return {
        "collector": (
            {
                "id": collector.id,
                "display_name": collector.display_name,
                "profile_key": collector.profile_key,
                "display_label": collector_display_label(collector),
            }
            if collector is not None
            else None
        ),
        "collection_source": (
            {
                "id": source.id,
                "label": source.label,
                "kind": source.kind,
                "external_key": source.external_key,
            }
            if source is not None
            else None
        ),
    }


def create_collector_profile(
    db: Session,
    *,
    workspace_id: int,
    display_name: str,
    is_shared: bool = False,
    random_number: Callable[[int], int] = secrets.randbelow,
) -> PersonnelProfile:
    used = {
        str(key)
        for (key,) in db.query(PersonnelProfile.profile_key)
        .filter(PersonnelProfile.workspace_id == workspace_id)
        .all()
        if isinstance(key, str)
        and len(key) == COLLECTOR_PROFILE_KEY_WIDTH
        and key.isascii()
        and key.isdecimal()
    }
    if len(used) >= COLLECTOR_PROFILE_KEY_CAPACITY:
        raise ValueError("collector profile key space is exhausted")

    candidates = chain(
        (
            (random_number(COLLECTOR_PROFILE_KEY_CAPACITY), True)
            for _ in range(COLLECTOR_PROFILE_KEY_RETRIES)
        ),
        ((number, False) for number in range(COLLECTOR_PROFILE_KEY_CAPACITY)),
    )
    for number, is_random_candidate in candidates:
        if is_random_candidate and (
            isinstance(number, bool)
            or not isinstance(number, int)
            or not 0 <= number < COLLECTOR_PROFILE_KEY_CAPACITY
        ):
            raise ValueError(
                "collector profile random number must return an integer between "
                f"0 and {COLLECTOR_PROFILE_KEY_CAPACITY - 1}"
            )
        key = f"{number:04d}"
        if key in used:
            continue
        try:
            with db.begin_nested():
                profile = PersonnelProfile(
                    workspace_id=workspace_id,
                    display_name=display_name,
                    profile_key=key,
                    is_shared=is_shared,
                )
                db.add(profile)
                db.flush()
        except IntegrityError:
            used.add(key)
            continue
        return profile

    if len(used) >= COLLECTOR_PROFILE_KEY_CAPACITY:
        raise ValueError("collector profile key space is exhausted")
    raise ValueError("collector profile key allocation could not find an unused key")


def assert_collection_attribution_complete(db: Session, task: Task) -> None:
    """Validate a persisted attribution before downstream task processing.

    Deactivation prevents a collector or source from being selected for a new
    attribution binding.  It must not invalidate a task that was already
    attributed, because its responsibility record and provenance remain part of
    the historical workflow.
    """
    if not task.collector_id:
        raise ValueError("collector attribution is required before processing")
    if not task.collection_source_id:
        raise ValueError("collection source attribution is required before processing")

    collector = db.get(PersonnelProfile, task.collector_id)
    if collector is None:
        raise ValueError("collector attribution is unavailable")
    source = db.get(CollectionSource, task.collection_source_id)
    if source is None:
        raise ValueError("collection source attribution is unavailable")

    workspace_id = resolve_task_workspace_id(db, task)
    if workspace_id is None:
        raise ValueError("task workspace is required for collection attribution")
    if collector.workspace_id != workspace_id and not collector.is_shared:
        raise ValueError("collector attribution belongs to another workspace")
    if source.workspace_id != workspace_id and not source.is_shared:
        raise ValueError("collection source attribution belongs to another workspace")


def ensure_legacy_attribution(
    db: Session,
    workspace_id: int,
    *,
    actor_id: int,
    task_ids: Sequence[int] | None = None,
) -> LegacyAttributionResult:
    """Bind only explicitly marked legacy tasks and append responsibility records."""
    actor = db.get(User, actor_id)
    if actor is None:
        raise ValueError("legacy attribution actor does not exist")
    if db.get(Workspace, workspace_id) is None:
        raise ValueError("workspace does not exist")

    workspace_tasks = (
        db.query(Task).filter(task_workspace_filter(db, workspace_id)).with_for_update().all()
    )
    by_id = {task.id: task for task in workspace_tasks}
    requested_ids = tuple(dict.fromkeys(int(task_id) for task_id in (task_ids or ())))
    if requested_ids:
        missing = [task_id for task_id in requested_ids if task_id not in by_id]
        if missing:
            raise ValueError("legacy task does not belong to the workspace")
        tasks = [by_id[task_id] for task_id in requested_ids]
    else:
        tasks = [task for task in workspace_tasks if _is_legacy_task(task)]

    invalid = [task.id for task in tasks if not _is_legacy_task(task)]
    if invalid:
        raise ValueError("task is not marked as legacy")

    profile, source, profile_is_new, source_is_new = _prepare_legacy_attribution_resources(
        db,
        workspace_id,
    )
    pending_tasks = [
        task for task in tasks if task.collector_id is None or task.collection_source_id is None
    ]
    collector_ids = [
        *([profile.id] if not profile_is_new and profile.id is not None else []),
        *(task.collector_id for task in pending_tasks if task.collector_id is not None),
    ]
    source_ids = [
        *([source.id] if not source_is_new and source.id is not None else []),
        *(
            task.collection_source_id
            for task in pending_tasks
            if task.collection_source_id is not None
        ),
    ]
    _preflight_legacy_attribution_resources(
        db,
        collector_ids=collector_ids,
        source_ids=source_ids,
        workspace_id=workspace_id,
    )
    locked_collectors, locked_sources = _lock_legacy_attribution_resources(
        db,
        collector_ids=collector_ids,
        source_ids=source_ids,
        workspace_id=workspace_id,
    )
    if not profile_is_new:
        profile = locked_collectors.get(profile.id)
    if not source_is_new:
        source = locked_sources.get(source.id)
    _assert_legacy_attribution_resource_is_bindable(
        resource_name="collector",
        resource=profile,
        task_workspace_id=workspace_id,
    )
    _assert_legacy_attribution_resource_is_bindable(
        resource_name="collection source",
        resource=source,
        task_workspace_id=workspace_id,
    )
    for task in pending_tasks:
        _assert_legacy_existing_attribution_is_bindable(
            db,
            task=task,
            workspace_id=workspace_id,
            collectors=locked_collectors,
            sources=locked_sources,
        )

    resolved: list[tuple[Task, PersonnelProfile, CollectionSource]] = []
    for task in pending_tasks:
        collector = locked_collectors.get(task.collector_id) if task.collector_id else profile
        collection_source = (
            locked_sources.get(task.collection_source_id) if task.collection_source_id else source
        )
        _assert_legacy_attribution_is_bindable(
            db,
            task=task,
            workspace_id=workspace_id,
            collector=collector,
            collection_source=collection_source,
        )
        resolved.append((task, collector, collection_source))

    if profile_is_new:
        db.add(profile)
    if source_is_new:
        db.add(source)
    if profile_is_new or source_is_new:
        db.flush()

    affected: list[int] = []
    for task, collector, collection_source in resolved:
        if task.collector_id is None:
            task.collector_id = collector.id
        if task.collection_source_id is None:
            task.collection_source_id = collection_source.id
        for asset in db.query(EgoEpisode).filter(EgoEpisode.task_id == task.id).all():
            if asset.provenance_snapshot_json is not None or asset.workflow_status == "published":
                continue
            asset.collector_id = collector.id
            asset.collection_source_id = collection_source.id
        _append_collection_record(
            db,
            task=task,
            workspace_id=workspace_id,
            source=collection_source,
            collector=collector,
            actor=actor,
            mode="legacy_attribution",
        )
        affected.append(task.id)
    db.commit()
    db.refresh(profile)
    return LegacyAttributionResult(
        profile=profile,
        source=source,
        affected_task_ids=tuple(affected),
    )


def get_or_create_legacy_attribution_resources(
    db: Session,
    workspace_id: int,
) -> tuple[PersonnelProfile, CollectionSource]:
    """Return the explicit legacy pair without committing the caller's transaction."""
    profile, source, profile_is_new, source_is_new = _prepare_legacy_attribution_resources(
        db,
        workspace_id,
    )
    if profile_is_new:
        db.add(profile)
    if source_is_new:
        db.add(source)
    if profile_is_new or source_is_new:
        db.flush()
    return profile, source


def _prepare_legacy_attribution_resources(
    db: Session,
    workspace_id: int,
) -> tuple[PersonnelProfile, CollectionSource, bool, bool]:
    """Build the legacy pair without persisting absent resources yet.

    ``ensure_legacy_attribution`` validates the entire batch against this pair
    before adding either new resource, so one invalid partial task cannot leave
    a reserved profile or source behind.
    """
    workspace = (
        db.query(Workspace).filter(Workspace.id == workspace_id).with_for_update().one_or_none()
    )
    if workspace is None:
        raise ValueError("workspace does not exist")
    profile = (
        db.query(PersonnelProfile)
        .filter(
            PersonnelProfile.workspace_id == workspace_id,
            PersonnelProfile.profile_key == LEGACY_PROFILE_KEY,
        )
        .one_or_none()
    )
    profile_is_new = profile is None
    if profile is None:
        profile = PersonnelProfile(
            workspace_id=workspace_id,
            display_name=LEGACY_DISPLAY_NAME,
            profile_key=LEGACY_PROFILE_KEY,
            is_active=True,
        )

    source = (
        db.query(CollectionSource)
        .filter(
            CollectionSource.workspace_id == workspace_id,
            CollectionSource.external_key == LEGACY_SOURCE_KEY,
        )
        .one_or_none()
    )
    source_is_new = source is None
    if source is None:
        source = CollectionSource(
            workspace_id=workspace_id,
            label=LEGACY_DISPLAY_NAME,
            kind="legacy_import",
            external_key=LEGACY_SOURCE_KEY,
            is_active=True,
        )
    return profile, source, profile_is_new, source_is_new


def _assert_legacy_existing_attribution_is_bindable(
    db: Session,
    *,
    task: Task,
    workspace_id: int,
    collectors: dict[int, PersonnelProfile],
    sources: dict[int, CollectionSource],
) -> None:
    """Validate the non-null side of a partial legacy assignment before writes."""
    task_workspace_id = resolve_task_workspace_id(db, task)
    if task_workspace_id != workspace_id:
        raise ValueError("legacy task does not belong to the workspace")
    if task.collector_id is not None:
        _assert_legacy_attribution_resource_is_bindable(
            resource_name="collector",
            resource=collectors.get(task.collector_id),
            task_workspace_id=task_workspace_id,
        )
    if task.collection_source_id is not None:
        _assert_legacy_attribution_resource_is_bindable(
            resource_name="collection source",
            resource=sources.get(task.collection_source_id),
            task_workspace_id=task_workspace_id,
        )


def _lock_legacy_attribution_resources(
    db: Session,
    *,
    collector_ids: Sequence[int],
    source_ids: Sequence[int],
    workspace_id: int,
) -> tuple[dict[int, PersonnelProfile], dict[int, CollectionSource]]:
    """Lock persisted legacy binding candidates before validating and writing.

    The lock acquisition runs in a savepoint.  If a later candidate has changed
    after the read-only preflight, rollback removes earlier no-op updates and
    their transactional trigger effects.  On success PostgreSQL retains each
    row lock until the caller's outer transaction commits.
    """
    collectors: dict[int, PersonnelProfile] = {}
    sources: dict[int, CollectionSource] = {}
    with db.begin_nested():
        for collector_id in sorted(set(collector_ids)):
            result = db.execute(
                update(PersonnelProfile)
                .where(
                    PersonnelProfile.id == collector_id,
                    PersonnelProfile.is_active.is_(True),
                )
                .values(
                    is_active=PersonnelProfile.is_active,
                    updated_at=PersonnelProfile.updated_at,
                )
            )
            collector = (
                db.query(PersonnelProfile)
                .populate_existing()
                .filter(PersonnelProfile.id == collector_id)
                .one_or_none()
            )
            _assert_legacy_attribution_resource_is_bindable(
                resource_name="collector",
                resource=collector,
                task_workspace_id=workspace_id,
            )
            if result.rowcount != 1:
                raise ValueError("collector attribution changed while locking")
            collectors[collector_id] = collector

        for source_id in sorted(set(source_ids)):
            result = db.execute(
                update(CollectionSource)
                .where(
                    CollectionSource.id == source_id,
                    CollectionSource.is_active.is_(True),
                )
                .values(
                    is_active=CollectionSource.is_active,
                    updated_at=CollectionSource.updated_at,
                )
            )
            source = (
                db.query(CollectionSource)
                .populate_existing()
                .filter(CollectionSource.id == source_id)
                .one_or_none()
            )
            _assert_legacy_attribution_resource_is_bindable(
                resource_name="collection source",
                resource=source,
                task_workspace_id=workspace_id,
            )
            if result.rowcount != 1:
                raise ValueError("collection source attribution changed while locking")
            sources[source_id] = source
    return collectors, sources


def _preflight_legacy_attribution_resources(
    db: Session,
    *,
    collector_ids: Sequence[int],
    source_ids: Sequence[int],
    workspace_id: int,
) -> None:
    """Reject every unavailable or out-of-scope candidate before lock writes.

    This deliberately uses ordinary reads.  The subsequent savepoint-protected
    conditional locks close the race between this check and binding.
    """
    normalized_collector_ids = sorted(set(collector_ids))
    normalized_source_ids = sorted(set(source_ids))
    collectors = {
        collector.id: collector
        for collector in (
            db.query(PersonnelProfile)
            .populate_existing()
            .filter(PersonnelProfile.id.in_(normalized_collector_ids))
            .all()
            if normalized_collector_ids
            else []
        )
    }
    sources = {
        source.id: source
        for source in (
            db.query(CollectionSource)
            .populate_existing()
            .filter(CollectionSource.id.in_(normalized_source_ids))
            .all()
            if normalized_source_ids
            else []
        )
    }
    for collector_id in normalized_collector_ids:
        _assert_legacy_attribution_resource_is_bindable(
            resource_name="collector",
            resource=collectors.get(collector_id),
            task_workspace_id=workspace_id,
        )
    for source_id in normalized_source_ids:
        _assert_legacy_attribution_resource_is_bindable(
            resource_name="collection source",
            resource=sources.get(source_id),
            task_workspace_id=workspace_id,
        )


def _assert_legacy_attribution_is_bindable(
    db: Session,
    *,
    task: Task,
    workspace_id: int,
    collector: PersonnelProfile | None,
    collection_source: CollectionSource | None,
) -> None:
    task_workspace_id = resolve_task_workspace_id(db, task)
    if task_workspace_id != workspace_id:
        raise ValueError("legacy task does not belong to the workspace")
    _assert_legacy_attribution_resource_is_bindable(
        resource_name="collector",
        resource=collector,
        task_workspace_id=task_workspace_id,
    )
    _assert_legacy_attribution_resource_is_bindable(
        resource_name="collection source",
        resource=collection_source,
        task_workspace_id=task_workspace_id,
    )


def _assert_legacy_attribution_resource_is_bindable(
    *,
    resource_name: str,
    resource: PersonnelProfile | CollectionSource | None,
    task_workspace_id: int,
) -> None:
    if resource is None or not resource.is_active:
        raise ValueError(f"{resource_name} attribution is unavailable")
    if resource.workspace_id != task_workspace_id and not resource.is_shared:
        raise ValueError(f"{resource_name} attribution belongs to another workspace")


def snapshot_provenance(
    db: Session,
    *,
    task: Task,
    asset: EgoEpisode,
    publisher_id: int | None,
) -> dict:
    """Return a display-safe immutable responsibility snapshot for publication."""
    if asset.task_id != task.id:
        raise ValueError("provenance asset does not belong to task")
    assert_collection_attribution_complete(db, task)

    collector_id = asset.collector_id or task.collector_id
    source_id = asset.collection_source_id or task.collection_source_id
    collector = db.get(PersonnelProfile, collector_id)
    source = db.get(CollectionSource, source_id)
    if collector is None:
        raise ValueError("collector attribution is unavailable")
    if source is None:
        raise ValueError("collection source attribution is unavailable")
    workspace_id = resolve_task_workspace_id(db, task)
    if collector.workspace_id != workspace_id and not collector.is_shared:
        raise ValueError("collector attribution belongs to another workspace")
    if source.workspace_id != workspace_id and not source.is_shared:
        raise ValueError("collection source attribution belongs to another workspace")

    return {
        "schema": "quicdata.provenance.v1",
        "captured_at": datetime.utcnow().isoformat() + "Z",
        "task": {
            "id": task.id,
            "data_source": task.data_source or "",
        },
        "asset": {
            "id": asset.id,
            "asset_id": asset.asset_id,
        },
        **collection_attribution_snapshot(collector, source),
        "actors": {
            "ingested_by_user_id": asset.ingested_by_user_id or task.ingested_by_user_id,
            "created_by_user_id": asset.created_by_user_id or task.created_by_user_id,
            "publisher_id": publisher_id,
        },
    }


def assign_collection_attribution(
    db: Session,
    *,
    task_id: int,
    collection_source_id: int,
    collector_id: int,
    actor_id: int,
) -> Task:
    """Assign validated working attribution and append an immutable record."""
    task = db.query(Task).filter(Task.id == task_id).with_for_update().one_or_none()
    if task is None:
        raise ValueError("task does not exist")
    _bind_collection_attribution(
        db,
        task=task,
        collection_source_id=collection_source_id,
        collector_id=collector_id,
        actor_id=actor_id,
        mode="manual_assignment",
    )
    db.commit()
    db.refresh(task)
    return task


def bind_collection_attribution_on_create(
    db: Session,
    *,
    task: Task,
    collection_source_id: int | None,
    collector_id: int | None,
    actor_id: int | None,
) -> Task:
    """Optionally bind an attribution pair as part of a new task transaction.

    No pair preserves the legacy creation contract.  A partial pair is rejected
    before any attribution row is written, and a complete pair is validated
    against the task workspace and actor before its responsibility record is
    appended.  Callers own the transaction and must commit the Task together
    with this binding.
    """
    if collection_source_id is None and collector_id is None:
        return task
    if collection_source_id is None or collector_id is None:
        raise ValueError("collector and collection source attribution must be provided together")
    if task.id is None:
        raise ValueError("task must be persisted before collection attribution is bound")
    return _bind_collection_attribution(
        db,
        task=task,
        collection_source_id=collection_source_id,
        collector_id=collector_id,
        actor_id=actor_id,
        mode="source_import",
    )


def _bind_collection_attribution(
    db: Session,
    *,
    task: Task,
    collection_source_id: int,
    collector_id: int,
    actor_id: int | None,
    mode: str,
) -> Task:
    workspace_id = resolve_task_workspace_id(db, task)
    if workspace_id is None:
        raise ValueError("task workspace is required for collection attribution")
    actor = require_task_actor(db, actor_id=actor_id, task=task)
    source = db.get(CollectionSource, collection_source_id)
    if source is None or not source.is_active:
        raise ValueError("collection source does not exist or is inactive")
    collector = db.get(PersonnelProfile, collector_id)
    if collector is None or not collector.is_active:
        raise ValueError("collector does not exist or is inactive")
    _assert_assignment_scope(
        resource_name="collection source",
        resource_workspace_id=source.workspace_id,
        is_shared=source.is_shared,
        task_workspace_id=workspace_id,
        actor=actor,
    )
    _assert_assignment_scope(
        resource_name="collector",
        resource_workspace_id=collector.workspace_id,
        is_shared=collector.is_shared,
        task_workspace_id=workspace_id,
        actor=actor,
    )

    task.collection_source_id = source.id
    task.collector_id = collector.id
    for asset in db.query(EgoEpisode).filter(EgoEpisode.task_id == task.id).all():
        if asset.provenance_snapshot_json is not None or asset.workflow_status == "published":
            continue
        asset.collection_source_id = source.id
        asset.collector_id = collector.id
    _append_collection_record(
        db,
        task=task,
        workspace_id=workspace_id,
        source=source,
        collector=collector,
        actor=actor,
        mode=mode,
    )
    return task


def _assert_assignment_scope(
    *,
    resource_name: str,
    resource_workspace_id: int,
    is_shared: bool,
    task_workspace_id: int,
    actor: User,
) -> None:
    if resource_workspace_id == task_workspace_id:
        return
    if not is_shared:
        raise ValueError(f"{resource_name} belongs to another workspace")
    if actor.role != "admin":
        raise PermissionError(f"admin is required to assign a shared {resource_name}")


def _is_legacy_task(task: Task) -> bool:
    provenance = (task.metadata_json or {}).get("provenance") or {}
    return task.data_source == "legacy_import" or bool(
        provenance.get("legacy_import") or provenance.get("legacy_import_batch_id")
    )


def _append_collection_record(
    db: Session,
    *,
    task: Task,
    workspace_id: int,
    source: CollectionSource,
    collector: PersonnelProfile,
    actor: User,
    mode: str,
) -> None:
    revision = (
        db.query(func.max(CollectionRecord.revision))
        .filter(CollectionRecord.task_id == task.id)
        .scalar()
        or 0
    ) + 1
    snapshot: dict[str, object] = collection_attribution_snapshot(collector, source)
    snapshot.update(
        {
            "schema": "quicdata.collection-attribution.v1",
            "mode": mode,
            "captured_at": datetime.utcnow().isoformat() + "Z",
            "recorded_by_user_id": actor.id,
            "actor_id": actor.id,
        }
    )
    db.add(
        CollectionRecord(
            task_id=task.id,
            workspace_id=workspace_id,
            revision=revision,
            collection_source_id=source.id,
            collector_id=collector.id,
            ingested_by_user_id=task.ingested_by_user_id,
            created_by_user_id=task.created_by_user_id,
            recorded_by_user_id=actor.id,
            data_source=task.data_source or "",
            snapshot_json=snapshot,
        )
    )


def append_offline_collection_record(
    db: Session,
    *,
    candidate_id: int,
    ego_source_id: str | None,
    workspace_id: int,
    source: CollectionSource,
    collector: PersonnelProfile,
    claimed_attribution: dict[str, object],
    resolution: str,
    actor_id: int | None,
) -> CollectionRecord:
    """Append immutable offline attribution evidence before Task creation.

    Candidate-linked records deliberately do not mutate an existing task
    attribution. A later import may reference this evidence, but the original
    trusted-device claim stays attached to the candidate snapshot.
    """
    revision = (
        db.query(func.max(CollectionRecord.revision))
        .filter(CollectionRecord.ego_source_candidate_id == candidate_id)
        .scalar()
        or 0
    ) + 1
    snapshot: dict[str, object] = collection_attribution_snapshot(collector, source)
    snapshot.update(
        {
            "schema": "quicdata.collection-attribution.v1",
            "mode": "trusted_offline_device",
            "resolution": resolution,
            "claimed_attribution": dict(claimed_attribution),
            "captured_at": datetime.utcnow().isoformat() + "Z",
            "recorded_by_user_id": actor_id,
            "actor_id": actor_id,
        }
    )
    record = CollectionRecord(
        task_id=None,
        ego_source_candidate_id=candidate_id,
        ego_source_id=ego_source_id,
        workspace_id=workspace_id,
        revision=revision,
        collection_source_id=source.id,
        collector_id=collector.id,
        recorded_by_user_id=actor_id,
        data_source="trusted_offline",
        snapshot_json=snapshot,
    )
    db.add(record)
    return record
