"""Bounded immutable source cache for official Episode publication."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from qrdf.models.episode import EpisodeMetadata

from data.config import settings
from data.infra import oss_client
from data.integrations.qrdf.paths import resolve_qrdf_episode_data_file
from data.utils.checksums import legacy_batch_tree_sha256, tree_sha256
from data.utils.storage_paths import cloud_path_is_safe, is_under_storage_root, storage_root_path
from data.utils.storage_uri import is_public_storage_uri, parse_storage_uri

logger = logging.getLogger("quicdata.publication_source_cache")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CACHE_KEY_RE = _SHA256_RE
_MARKER_NAME = "complete.json"
_SOURCE_NAME = "source"


@dataclass(frozen=True)
class PublicationSourceFacts:
    artifact_id: int
    storage_uri: str
    checksum_sha256: str

    @classmethod
    def from_artifact(cls, artifact: object) -> PublicationSourceFacts:
        return cls(
            artifact_id=int(artifact.id),
            storage_uri=str(artifact.storage_uri),
            checksum_sha256=str(getattr(artifact, "checksum_sha256", "") or "").lower(),
        )


@contextmanager
def acquire_publication_source(facts: PublicationSourceFacts) -> Iterator[Path]:
    """Yield one verified QRDF source while holding its shared cache lock."""
    normalized = _validate_facts(facts)
    root, entries, locks, staging_root = _cache_paths()
    cleanup_publication_source_cache()
    key = _cache_key(normalized)
    entry = entries / key
    staging: Path | None = None

    with _open_lock(locks / f"{key}.lock", exclusive=True) as lock_file:
        if entry.exists() or entry.is_symlink():
            source = _validate_entry(entry, normalized)
            logger.info(
                "publication source cache hit artifact_id=%s cache_key=%s",
                normalized.artifact_id,
                key[:12],
            )
        else:
            staging = Path(tempfile.mkdtemp(prefix=f"{key}-", dir=staging_root))
            try:
                source = _materialize_source(normalized, staging=staging)
                _validate_source(source, expected_checksum=normalized.checksum_sha256)
                marker = _marker_payload(normalized)
                (staging / _MARKER_NAME).write_text(
                    json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
                os.replace(staging, entry)
                staging = None
                source = _validate_entry(entry, normalized)
                logger.info(
                    "publication source cache miss artifact_id=%s cache_key=%s",
                    normalized.artifact_id,
                    key[:12],
                )
            finally:
                if staging is not None and (staging.exists() or staging.is_symlink()):
                    _remove_staging(staging, staging_root=staging_root)

        os.utime(entry, None, follow_symlinks=False)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
        yield source


def cleanup_publication_source_cache(
    *,
    ttl_seconds: int | None = None,
    max_bytes: int | None = None,
) -> dict[str, int]:
    """Remove only unlocked direct cache entries, oldest first."""
    ttl = int(settings.publication_source_cache_ttl_seconds if ttl_seconds is None else ttl_seconds)
    capacity = int(settings.publication_source_cache_max_bytes if max_bytes is None else max_bytes)
    if ttl < 0 or capacity < 0:
        raise ValueError("publication source cache bounds must be non-negative")
    _root, entries, locks, staging_root = _cache_paths()
    now = time.time()
    removed = 0
    reclaimed = 0
    candidates: list[tuple[float, int, Path]] = []

    for entry in entries.iterdir():
        if entry.is_symlink() or not entry.is_dir() or _CACHE_KEY_RE.fullmatch(entry.name) is None:
            logger.warning("unsafe publication cache entry skipped name=%s", entry.name)
            continue
        try:
            size = _tree_size(entry)
            mtime = entry.stat(follow_symlinks=False).st_mtime
        except (OSError, ValueError) as exc:
            logger.warning(
                "publication cache entry inspection failed cache_key=%s error_type=%s",
                entry.name[:12],
                type(exc).__name__,
            )
            continue
        candidates.append((mtime, size, entry))

    total = sum(size for _mtime, size, _entry in candidates)
    for mtime, size, entry in sorted(candidates, key=lambda value: value[0]):
        if now - mtime <= ttl and total <= capacity:
            continue
        try:
            with _open_lock(locks / f"{entry.name}.lock", exclusive=True, nonblocking=True):
                _remove_entry(entry, entries=entries)
        except BlockingIOError:
            continue
        total -= size
        removed += 1
        reclaimed += size

    _cleanup_staging(staging_root, locks=locks, older_than=max(ttl, 300), now=now)
    return {
        "removed_entries": removed,
        "reclaimed_bytes": reclaimed,
        "remaining_bytes": max(total, 0),
    }


def _validate_facts(facts: PublicationSourceFacts) -> PublicationSourceFacts:
    if (
        not isinstance(facts.artifact_id, int)
        or isinstance(facts.artifact_id, bool)
        or facts.artifact_id <= 0
    ):
        raise ValueError("publication source artifact id is invalid")
    storage_uri = str(facts.storage_uri or "").strip()
    if not is_public_storage_uri(storage_uri):
        raise ValueError("publication source storage URI is invalid")
    checksum = str(facts.checksum_sha256 or "").strip().lower()
    if checksum and _SHA256_RE.fullmatch(checksum) is None:
        raise ValueError("publication source checksum is invalid")
    return PublicationSourceFacts(facts.artifact_id, storage_uri, checksum)


def _cache_key(facts: PublicationSourceFacts) -> str:
    identity = f"{facts.artifact_id}\0{facts.storage_uri}\0{facts.checksum_sha256}".encode()
    return hashlib.sha256(identity).hexdigest()


def _cache_paths() -> tuple[Path, Path, Path, Path]:
    storage_root = storage_root_path()
    root = storage_root / "hot" / "publication-source-cache"
    entries = root / "entries"
    locks = root / "locks"
    staging = root / ".tmp"
    for path in (root, entries, locks, staging):
        path.mkdir(parents=True, exist_ok=True)
        if (
            path.is_symlink()
            or not path.is_dir()
            or not is_under_storage_root(path, root=storage_root)
        ):
            raise ValueError("publication source cache path is unsafe")
    return root, entries, locks, staging


@contextmanager
def _open_lock(path: Path, *, exclusive: bool, nonblocking: bool = False) -> Iterator[object]:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    lock_file = os.fdopen(descriptor, "a+b", buffering=0)
    try:
        if not stat.S_ISREG(os.fstat(lock_file.fileno()).st_mode):
            raise ValueError("publication source cache lock is not a regular file")
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if nonblocking:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(lock_file.fileno(), operation)
        except OSError as exc:
            if nonblocking and exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise BlockingIOError from exc
            raise
        yield lock_file
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def _materialize_source(facts: PublicationSourceFacts, *, staging: Path) -> Path:
    source = staging / _SOURCE_NAME
    if facts.storage_uri.startswith("oss://"):
        bucket, key = parse_storage_uri(facts.storage_uri)
        if not cloud_path_is_safe(bucket, key):
            raise ValueError("publication source object path is invalid")
        materialized = Path(oss_client.download_to(source, bucket, key)).resolve()
        if materialized != source.resolve():
            raise ValueError("publication source downloader returned an unsafe path")
        return source

    relative = facts.storage_uri.removeprefix("nas://")
    parts = PurePosixPath(relative).parts
    if (
        not relative
        or relative.startswith(("/", "~"))
        or PureWindowsPath(relative).drive
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError("publication NAS source path is invalid")
    storage_root = storage_root_path()
    origin = (storage_root / Path(*parts)).resolve()
    if (
        not is_under_storage_root(origin, root=storage_root)
        or not origin.is_dir()
        or origin.is_symlink()
    ):
        raise ValueError("publication raw source is unavailable")
    shutil.copytree(origin, source, symlinks=True)
    return source


def _validate_entry(entry: Path, facts: PublicationSourceFacts) -> Path:
    if entry.is_symlink() or not entry.is_dir():
        raise ValueError("publication source cache entry is unsafe")
    marker_path = entry / _MARKER_NAME
    if marker_path.is_symlink() or not marker_path.is_file():
        raise ValueError("publication source cache marker is unavailable")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("publication source cache marker is invalid") from exc
    if marker != _marker_payload(facts):
        raise ValueError("publication source cache marker does not match the artifact")
    source = entry / _SOURCE_NAME
    _validate_source(source, expected_checksum="")
    return source


def _validate_source(source: Path, *, expected_checksum: str) -> None:
    _require_safe_tree(source)
    metadata_path = source / "metadata.json"
    if metadata_path.is_symlink() or not metadata_path.is_file():
        raise ValueError("publication source metadata is unavailable")
    metadata = EpisodeMetadata.load(metadata_path)
    data_file = resolve_qrdf_episode_data_file(metadata, source)
    if (
        data_file.is_symlink()
        or not data_file.is_file()
        or not is_under_storage_root(data_file, root=source.resolve())
    ):
        raise ValueError("publication source data_file must be inside the QRDF directory")
    if expected_checksum:
        actual_checksum = tree_sha256(source)
        if (
            actual_checksum != expected_checksum
            and legacy_batch_tree_sha256(source) != expected_checksum
        ):
            raise ValueError("publication source checksum does not match the artifact")


def _require_safe_tree(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("publication source cache contains a symlink or invalid root")
    for directory, names, files in os.walk(root, followlinks=False):
        current = Path(directory)
        if current.is_symlink():
            raise ValueError("publication source cache contains a symlink")
        for name in [*names, *files]:
            item = current / name
            if item.is_symlink():
                raise ValueError("publication source cache contains a symlink")
            if not item.is_dir() and not item.is_file():
                raise ValueError("publication source cache contains an unsupported entry")


def _marker_payload(facts: PublicationSourceFacts) -> dict[str, object]:
    return {
        "schema": "quicdata.publication_source_cache.v1",
        "artifact_id": facts.artifact_id,
        "storage_uri": facts.storage_uri,
        "checksum_sha256": facts.checksum_sha256,
    }


def _tree_size(root: Path) -> int:
    _require_safe_tree(root)
    return sum(
        item.stat(follow_symlinks=False).st_size for item in root.rglob("*") if item.is_file()
    )


def _remove_entry(entry: Path, *, entries: Path) -> None:
    if (
        entry.is_symlink()
        or not entry.is_dir()
        or entry.parent.resolve() != entries.resolve()
        or _CACHE_KEY_RE.fullmatch(entry.name) is None
    ):
        raise ValueError("publication source cache cleanup target is unsafe")
    shutil.rmtree(entry)


def _remove_staging(staging: Path, *, staging_root: Path) -> None:
    if (
        staging.is_symlink()
        or staging.parent.resolve() != staging_root.resolve()
        or not staging.name
    ):
        raise ValueError("publication source cache staging target is unsafe")
    shutil.rmtree(staging)


def _cleanup_staging(staging_root: Path, *, locks: Path, older_than: int, now: float) -> None:
    for staging in staging_root.iterdir():
        if staging.is_symlink() or not staging.is_dir():
            logger.warning("unsafe publication cache staging entry skipped name=%s", staging.name)
            continue
        cache_key = staging.name[:64]
        if _CACHE_KEY_RE.fullmatch(cache_key) is None or staging.name[64:65] != "-":
            logger.warning(
                "unrecognized publication cache staging entry skipped name=%s", staging.name
            )
            continue
        try:
            if now - staging.stat(follow_symlinks=False).st_mtime <= older_than:
                continue
            with _open_lock(locks / f"{cache_key}.lock", exclusive=True, nonblocking=True):
                _remove_staging(staging, staging_root=staging_root)
        except BlockingIOError:
            continue
        except OSError as exc:
            logger.warning(
                "publication cache staging cleanup failed error_type=%s",
                type(exc).__name__,
            )
