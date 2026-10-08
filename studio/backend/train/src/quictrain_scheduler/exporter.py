"""CPFS → export-root artifact export worker (no checkpoint bytes through API)."""

from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path
from urllib.parse import urlparse

from quictrain_core import new_id
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from quictrain_api.db import ArtifactExportRecord, ArtifactRecord, AuditEventRecord, utcnow
from quictrain_api.settings import get_settings

LOGGER = logging.getLogger(__name__)

EXPORT_PENDING = "PENDING"
EXPORT_RUNNING = "RUNNING"
EXPORT_SUCCEEDED = "SUCCEEDED"
EXPORT_FAILED = "FAILED"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _local_path(uri: str) -> Path | None:
    if uri.startswith("file://"):
        return Path(urlparse(uri).path)
    if uri.startswith("bmcpfs://"):
        # bmcpfs://fs/path → /mnt/cpfs/path style; for local tests allow file mirror.
        settings = get_settings()
        remainder = uri.split("://", 1)[1]
        parts = remainder.split("/", 1)
        relative = parts[1] if len(parts) > 1 else ""
        return Path(settings.cpfs_mount_path) / relative
    if uri.startswith("/") or uri.startswith("./"):
        return Path(uri)
    return None


class Exporter:
    def __init__(self, session_factory: sessionmaker) -> None:
        self.session_factory = session_factory

    def run_once(self) -> bool:
        worked = False
        with self.session_factory() as session:
            export_ids = list(
                session.scalars(
                    select(ArtifactExportRecord.id)
                    .where(ArtifactExportRecord.state.in_([EXPORT_PENDING, EXPORT_RUNNING]))
                    .order_by(ArtifactExportRecord.created_at.asc())
                    .limit(20)
                )
            )
        if export_ids:
            worked = True
        for export_id in export_ids:
            try:
                with self.session_factory() as session:
                    record = session.get(ArtifactExportRecord, export_id)
                    if record is None:
                        continue
                    self._process(session, record)
                    session.commit()
            except Exception:
                LOGGER.exception("Export %s failed", export_id)
        if get_settings().export_gc_enabled:
            with self.session_factory() as session:
                purged = gc_expired_exports(session)
                session.commit()
                if purged:
                    worked = True
        return worked

    def _process(self, session: Session, export: ArtifactExportRecord) -> None:
        artifact = session.get(ArtifactRecord, export.artifact_id)
        if artifact is None:
            export.state = EXPORT_FAILED
            export.error_message = "artifact missing"
            export.finished_at = utcnow()
            return

        export.state = EXPORT_RUNNING
        source = _local_path(export.source_uri)
        if source is None or not source.is_file():
            export.state = EXPORT_FAILED
            export.error_message = f"source unavailable: {export.source_uri}"
            export.finished_at = utcnow()
            return

        settings = get_settings()
        export_dir = Path(settings.resolved_export_root()) / export.id
        export_dir.mkdir(parents=True, exist_ok=True)
        destination = export_dir / artifact.name
        shutil.copy2(source, destination)
        observed = _sha256_file(destination)
        if artifact.sha256 and artifact.sha256 != observed and len(artifact.sha256) == 64:
            export.state = EXPORT_FAILED
            export.error_message = "checksum mismatch"
            export.bytes_copied = destination.stat().st_size
            export.finished_at = utcnow()
            return

        export_uri = f"{settings.export_oss_prefix.rstrip('/')}/{export.id}/{artifact.name}"
        # Local mirror path for Fake/dev download signing without streaming via API.
        local_export_uri = f"file://{destination.resolve()}"
        export.export_uri = export_uri
        export.bytes_copied = destination.stat().st_size
        export.checksum = f"sha256:{observed}"
        export.state = EXPORT_SUCCEEDED
        export.finished_at = utcnow()
        artifact.export_uri = export_uri
        # Persist local file URI in provider-compatible form for Fake artifact client tests.
        artifact.export_uri = local_export_uri if settings.provider == "fake" else export_uri
        export.export_uri = artifact.export_uri
        session.add(
            AuditEventRecord(
                id=new_id("aud"),
                actor_id=export.actor_id,
                action="artifact.export.succeeded",
                resource_type="artifact",
                resource_id=artifact.id,
                details={
                    "export_id": export.id,
                    "export_uri": artifact.export_uri,
                    "bytes_copied": export.bytes_copied,
                },
            )
        )


def request_export(
    session: Session,
    artifact: ArtifactRecord,
    *,
    actor_id: str,
    job_id: str,
) -> ArtifactExportRecord:
    existing = session.scalar(
        select(ArtifactExportRecord)
        .where(
            ArtifactExportRecord.artifact_id == artifact.id,
            ArtifactExportRecord.state.in_([EXPORT_PENDING, EXPORT_RUNNING, EXPORT_SUCCEEDED]),
        )
        .order_by(ArtifactExportRecord.created_at.desc())
    )
    if existing is not None:
        return existing

    record = ArtifactExportRecord(
        id=new_id("exp"),
        artifact_id=artifact.id,
        job_id=job_id,
        attempt_id=artifact.attempt_id,
        state=EXPORT_PENDING,
        source_uri=artifact.uri,
        checksum=artifact.sha256,
        bytes_copied=0,
        actor_id=actor_id,
        retention_days=artifact.retention_days,
    )
    session.add(record)
    session.add(
        AuditEventRecord(
            id=new_id("aud"),
            actor_id=actor_id,
            action="artifact.export.requested",
            resource_type="artifact",
            resource_id=artifact.id,
            details={"export_id": record.id},
        )
    )
    session.flush()
    return record


def gc_expired_exports(session: Session) -> list[str]:
    """Expire SUCCEEDED exports past retention_days; clear artifact.export_uri."""

    from datetime import timedelta

    purged: list[str] = []
    settings = get_settings()
    now = utcnow()
    for export in session.scalars(
        select(ArtifactExportRecord).where(
            ArtifactExportRecord.state == EXPORT_SUCCEEDED,
            ArtifactExportRecord.finished_at.is_not(None),
        )
    ):
        assert export.finished_at is not None
        finished = export.finished_at
        if finished.tzinfo is None:
            from datetime import UTC

            finished = finished.replace(tzinfo=UTC)
        age = now - finished
        if age < timedelta(days=max(export.retention_days, 1)):
            continue
        export_dir = Path(settings.resolved_export_root()) / export.id
        if export_dir.exists():
            shutil.rmtree(export_dir, ignore_errors=True)
        artifact = session.get(ArtifactRecord, export.artifact_id)
        if artifact is not None and artifact.export_uri == export.export_uri:
            artifact.export_uri = None
        export.state = "EXPIRED"
        session.add(
            AuditEventRecord(
                id=new_id("aud"),
                actor_id="system",
                action="artifact.export.expired",
                resource_type="artifact_export",
                resource_id=export.id,
                details={"artifact_id": export.artifact_id},
            )
        )
        purged.append(export.id)
    return purged
