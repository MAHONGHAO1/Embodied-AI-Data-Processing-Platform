"""Validated, idempotent import sessions for one Duance QRDF episode."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from data.database import Batch, ImportSession, TaskLabel, User
from data.integrations.qrdf.paths import validate_qrdf_external_episode_id
from data.services.import_sessions import create_import_session

MAX_DUANCE_METADATA_BYTES = 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DuanceImportValidationError(ValueError):
    """Raised when a client declaration is not a safe QRDF source manifest."""


class DuanceImportConflictError(ValueError):
    """Raised when an existing source identity has incompatible durable scope."""


@dataclass(frozen=True, slots=True)
class DuanceImportManifest:
    """The server-owned declaration for exactly one QRDF metadata/MCAP pair."""

    source_key: str
    episode_id: str
    start_ns: int
    end_ns: int
    metadata_text: str
    metadata_sha256: str
    data_path: str
    data_size_bytes: int
    data_sha256: str

    def to_storage(self) -> dict[str, object]:
        return {
            "source_key": self.source_key,
            "source": {
                "episode_id": self.episode_id,
                "start_ns": str(self.start_ns),
                "end_ns": str(self.end_ns),
                "metadata_sha256": self.metadata_sha256,
                "data_mcap_sha256": self.data_sha256,
            },
            "metadata_text": self.metadata_text,
            "data_file": {
                "path": self.data_path,
                "size_bytes": self.data_size_bytes,
                "sha256": self.data_sha256,
            },
        }


def create_duance_import(
    db: Session,
    *,
    batch_id: int,
    actor_id: int | None,
    task_label_id: int,
    source: Mapping[str, object],
    metadata_text: str,
    data_file: Mapping[str, object],
) -> ImportSession:
    """Create or reuse the durable direct-import resource for one source identity."""
    manifest = build_duance_import_manifest(
        source=source,
        metadata_text=metadata_text,
        data_file=data_file,
    )
    batch = db.scalar(select(Batch).where(Batch.id == batch_id).with_for_update())
    if batch is None:
        raise ValueError("batch does not exist")
    if actor_id is not None and db.get(User, actor_id) is None:
        raise ValueError("import actor does not exist")
    if db.get(TaskLabel, task_label_id) is None:
        raise ValueError("task label does not exist")

    existing = db.scalar(
        select(ImportSession)
        .where(
            ImportSession.batch_id == batch.id,
            ImportSession.import_type == "duance_episode",
            ImportSession.source_fingerprint == manifest.source_key,
        )
        .with_for_update()
    )
    if existing is not None:
        if existing.task_label_id != task_label_id:
            raise DuanceImportConflictError(
                "source identity already belongs to a different task label"
            )
        return existing

    import_session = create_import_session(
        db,
        batch_id=batch.id,
        import_type="duance_episode",
        actor_id=actor_id,
        task_label_id=task_label_id,
        original_name=PurePosixPath(manifest.data_path).name,
        source_fingerprint=manifest.source_key,
    )
    import_session.result_json = {"duance_manifest": manifest.to_storage()}
    db.flush()
    return import_session


def build_duance_import_manifest(
    *,
    source: Mapping[str, object],
    metadata_text: str,
    data_file: Mapping[str, object],
) -> DuanceImportManifest:
    """Validate a client request before it receives a durable upload resource."""
    if not isinstance(source, Mapping) or not isinstance(data_file, Mapping):
        raise DuanceImportValidationError("Duance import declaration is invalid")
    episode_id = _episode_id(source.get("episode_id"))
    start_ns = _timestamp(source.get("start_ns"), "source start_ns")
    end_ns = _timestamp(source.get("end_ns"), "source end_ns")
    if start_ns >= end_ns:
        raise DuanceImportValidationError("source timeline is invalid")
    metadata_sha256 = _sha256(source.get("metadata_sha256"), "metadata SHA-256")
    data_sha256 = _sha256(source.get("data_mcap_sha256"), "MCAP SHA-256")
    data_path = _data_path(data_file.get("path"))
    data_size_bytes = _positive_size(data_file.get("size_bytes"))
    if _sha256(data_file.get("sha256"), "data file SHA-256") != data_sha256:
        raise DuanceImportValidationError("data file SHA-256 does not match source")
    metadata = _metadata(metadata_text, metadata_sha256)
    if metadata.get("episode_id") != episode_id:
        raise DuanceImportValidationError("metadata episode_id does not match source")
    if metadata.get("data_file") != data_path:
        raise DuanceImportValidationError("metadata data_file does not match source")
    timing = metadata.get("timing")
    if not isinstance(timing, dict):
        raise DuanceImportValidationError("metadata timing is invalid")
    if (
        _metadata_timestamp(timing.get("start_timestamp_ns"), "metadata start_timestamp_ns")
        != start_ns
    ):
        raise DuanceImportValidationError("metadata start timestamp does not match source")
    if _metadata_timestamp(timing.get("end_timestamp_ns"), "metadata end_timestamp_ns") != end_ns:
        raise DuanceImportValidationError("metadata end timestamp does not match source")

    source_key = hashlib.sha256(
        json.dumps(
            {
                "data_mcap_sha256": data_sha256,
                "end_ns": end_ns,
                "episode_id": episode_id,
                "metadata_sha256": metadata_sha256,
                "start_ns": start_ns,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return DuanceImportManifest(
        source_key=source_key,
        episode_id=episode_id,
        start_ns=start_ns,
        end_ns=end_ns,
        metadata_text=metadata_text,
        metadata_sha256=metadata_sha256,
        data_path=data_path,
        data_size_bytes=data_size_bytes,
        data_sha256=data_sha256,
    )


def load_duance_import_manifest(import_session: ImportSession) -> DuanceImportManifest:
    """Revalidate the persisted declaration before using it for upload or parsing."""
    if import_session.import_type != "duance_episode":
        raise DuanceImportValidationError("import session is not a Duance direct import")
    result = import_session.result_json
    if not isinstance(result, dict):
        raise DuanceImportValidationError("Duance import manifest is unavailable")
    manifest = result.get("duance_manifest")
    if not isinstance(manifest, dict):
        raise DuanceImportValidationError("Duance import manifest is unavailable")
    source = manifest.get("source")
    metadata_text = manifest.get("metadata_text")
    data_file = manifest.get("data_file")
    if (
        not isinstance(source, dict)
        or not isinstance(metadata_text, str)
        or not isinstance(data_file, dict)
    ):
        raise DuanceImportValidationError("Duance import manifest is invalid")
    parsed = build_duance_import_manifest(
        source=source,
        metadata_text=metadata_text,
        data_file=data_file,
    )
    stored_key = manifest.get("source_key")
    if stored_key != parsed.source_key or import_session.source_fingerprint != parsed.source_key:
        raise DuanceImportValidationError("Duance import source identity is invalid")
    return parsed


def _metadata(metadata_text: str, expected_sha256: str) -> dict[str, object]:
    if not isinstance(metadata_text, str):
        raise DuanceImportValidationError("metadata text is invalid")
    try:
        encoded = metadata_text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DuanceImportValidationError("metadata text is not UTF-8") from exc
    if not encoded or len(encoded) > MAX_DUANCE_METADATA_BYTES:
        raise DuanceImportValidationError("metadata text size is invalid")
    if hashlib.sha256(encoded).hexdigest() != expected_sha256:
        raise DuanceImportValidationError("metadata SHA-256 does not match metadata text")
    try:
        parsed = json.loads(metadata_text)
    except json.JSONDecodeError as exc:
        raise DuanceImportValidationError("metadata JSON is invalid") from exc
    if not isinstance(parsed, dict):
        raise DuanceImportValidationError("metadata JSON must be an object")
    return parsed


def _episode_id(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise DuanceImportValidationError("source episode_id is invalid")
    try:
        validate_qrdf_external_episode_id(value)
    except ValueError as exc:
        raise DuanceImportValidationError("source episode_id is invalid") from exc
    return value


def _timestamp(value: object, label: str) -> int:
    if not isinstance(value, str) or not value.isdecimal():
        raise DuanceImportValidationError(f"{label} is invalid")
    return int(value)


def _metadata_timestamp(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise DuanceImportValidationError(f"{label} is invalid")
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    raise DuanceImportValidationError(f"{label} is invalid")


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DuanceImportValidationError(f"{label} is invalid")
    return value


def _data_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise DuanceImportValidationError("data file path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(part in {".", ".."} for part in path.parts):
        raise DuanceImportValidationError("data file path is invalid")
    return value


def _positive_size(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DuanceImportValidationError("data file size is invalid")
    return value
