"""Fail-soft projections for producer-declared QRDF capture provenance.

The capture package decides neither its target workspace nor its task set.
Those are already fixed by the ImportSession before this module is called.
This module only projects optional producer hints onto workspace-local
collection resources. Its scan-time projection is fail-soft; import intake
applies the separate, explicit candidate-level gate for a malformed claimed
collector identifier. Raw hints never reach browser-facing DTOs unchanged.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from data.database import CollectionDevice, Episode, PersonnelProfile, WorkspacePersonnelProfile

SourceGroupStatus = Literal["valid", "missing", "invalid", "legacy"]
ResourceMatchStatus = Literal["matched", "unknown", "unmatched"]
CollectorHintStatus = Literal["missing", "valid", "invalid"]

_COLLECTOR_IDENTIFIER = re.compile(r"^[1-9][0-9]*$")
_UNKNOWN_COLLECTOR_IDENTIFIER = "unknown"
_MAX_SOURCE_GROUP_SCALARS = 128
_MAX_SERIAL_SCALARS = 128


@dataclass(frozen=True)
class CaptureProvenanceProjection:
    """Safe, workspace-local interpretation of optional capture hints."""

    source_group_key: str | None
    source_group_name: str | None
    source_group_status: SourceGroupStatus
    collector_reported_identifier: str | None
    collector_identifier: str | None
    collector_hint_status: CollectorHintStatus
    collector_profile_id: int | None
    collector_match_status: ResourceMatchStatus
    device_serial: str | None
    collection_device_id: int | None
    device_match_status: ResourceMatchStatus


@dataclass(frozen=True)
class SourceGroupBackfillPage:
    """One explicitly bounded, database-only legacy migration page."""

    scanned_count: int
    eligible_count: int
    updated_count: int
    next_after_episode_id: int | None


def is_unknown_collector_identifier(value: object) -> bool:
    """Return whether a producer used the reserved unknown collector marker.

    EGO writes ``unknown`` when no collector was configured.  It is an
    absence marker, not a claimed collector identifier, so all intake paths
    must interpret it consistently as an unreported collector.
    """
    if not isinstance(value, str):
        return False
    normalized = unicodedata.normalize("NFC", value)
    return normalized.casefold() == _UNKNOWN_COLLECTOR_IDENTIFIER


def project_capture_provenance(
    db: Session,
    *,
    workspace_id: int,
    raw_metadata: object,
) -> CaptureProvenanceProjection:
    """Project optional source-group, operator, and device hints safely.

    ``workspace_id`` originates from the authorized import session.  It is
    deliberately the only tenancy input accepted here, so a package can never
    select or escape its target tenant through metadata.
    """
    if isinstance(workspace_id, bool) or not isinstance(workspace_id, int) or workspace_id <= 0:
        raise ValueError("workspace_id must be a positive integer")

    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    source_group_key, source_group_name, source_group_status = _project_source_group(metadata)

    (
        collector_reported_identifier,
        collector_identifier,
        collector_hint_status,
        collector_match_status,
    ) = _extract_collector_identifier(metadata)
    collector_profile_id = None
    if collector_identifier is not None:
        collector_profile_id = db.scalar(
            select(PersonnelProfile.id)
            .join(
                WorkspacePersonnelProfile,
                WorkspacePersonnelProfile.personnel_profile_id == PersonnelProfile.id,
            )
            .where(
                WorkspacePersonnelProfile.workspace_id == workspace_id,
                PersonnelProfile.profile_key == collector_identifier,
            )
        )
        if collector_profile_id is None:
            collector_match_status = "unmatched"
        else:
            collector_match_status = "matched"

    device_serial, device_match_status = _extract_device_serial(metadata)
    collection_device_id = None
    if device_serial is not None:
        collection_device_id = db.scalar(
            select(CollectionDevice.id).where(
                CollectionDevice.workspace_id == workspace_id,
                func.upper(func.btrim(CollectionDevice.serial_number)) == device_serial.upper(),
            )
        )
        if collection_device_id is None:
            device_match_status = "unmatched"
        else:
            device_match_status = "matched"

    return CaptureProvenanceProjection(
        source_group_key=source_group_key,
        source_group_name=source_group_name,
        source_group_status=source_group_status,
        collector_reported_identifier=collector_reported_identifier,
        collector_identifier=collector_identifier,
        collector_hint_status=collector_hint_status,
        collector_profile_id=collector_profile_id,
        collector_match_status=collector_match_status,
        device_serial=device_serial,
        collection_device_id=collection_device_id,
        device_match_status=device_match_status,
    )


def backfill_episode_source_groups(
    db: Session,
    *,
    limit: int,
    after_episode_id: int | None,
    apply: bool,
) -> SourceGroupBackfillPage:
    """Recover only reliable legacy source-group facts from the database.

    This deliberately does not read OSS, alter immutable raw artifacts, or
    infer a grouping from task labels.  It is safe to resume with the returned
    primary-key cursor after each independently committed page.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1_000:
        raise ValueError("source group backfill limit is invalid")
    if after_episode_id is not None and (
        not isinstance(after_episode_id, int)
        or isinstance(after_episode_id, bool)
        or after_episode_id < 0
    ):
        raise ValueError("source group backfill cursor is invalid")

    query = select(Episode).where(
        Episode.kind == "source",
        Episode.reported_source_group_status == "missing",
    )
    if after_episode_id is not None:
        query = query.where(Episode.id > after_episode_id)
    rows = list(db.scalars(query.order_by(Episode.id).limit(limit + 1)))
    page = rows[:limit]
    eligible_count = 0
    updated_count = 0
    for episode in page:
        projection = project_capture_provenance(
            db,
            workspace_id=episode.workspace_id,
            raw_metadata=_legacy_source_group_metadata(episode.metadata_json),
        )
        # Historical metadata is not an authority for new invalid labels.  A
        # record is upgraded only when the old import stored a safely usable
        # task identifier; all other rows remain "missing".
        if projection.source_group_status != "legacy":
            continue
        eligible_count += 1
        if apply:
            episode.reported_source_group_key = projection.source_group_key
            episode.reported_source_group_name = projection.source_group_name
            episode.reported_source_group_status = projection.source_group_status
            updated_count += 1

    return SourceGroupBackfillPage(
        scanned_count=len(page),
        eligible_count=eligible_count,
        updated_count=updated_count,
        next_after_episode_id=page[-1].id if len(rows) > limit else None,
    )


def _project_source_group(
    metadata: dict[object, object],
) -> tuple[str | None, str | None, SourceGroupStatus]:
    source_group = metadata.get("source_group")
    if source_group is None:
        legacy_value = metadata.get("collection_task_id")
        if legacy_value is None:
            return None, None, "missing"
        legacy_name = _normalize_source_group_name(legacy_value)
        if legacy_name is None:
            return None, None, "invalid"
        digest = hashlib.sha256(legacy_name.encode("utf-8")).hexdigest()
        return f"legacy_sg1_{digest}", legacy_name, "legacy"
    if not isinstance(source_group, dict):
        return None, None, "invalid"
    name = _normalize_source_group_name(source_group.get("name"))
    key = source_group.get("key")
    if name is None or not isinstance(key, str):
        return None, None, "invalid"
    expected_key = f"sg1_{hashlib.sha256(name.encode('utf-8')).hexdigest()}"
    if key != expected_key:
        return None, None, "invalid"
    return expected_key, name, "valid"


def _legacy_source_group_metadata(metadata_json: object) -> dict[str, object]:
    """Extract the only historic grouping fact that old imports persisted."""
    if not isinstance(metadata_json, dict):
        return {}
    import_metadata = metadata_json.get("import")
    if not isinstance(import_metadata, dict):
        return {}
    if "legacy_ego_task_id" not in import_metadata:
        return {}
    return {"collection_task_id": import_metadata.get("legacy_ego_task_id")}


def _normalize_source_group_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        return None
    normalized = " ".join(normalized.split())
    if not normalized or len(normalized) > _MAX_SOURCE_GROUP_SCALARS:
        return None
    return normalized


def _extract_collector_identifier(
    metadata: dict[object, object],
) -> tuple[str | None, str | None, CollectorHintStatus, ResourceMatchStatus]:
    operator = metadata.get("operator")
    if operator is None:
        return None, None, "missing", "unknown"
    if not isinstance(operator, dict):
        return None, None, "invalid", "unmatched"
    value = operator.get("id")
    if value is None:
        return None, None, "missing", "unknown"
    if is_unknown_collector_identifier(value):
        return None, None, "missing", "unknown"
    reported_identifier = _safe_collector_reported_identifier(value)
    if (
        not isinstance(value, str)
        or reported_identifier is None
        or not _COLLECTOR_IDENTIFIER.fullmatch(value)
    ):
        return reported_identifier, None, "invalid", "unmatched"
    return reported_identifier, value, "valid", "unmatched"


def _safe_collector_reported_identifier(value: object) -> str | None:
    """Keep a bounded display value without treating it as an identifier."""
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFC", value)
    elif type(value) is int:
        normalized = str(value)
    else:
        return None
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        return None
    if not 1 <= len(normalized) <= 128:
        return None
    return normalized


def _extract_device_serial(
    metadata: dict[object, object],
) -> tuple[str | None, ResourceMatchStatus]:
    devices = metadata.get("devices")
    if devices is None:
        return None, "unknown"
    if not isinstance(devices, list):
        return None, "unmatched"

    serials: set[str] = set()
    saw_invalid = False
    for device in devices:
        if not isinstance(device, dict):
            saw_invalid = True
            continue
        serial = device.get("serial_number")
        if serial is None:
            continue
        normalized = _normalize_serial(serial)
        if normalized is None:
            saw_invalid = True
            continue
        serials.add(normalized)

    if not serials:
        return None, "unmatched" if saw_invalid else "unknown"
    if len(serials) != 1:
        return None, "unmatched"
    return next(iter(serials)), "unmatched"


def _normalize_serial(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if any(unicodedata.category(character).startswith("C") for character in value):
        return None
    normalized = value.strip()
    if not 1 <= len(normalized) <= _MAX_SERIAL_SCALARS or not normalized.isprintable():
        return None
    return normalized
