"""Local filesystem materializer (CPU-only control plane)."""

from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path
from urllib.parse import urlparse

from quictrain_core import DatasetReadiness, MaterializationState, new_id
from quictrain_core.materialization import is_dataset_archive_uri
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from quictrain_api.db import (
    AuditEventRecord,
    DatasetVersionRecord,
    MaterializationAttemptRecord,
    utcnow,
)
from quictrain_api.settings import get_settings

LOGGER = logging.getLogger(__name__)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _local_path_from_uri(uri: str) -> Path | None:
    if uri.startswith("file://"):
        return Path(urlparse(uri).path)
    if uri.startswith("/") or uri.startswith("./"):
        return Path(uri)
    return None


def target_uri_for_dataset(dataset: DatasetVersionRecord) -> str:
    settings = get_settings()
    root = Path(settings.resolved_materialization_root())
    relative = f"{dataset.external_dataset_id}/{dataset.version}"
    return str((root / relative).resolve())


class Materializer:
    """Idempotent OSS/file → local CPFS-layout materialization controller."""

    def __init__(self, session_factory: sessionmaker) -> None:
        self.session_factory = session_factory

    def run_once(self) -> bool:
        with self.session_factory() as session:
            attempt_ids = list(
                session.scalars(
                    select(MaterializationAttemptRecord.id)
                    .where(
                        MaterializationAttemptRecord.state.in_(
                            [MaterializationState.PENDING.value, MaterializationState.RUNNING.value]
                        )
                    )
                    .order_by(MaterializationAttemptRecord.created_at.asc())
                    .limit(20)
                )
            )
        if not attempt_ids:
            return False
        for attempt_id in attempt_ids:
            try:
                with self.session_factory() as session:
                    attempt = session.get(MaterializationAttemptRecord, attempt_id)
                    if attempt is None:
                        continue
                    self._process(session, attempt)
                    session.commit()
            except Exception:
                LOGGER.exception("Materialization attempt %s failed", attempt_id)
        return True

    def _process(self, session: Session, attempt: MaterializationAttemptRecord) -> None:
        dataset = session.get(DatasetVersionRecord, attempt.dataset_version_id)
        if dataset is None:
            attempt.state = MaterializationState.FAILED.value
            attempt.error_message = "dataset missing"
            attempt.finished_at = utcnow()
            return

        if attempt.state == MaterializationState.PENDING.value:
            attempt.state = MaterializationState.RUNNING.value
            attempt.started_at = utcnow()
            dataset.status = DatasetReadiness.MATERIALIZING.value
            manifest = dict(dataset.manifest)
            manifest["status"] = dataset.status
            dataset.manifest = manifest

        source = _local_path_from_uri(attempt.source_uri)
        target = Path(attempt.target_uri)
        try:
            if is_dataset_archive_uri(attempt.source_uri):
                raise ValueError("archive extraction and dataset verification are not supported")
            if source is not None and source.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    if target.exists():
                        shutil.rmtree(target)
                    shutil.copytree(source, target)
                    bytes_copied = sum(
                        path.stat().st_size for path in target.rglob("*") if path.is_file()
                    )
                    observed = dataset.checksum
                else:
                    shutil.copy2(source, target)
                    bytes_copied = target.stat().st_size
                    observed = f"sha256:{_sha256_file(target)}"
            else:
                from .object_store import resolve_object_store

                store = resolve_object_store()
                bytes_copied = store.fetch_to_directory(attempt.source_uri, target)
                if target.is_file():
                    observed = f"sha256:{_sha256_file(target)}"
                else:
                    observed = dataset.checksum
        except Exception as exc:
            attempt.state = MaterializationState.FAILED.value
            attempt.error_message = f"source unavailable: {attempt.source_uri} ({exc})"
            attempt.finished_at = utcnow()
            dataset.status = DatasetReadiness.FAILED.value
            manifest = dict(dataset.manifest)
            manifest["status"] = dataset.status
            dataset.manifest = manifest
            return

        if (
            target.is_file()
            and dataset.checksum.startswith("sha256:")
            and observed != dataset.checksum
        ):
            attempt.state = MaterializationState.FAILED.value
            attempt.error_message = "checksum mismatch"
            attempt.bytes_copied = bytes_copied
            attempt.finished_at = utcnow()
            dataset.status = DatasetReadiness.FAILED.value
            manifest = dict(dataset.manifest)
            manifest["status"] = dataset.status
            dataset.manifest = manifest
            return

        attempt.bytes_copied = bytes_copied
        attempt.checksum = observed
        attempt.state = MaterializationState.SUCCEEDED.value
        attempt.finished_at = utcnow()
        dataset.status = DatasetReadiness.READY.value
        dataset.materialized_uri = attempt.target_uri
        manifest = dict(dataset.manifest)
        manifest["status"] = dataset.status
        manifest["materialized_uri"] = dataset.materialized_uri
        dataset.manifest = manifest
        session.add(
            AuditEventRecord(
                id=new_id("aud"),
                actor_id=attempt.actor_id,
                action="dataset.materialize.succeeded",
                resource_type="dataset_version",
                resource_id=dataset.id,
                details={
                    "attempt_id": attempt.id,
                    "bytes_copied": bytes_copied,
                    "target_uri": attempt.target_uri,
                },
            )
        )


def request_materialization(
    session: Session,
    dataset: DatasetVersionRecord,
    *,
    actor_id: str,
) -> MaterializationAttemptRecord:
    from quictrain_core import MATERIALIZABLE_READINESS

    from quictrain_api.errors import ServiceError

    if is_dataset_archive_uri(dataset.uri):
        raise ServiceError(
            "DATASET_ARCHIVE_MATERIALIZATION_UNSUPPORTED",
            "归档文件需要安全解包和数据集验证，当前物化器不能将归档直接标记为 READY。",
            status_code=409,
        )

    if (
        dataset.status not in MATERIALIZABLE_READINESS
        and dataset.status != DatasetReadiness.READY.value
    ):
        # Allow READY re-trigger as no-op via lease reuse below.
        if dataset.status == DatasetReadiness.MATERIALIZING.value:
            existing = session.scalar(
                select(MaterializationAttemptRecord)
                .where(
                    MaterializationAttemptRecord.dataset_version_id == dataset.id,
                    MaterializationAttemptRecord.state.in_(
                        [MaterializationState.PENDING.value, MaterializationState.RUNNING.value]
                    ),
                )
                .order_by(MaterializationAttemptRecord.number.desc())
            )
            if existing:
                return existing

    lease_key = f"{dataset.id}:content:{dataset.checksum}"
    existing = session.scalar(
        select(MaterializationAttemptRecord).where(
            MaterializationAttemptRecord.dataset_version_id == dataset.id,
            MaterializationAttemptRecord.checksum == dataset.checksum,
            MaterializationAttemptRecord.state == MaterializationState.SUCCEEDED.value,
        )
    )
    if existing and dataset.status == DatasetReadiness.READY.value:
        return existing

    number = (
        session.scalar(
            select(func.coalesce(func.max(MaterializationAttemptRecord.number), 0)).where(
                MaterializationAttemptRecord.dataset_version_id == dataset.id
            )
        )
        or 0
    ) + 1
    target = target_uri_for_dataset(dataset)
    attempt = MaterializationAttemptRecord(
        id=new_id("mat"),
        dataset_version_id=dataset.id,
        number=number,
        state=MaterializationState.PENDING.value,
        lease_key=f"{lease_key}:attempt:{number}",
        source_uri=dataset.uri,
        target_uri=target,
        checksum=dataset.checksum,
        bytes_copied=0,
        actor_id=actor_id,
    )
    session.add(attempt)
    if dataset.status != DatasetReadiness.READY.value:
        dataset.status = DatasetReadiness.MATERIALIZING.value
        manifest = dict(dataset.manifest)
        manifest["status"] = dataset.status
        dataset.manifest = manifest
    session.add(
        AuditEventRecord(
            id=new_id("aud"),
            actor_id=actor_id,
            action="dataset.materialize.requested",
            resource_type="dataset_version",
            resource_id=dataset.id,
            details={"attempt_id": attempt.id, "lease_key": attempt.lease_key},
        )
    )
    session.flush()
    return attempt


def gc_unreferenced_materializations(session: Session, *, keep_uris: set[str]) -> list[str]:
    """Remove materialized directories not referenced by READY datasets. Never deletes keep_uris."""

    removed: list[str] = []
    settings = get_settings()
    root = Path(settings.resolved_materialization_root())
    if not root.exists():
        return removed
    referenced = {
        record.materialized_uri
        for record in session.scalars(select(DatasetVersionRecord)).all()
        if record.materialized_uri
    }
    referenced |= keep_uris
    for path in root.glob("*/*"):
        if not path.is_dir():
            continue
        uri = str(path.resolve())
        if uri in referenced:
            continue
        # Only GC failed/orphaned trees marked by a FAILED attempt target.
        failed = session.scalar(
            select(MaterializationAttemptRecord).where(
                MaterializationAttemptRecord.target_uri == uri,
                MaterializationAttemptRecord.state == MaterializationState.FAILED.value,
            )
        )
        if failed is None:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed.append(uri)
    return removed
