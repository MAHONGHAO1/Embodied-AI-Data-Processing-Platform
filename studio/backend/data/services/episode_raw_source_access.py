"""Authorized delivery helpers for one persisted Source Episode raw package."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from data.database import Episode, EpisodeArtifact
from data.infra import oss_client
from data.security.signed_url import create_signed_download_token
from data.services.batch_paths import raw_episode_source_prefix
from data.utils.storage_paths import cloud_path_is_safe, is_under_storage_root, storage_root_path
from data.utils.storage_uri import parse_storage_uri

_LOCAL_RAW_SOURCE_TTL_SECONDS = 600
_LOCAL_RAW_SOURCE_MAX_USES = 1
_RAW_SOURCE_SIGN_TIMEOUT_SECONDS = 3.0
_raw_source_sign_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="raw-source-sign")


@dataclass(frozen=True)
class RawSourceAccess:
    url: str
    expires_at: str
    media_type: str
    direct: bool

    def as_payload(self) -> dict[str, object]:
        return {
            "direct": self.direct,
            "url": self.url,
            "expires_at": self.expires_at,
            "media_type": self.media_type,
        }


def raw_source_file_names(*, episode: Episode, artifact: EpisodeArtifact) -> tuple[str, ...]:
    """Return the small, known file set for an already-registered raw source."""
    if _raw_source_location(episode=episode, artifact=artifact) is None:
        return ()
    metadata = episode.metadata_json if isinstance(episode.metadata_json, dict) else {}
    source = metadata.get("source") if isinstance(metadata.get("source"), dict) else {}
    data_file = _safe_relative_file_name(
        source.get("data_file") if isinstance(source, dict) else None
    )
    data_file = data_file or "data.mcap"
    return tuple(dict.fromkeys((data_file, "metadata.json", "complete.json")))


def issue_raw_source_file_access(
    *,
    episode: Episode,
    artifact: EpisodeArtifact,
    file_name: str,
    actor_id: int | None,
) -> RawSourceAccess | None:
    """Issue only an exact file delivery capability after router authorization."""
    location = _raw_source_location(episode=episode, artifact=artifact)
    normalized_file = _safe_relative_file_name(file_name)
    if (
        location is None
        or normalized_file is None
        or normalized_file
        not in raw_source_file_names(
            episode=episode,
            artifact=artifact,
        )
    ):
        return None
    scheme, bucket, prefix = location
    if scheme == "oss":
        if not oss_client.browser_direct_enabled():
            return None
        key = f"{prefix}/{normalized_file}"
        if not cloud_path_is_safe(bucket, key) or not oss_client.browser_direct_object_allowed(
            bucket, key
        ):
            return None

        def sign() -> str | None:
            if not oss_client.object_exists(bucket, key):
                return None
            return oss_client.sign_browser_get_url(
                bucket,
                key,
                download_name=_download_name(episode, normalized_file),
            )

        try:
            signed_url = _run_with_timeout(sign, timeout_seconds=_RAW_SOURCE_SIGN_TIMEOUT_SECONDS)
        except (TimeoutError, ValueError, OSError):
            return None
        if not signed_url:
            return None
        ttl = oss_client.browser_direct_ttl_seconds()
        return RawSourceAccess(
            url=signed_url,
            expires_at=(datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat(),
            media_type=_media_type(normalized_file),
            direct=True,
        )

    if actor_id is None or actor_id <= 0 or oss_client.is_oss_configured():
        return None
    if (
        raw_source_local_file_path(episode=episode, artifact=artifact, file_name=normalized_file)
        is None
    ):
        return None
    token = create_signed_download_token(
        resource_type="episode_raw_source",
        resource_id=episode.id,
        subject=str(actor_id),
        ttl_seconds=_LOCAL_RAW_SOURCE_TTL_SECONDS,
        max_uses=_LOCAL_RAW_SOURCE_MAX_USES,
        extra={"artifact_id": artifact.id, "file": normalized_file},
    )
    return RawSourceAccess(
        url=(
            f"/api/v1/episodes/{episode.id}/raw-source/media?file={quote(normalized_file, safe='')}&sig="
            f"{quote(token, safe='')}"
        ),
        expires_at=(
            datetime.now(timezone.utc) + timedelta(seconds=_LOCAL_RAW_SOURCE_TTL_SECONDS)
        ).isoformat(),
        media_type=_media_type(normalized_file),
        direct=False,
    )


def raw_source_local_file_path(
    *,
    episode: Episode,
    artifact: EpisodeArtifact,
    file_name: str,
) -> Path | None:
    """Resolve a local raw file only under its exact canonical source root."""
    location = _raw_source_location(episode=episode, artifact=artifact)
    normalized_file = _safe_relative_file_name(file_name)
    if (
        location is None
        or normalized_file is None
        or normalized_file
        not in raw_source_file_names(
            episode=episode,
            artifact=artifact,
        )
    ):
        return None
    scheme, _bucket, prefix = location
    if scheme != "nas":
        return None
    root = storage_root_path()
    relative_parts = (*PurePosixPath(prefix).parts, *PurePosixPath(normalized_file).parts)
    current = root
    for part in relative_parts:
        current = current / part
        if current.is_symlink():
            return None
    try:
        resolved = current.resolve()
    except OSError:
        return None
    if not resolved.is_file() or not is_under_storage_root(resolved, root=root):
        return None
    return resolved


def raw_source_file_size_bytes(
    *,
    episode: Episode,
    artifact: EpisodeArtifact,
    file_name: str,
) -> int | None:
    """Return a verified raw file size without probing an untrusted location."""
    location = _raw_source_location(episode=episode, artifact=artifact)
    normalized_file = _safe_relative_file_name(file_name)
    if (
        location is None
        or normalized_file is None
        or normalized_file
        not in raw_source_file_names(
            episode=episode,
            artifact=artifact,
        )
    ):
        return None
    scheme, _bucket, _prefix = location
    if scheme == "nas":
        local_path = raw_source_local_file_path(
            episode=episode,
            artifact=artifact,
            file_name=normalized_file,
        )
        if local_path is None:
            return None
        try:
            size = local_path.stat().st_size
        except OSError:
            return None
        return size if size >= 0 else None
    if scheme != "oss":
        return None

    operations = [
        operation
        for operation in artifact.operations
        if (
            operation.artifact_id == artifact.id
            and operation.operation_kind == "raw_source_publish"
            and operation.status == "published"
            and operation.target_uri == artifact.storage_uri
        )
    ]
    if len(operations) != 1:
        return None
    manifest = operations[0].manifest_json
    if not isinstance(manifest, dict) or manifest.get("kind") != "directory":
        return None
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        return None
    matching_sizes: list[int] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("path") != normalized_file:
            continue
        size = entry.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            return None
        matching_sizes.append(size)
    return matching_sizes[0] if len(matching_sizes) == 1 else None


def raw_source_artifact_for_episode(*, episode: Episode) -> EpisodeArtifact | None:
    """Choose the only raw source artifact that belongs to this Source Episode."""
    candidates = [
        artifact
        for artifact in episode.artifacts
        if artifact.artifact_type == "raw_source"
        and artifact.storage_role == "raw"
        and artifact.episode_id == episode.id
    ]
    return candidates[0] if len(candidates) == 1 else None


def _raw_source_location(
    *,
    episode: Episode,
    artifact: EpisodeArtifact,
) -> tuple[str, str, str] | None:
    if (
        episode.kind != "source"
        or artifact.episode_id != episode.id
        or artifact.artifact_type != "raw_source"
        or artifact.storage_role != "raw"
    ):
        return None
    try:
        expected_prefix = raw_episode_source_prefix(
            workspace_id=episode.workspace_id,
            task_set_id=episode.task_set_id,
            batch_id=episode.batch_id,
            episode_id=episode.id,
            artifact_id=artifact.id,
        )
    except (TypeError, ValueError):
        return None
    uri = str(artifact.storage_uri or "")
    if uri.startswith("oss://"):
        try:
            bucket, key = parse_storage_uri(uri)
        except ValueError:
            return None
        if bucket != oss_client.bucket_name("raw") or key != expected_prefix:
            return None
        return "oss", bucket, expected_prefix
    if uri == f"nas://{expected_prefix}":
        return "nas", "", expected_prefix
    return None


def _safe_relative_file_name(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or "\\" in value or "\x00" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _download_name(episode: Episode, file_name: str) -> str:
    safe_leaf = Path(file_name).name
    return f"{episode.episode_uid}-{safe_leaf}"[:180]


def _media_type(file_name: str) -> str:
    return "application/json" if file_name.endswith(".json") else "application/octet-stream"


def _run_with_timeout(fn, *, timeout_seconds: float):
    future = _raw_source_sign_executor.submit(fn)
    try:
        return future.result(timeout=timeout_seconds)
    except FuturesTimeout as exc:
        future.cancel()
        raise TimeoutError("raw source signing timed out") from exc
