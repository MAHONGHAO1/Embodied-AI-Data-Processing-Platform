"""Platform adapter for QRDF VFR RGB preview artifacts."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import uuid
from collections.abc import Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from filelock import FileLock
from qrdf.models.episode import EpisodeMetadata
from qrdf.models.preview import (
    PreviewGenerationParameters,
    PreviewManifest,
    PreviewStreamArtifact,
    PreviewTimeline,
    PreviewTimelineEntry,
)
from qrdf.reader.episode import Episode

from data.integrations.qrdf.paths import (
    resolve_qrdf_episode_data_file,
    validate_qrdf_external_episode_id,
)
from data.utils.storage_paths import is_under_storage_root, storage_root_path

DEFAULT_PREVIEW_MAX_LONG_EDGE_PX = 1280

logger = logging.getLogger(__name__)


def _load_manifest(preview_root: Path) -> PreviewManifest:
    root = _preview_root_path(preview_root)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("QRDF preview manifest is unavailable")
    return PreviewManifest.load(manifest_path)


def _stream_for(manifest: PreviewManifest, topic: str) -> PreviewStreamArtifact:
    stream = next((item for item in manifest.streams if item.topic == topic), None)
    if stream is None:
        raise ValueError(f"QRDF preview topic is unavailable: {topic}")
    return stream


def resolve_preview_artifact(preview_root: Path, relative_path: str) -> Path:
    """Resolve a manifest artifact without allowing path or symlink escape."""
    root = _preview_root_path(preview_root)
    posix = PurePosixPath(relative_path)
    windows = PureWindowsPath(relative_path)
    if (
        not relative_path
        or Path(relative_path).is_absolute()
        or windows.anchor
        or ".." in posix.parts
        or ".." in windows.parts
    ):
        raise ValueError("preview artifact is outside preview root")
    candidate = root / Path(*posix.parts)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("preview artifact is outside preview root") from exc
    _reject_preview_symlink_components(root, candidate)
    resolved_root = root.resolve(strict=False)
    resolved_candidate = candidate.resolve(strict=False)
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("preview artifact is outside preview root") from exc
    return candidate


def _preview_root_path(preview_root: Path) -> Path:
    root = Path(preview_root)
    if root.is_symlink():
        raise ValueError("QRDF preview root must not be a symbolic link")
    return root


def _reject_preview_symlink_components(root: Path, candidate: Path) -> None:
    """Reject lexical links before resolving so contained links cannot bypass scope."""
    current = root
    try:
        parts = candidate.relative_to(root).parts
    except ValueError as exc:
        raise ValueError("preview artifact is outside preview root") from exc
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("preview artifact must not traverse a symbolic link")


def _validate_manifest_artifacts(preview_root: Path, manifest: PreviewManifest) -> None:
    for stream in manifest.streams:
        for relative_path in (stream.video_path, stream.timeline_path):
            if (
                relative_path
                and not resolve_preview_artifact(preview_root, relative_path).is_file()
            ):
                raise ValueError("QRDF preview manifest references a missing artifact")


def _generation_directory(preview_root: Path, relative_path: str) -> Path:
    parts = PurePosixPath(relative_path).parts
    if len(parts) < 3 or parts[0] != "generations":
        raise ValueError("QRDF preview artifact is outside a generation")
    return resolve_preview_artifact(preview_root, str(PurePosixPath(*parts[:2])))


def _replace_generation(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".staging-{uuid.uuid4().hex}"
    backup = target.parent / f".old-{uuid.uuid4().hex}"
    shutil.copytree(source, staging)
    try:
        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(staging, target)
        except Exception:
            if backup.exists() and not target.exists():
                os.replace(backup, target)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if backup.exists():
            shutil.rmtree(backup)


def publish_qrdf_preview_generation(source_preview_root: Path, target_preview_root: Path) -> Path:
    """Publish complete QRDF generations first and the manifest last."""
    source_root = Path(source_preview_root).resolve()
    target_root = Path(target_preview_root)
    storage_root = storage_root_path()
    if not is_under_storage_root(target_root, root=storage_root):
        raise ValueError("preview publish target is outside storage_root")

    manifest = _load_manifest(source_root)
    _validate_manifest_artifacts(source_root, manifest)
    target_root.mkdir(parents=True, exist_ok=True)

    generation_names: set[str] = set()
    for stream in manifest.streams:
        for relative_path in (stream.video_path, stream.timeline_path):
            if relative_path:
                generation_names.add(_generation_directory(source_root, relative_path).name)

    for generation_name in sorted(generation_names):
        source_generation = source_root / "generations" / generation_name
        target_generation = target_root / "generations" / generation_name
        _replace_generation(source_generation, target_generation)

    _validate_manifest_artifacts(target_root, manifest)
    temporary_manifest = target_root / f".manifest-{uuid.uuid4().hex}.tmp"
    try:
        manifest.save(temporary_manifest)
        os.replace(temporary_manifest, target_root / "manifest.json")
    finally:
        if temporary_manifest.exists():
            temporary_manifest.unlink()
    return target_root.resolve()


def load_qrdf_preview_descriptor(
    preview_root: Path,
    *,
    topic: str,
    media_api: str,
    timeline_api: str,
) -> dict[str, Any] | None:
    """Return a public descriptor without exposing artifact filesystem paths."""
    try:
        manifest = _load_manifest(Path(preview_root))
        stream = _stream_for(manifest, topic)
        if stream.video_path:
            resolve_preview_artifact(Path(preview_root), stream.video_path)
        timeline_entry_count = 0
        if stream.timeline_path:
            timeline_path = resolve_preview_artifact(Path(preview_root), stream.timeline_path)
            timeline_entry_count = len(PreviewTimeline.load(timeline_path).entries)
    except (OSError, ValueError):
        return None
    return {
        "preview_manifest_version": manifest.preview_manifest_version,
        "artifact_id": manifest.artifact_id,
        "topic": stream.topic,
        "status": stream.status,
        "media_api": media_api if stream.video_path else "",
        "timeline_api": timeline_api if stream.timeline_path else "",
        "max_long_edge_px": manifest.parameters.max_edge,
        "source_frame_count": stream.source_frame_count,
        "timeline_entry_count": timeline_entry_count,
        "encoded_frame_count": stream.encoded_frame_count,
        "dropped_frame_count": stream.dropped_frame_count,
        "first_timestamp_ns": stream.first_timestamp_ns,
        "last_timestamp_ns": stream.last_timestamp_ns,
        "failure_reasons": list(stream.failure_reasons),
        "legacy": False,
    }


def resolve_preview_stream_artifact(
    preview_root: Path,
    *,
    topic: str,
    artifact: Literal["media", "timeline"],
) -> Path:
    manifest = _load_manifest(Path(preview_root))
    stream = _stream_for(manifest, topic)
    relative_path = stream.video_path if artifact == "media" else stream.timeline_path
    if not relative_path:
        raise ValueError(f"QRDF preview {artifact} is unavailable")
    path = resolve_preview_artifact(Path(preview_root), relative_path)
    if not path.is_file():
        raise ValueError(f"QRDF preview {artifact} is unavailable")
    return path


def load_preview_timeline(preview_root: Path, *, topic: str) -> dict[str, Any]:
    path = resolve_preview_stream_artifact(preview_root, topic=topic, artifact="timeline")
    return PreviewTimeline.load(path).model_dump(mode="json", exclude_none=True)


def create_derived_rgb_preview(
    source_preview_root: Path,
    *,
    target_root: Path,
    topic: str,
    start_ns: int,
    end_ns: int,
    media_api: str,
    timeline_api: str,
) -> dict[str, Any]:
    """Create a bounded child preview from an already-published source preview.

    The source preview is the only media input.  Derived assets must never
    materialize, slice, or re-encode the source MCAP just to make a playable
    clip.
    """
    if start_ns >= end_ns:
        raise ValueError("derived preview interval is invalid")
    if not topic:
        raise ValueError("derived preview topic is required")

    storage_root = storage_root_path()
    source_root = Path(source_preview_root).resolve()
    destination_root = Path(target_root)
    if not is_under_storage_root(source_root, root=storage_root):
        raise ValueError("derived preview source is outside storage_root")
    if not is_under_storage_root(destination_root, root=storage_root):
        raise ValueError("derived preview destination is outside storage_root")

    source_manifest = _load_manifest(source_root)
    _stream_for(source_manifest, topic)
    source_video = resolve_preview_stream_artifact(source_root, topic=topic, artifact="media")
    source_timeline = PreviewTimeline.load(
        resolve_preview_stream_artifact(source_root, topic=topic, artifact="timeline")
    )
    scoped_entries = [
        entry for entry in source_timeline.entries if start_ns <= int(entry.timestamp_ns) < end_ns
    ]
    scoped_frames = [entry for entry in scoped_entries if entry.kind == "frame"]
    if not scoped_frames:
        raise ValueError("derived preview interval has no encoded RGB frames")

    start_pts_us = int(scoped_frames[0].video_pts_us or "0")
    end_pts_us = _derived_preview_end_pts_us(
        source_timeline.entries,
        end_ns=end_ns,
        start_pts_us=start_pts_us,
    )
    artifact_id = f"derived-{uuid.uuid4().hex}"
    staging_root = destination_root.parent / f".staging-{artifact_id}"
    if not is_under_storage_root(staging_root, root=storage_root):
        raise ValueError("derived preview staging path is outside storage_root")

    try:
        generation_root = staging_root / "generations" / artifact_id
        generation_root.mkdir(parents=True, exist_ok=False)
        relative_video_path = f"generations/{artifact_id}/preview.mp4"
        relative_timeline_path = f"generations/{artifact_id}/preview.timeline.json"
        _transcode_preview_clip(
            source_video,
            generation_root / "preview.mp4",
            start_pts_us=start_pts_us,
            end_pts_us=end_pts_us,
        )
        derived_timeline = PreviewTimeline(
            topic=topic,
            entries=_derived_timeline_entries(scoped_entries, start_pts_us=start_pts_us),
        )
        derived_timeline.save(generation_root / "preview.timeline.json")
        dropped_entries = [entry for entry in scoped_entries if entry.kind == "dropped"]
        stream = PreviewStreamArtifact(
            topic=topic,
            status="partial" if dropped_entries else "complete",
            video_path=relative_video_path,
            timeline_path=relative_timeline_path,
            source_frame_count=len(scoped_entries),
            encoded_frame_count=len(scoped_frames),
            dropped_frame_count=len(dropped_entries),
            first_timestamp_ns=scoped_entries[0].timestamp_ns,
            last_timestamp_ns=scoped_entries[-1].timestamp_ns,
            failure_reasons=sorted(
                {str(entry.reason) for entry in dropped_entries if entry.reason}
            ),
        )
        PreviewManifest(
            artifact_id=artifact_id,
            source=source_manifest.source,
            parameters=PreviewGenerationParameters(
                max_edge=source_manifest.parameters.max_edge,
                keyframe_interval_s=source_manifest.parameters.keyframe_interval_s,
                codec=source_manifest.parameters.codec,
                pixel_format=source_manifest.parameters.pixel_format,
                requested_topics=[topic],
            ),
            generator_version=source_manifest.generator_version,
            streams=[stream],
        ).save(staging_root / "manifest.json")
        publish_qrdf_preview_generation(staging_root, destination_root)
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)

    descriptor = load_qrdf_preview_descriptor(
        destination_root,
        topic=topic,
        media_api=media_api,
        timeline_api=timeline_api,
    )
    if descriptor is None or not descriptor.get("media_api") or not descriptor.get("timeline_api"):
        raise ValueError("derived preview did not publish a usable artifact")
    return descriptor


def _derived_preview_end_pts_us(
    entries: Sequence[PreviewTimelineEntry],
    *,
    end_ns: int,
    start_pts_us: int,
) -> int:
    frame_entries = [entry for entry in entries if entry.kind == "frame"]
    for entry in frame_entries:
        if int(entry.timestamp_ns) >= end_ns:
            return int(entry.video_pts_us or "0")

    last_pts_us = int(frame_entries[-1].video_pts_us or "0")
    if len(frame_entries) >= 2:
        previous_pts_us = int(frame_entries[-2].video_pts_us or "0")
        frame_interval_us = max(last_pts_us - previous_pts_us, 1)
    else:
        frame_interval_us = 33_333
    return max(last_pts_us + frame_interval_us, start_pts_us + 1)


def _derived_timeline_entries(
    entries: Sequence[PreviewTimelineEntry],
    *,
    start_pts_us: int,
) -> list[PreviewTimelineEntry]:
    result: list[PreviewTimelineEntry] = []
    frame_index = 0
    for entry in entries:
        if entry.kind == "frame":
            result.append(
                PreviewTimelineEntry(
                    kind="frame",
                    timestamp_ns=entry.timestamp_ns,
                    frame_index=frame_index,
                    video_pts_us=str(int(entry.video_pts_us or "0") - start_pts_us),
                )
            )
            frame_index += 1
        else:
            result.append(
                PreviewTimelineEntry(
                    kind="dropped",
                    timestamp_ns=entry.timestamp_ns,
                    reason=entry.reason or "preview_frame_dropped",
                    detail=entry.detail,
                )
            )
    return result


def _transcode_preview_clip(
    source: Path,
    target: Path,
    *,
    start_pts_us: int,
    end_pts_us: int,
) -> None:
    """Re-encode a bounded VFR *preview* MP4 without touching raw QRDF data."""
    if end_pts_us <= start_pts_us:
        raise ValueError("derived preview media interval is invalid")
    try:
        import imageio_ffmpeg
    except ImportError as exc:  # pragma: no cover - deployment dependency failure
        raise RuntimeError("preview clip encoder is unavailable") from exc

    target.parent.mkdir(parents=True, exist_ok=True)
    start_seconds = start_pts_us / 1_000_000
    end_seconds = end_pts_us / 1_000_000
    video_filter = f"trim=start={start_seconds:.6f}:end={end_seconds:.6f},setpts=PTS-STARTPTS"
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        video_filter,
        "-vsync",
        "vfr",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-y",
        str(target),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("preview clip transcode failed") from exc
    if completed.returncode != 0 or not target.is_file() or target.stat().st_size <= 0:
        raise RuntimeError("preview clip transcode failed")


def _lock_path(episode_dir: Path, publish_root: Path | None) -> Path:
    identity = (
        f"{Path(episode_dir).resolve()}:{Path(publish_root).resolve() if publish_root else ''}"
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    lock_root = storage_root_path() / "previews" / "locks"
    if not is_under_storage_root(lock_root):
        raise ValueError("preview lock path is outside storage_root")
    lock_root.mkdir(parents=True, exist_ok=True)
    return lock_root / f"{digest}.lock"


def _link_or_copy_regular_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _hardlink_preview_data_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError as exc:
        raise ValueError(
            "QRDF preview source data_file cannot be hard-linked into preview view"
        ) from exc


def _link_safe_tree(
    source_root: Path,
    target_root: Path,
    *,
    excluded_top_level: set[str] | None = None,
    skip_relative_path: PurePosixPath | None = None,
) -> None:
    """Materialize regular files only; preview views never inherit symlinks."""
    excluded = excluded_top_level or set()

    def visit(current: Path) -> None:
        for entry in sorted(current.iterdir(), key=lambda item: item.name):
            relative = PurePosixPath(entry.relative_to(source_root).as_posix())
            if len(relative.parts) == 1 and relative.name in excluded:
                continue
            if skip_relative_path is not None and relative == skip_relative_path:
                continue
            if entry.is_symlink():
                raise ValueError("QRDF preview source cannot contain symbolic links")
            destination = target_root.joinpath(*relative.parts)
            if entry.is_dir():
                destination.mkdir(parents=True, exist_ok=False)
                visit(entry)
            elif entry.is_file():
                _link_or_copy_regular_file(entry, destination)
            else:
                raise ValueError("QRDF preview source contains an unsupported filesystem entry")

    visit(source_root)


def _preview_view_staging_root(episode_path: Path) -> Path:
    storage_root = storage_root_path()
    if not is_under_storage_root(episode_path, root=storage_root):
        raise ValueError("QRDF preview source is outside storage_root")
    staging_root = (
        storage_root / "process" / "preview" / ".staging" / f"episode-view-{uuid.uuid4().hex}"
    )
    if not is_under_storage_root(staging_root, root=storage_root):
        raise ValueError("QRDF preview staging path is outside storage_root")
    return staging_root


def _load_preview_metadata(episode_path: Path) -> EpisodeMetadata:
    metadata_path = Path(episode_path) / "metadata.json"
    try:
        resolved_metadata = metadata_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("QRDF preview source metadata is unavailable") from exc
    if not is_under_storage_root(resolved_metadata, root=storage_root_path()):
        raise ValueError("QRDF preview source metadata must resolve inside storage_root")
    if not resolved_metadata.is_file():
        raise ValueError("QRDF preview source metadata is unavailable")
    return EpisodeMetadata.load(resolved_metadata)


def _safe_source_preview_root(source_root: Path, *, storage_root: Path) -> Path | None:
    media_root = source_root / "media"
    if not media_root.exists() and not media_root.is_symlink():
        return None
    if media_root.is_symlink() or not media_root.is_dir():
        raise ValueError("QRDF preview source media is not a directory")
    resolved_media = media_root.resolve()
    if not is_under_storage_root(resolved_media, root=storage_root):
        raise ValueError("QRDF preview source media must resolve inside storage_root")
    try:
        resolved_media.relative_to(source_root)
    except ValueError as exc:
        raise ValueError(
            "QRDF preview source media must resolve inside the episode directory"
        ) from exc

    preview_root = resolved_media / "preview"
    if not preview_root.exists() and not preview_root.is_symlink():
        return None
    if preview_root.is_symlink() or not preview_root.is_dir():
        raise ValueError("QRDF preview source media is not a directory")
    resolved_preview = preview_root.resolve()
    if not is_under_storage_root(resolved_preview, root=storage_root):
        raise ValueError("QRDF preview source media must resolve inside storage_root")
    try:
        resolved_preview.relative_to(source_root)
    except ValueError as exc:
        raise ValueError(
            "QRDF preview source media must resolve inside the episode directory"
        ) from exc
    return resolved_preview


def _cleanup_preview_staging(staging_root: Path) -> None:
    try:
        shutil.rmtree(staging_root)
    except OSError:
        logger.warning(
            "QRDF preview staging cleanup failed",
            extra={"preview_staging_kind": "identity_view"},
        )


def _create_identity_matched_preview_view(
    source_episode: Path,
    staging_root: Path,
    *,
    metadata: EpisodeMetadata,
) -> Path:
    """Build a temporary SDK-valid episode view without changing raw data."""
    source_root = Path(source_episode).resolve()
    storage_root = storage_root_path()
    if not is_under_storage_root(source_root, root=storage_root):
        raise ValueError("QRDF preview source is outside storage_root")
    episode_id = validate_qrdf_external_episode_id(metadata.episode_id)
    data_relative = PurePosixPath(metadata.data_file)
    source_mcap = resolve_qrdf_episode_data_file(metadata, source_root)
    if not source_mcap.is_file():
        raise ValueError("QRDF preview source data_file is unavailable")

    view = Path(staging_root) / episode_id
    if not is_under_storage_root(view, root=storage_root):
        raise ValueError("QRDF preview view is outside storage_root")
    view.mkdir(parents=True, exist_ok=False)
    _link_safe_tree(
        source_root,
        view,
        excluded_top_level={"media"},
        skip_relative_path=data_relative,
    )
    view_mcap = view.joinpath(*data_relative.parts)
    _hardlink_preview_data_file(source_mcap, view_mcap)

    source_preview_root = _safe_source_preview_root(source_root, storage_root=storage_root)
    if source_preview_root is not None:
        _link_safe_tree(source_preview_root, view / "media" / "preview")
    return view


def ensure_qrdf_rgb_preview(
    episode_dir: Path,
    *,
    topic: str,
    media_api: str,
    timeline_api: str,
    publish_root: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Generate/reuse one QRDF topic and return its public descriptor."""
    target_root = ensure_qrdf_rgb_previews(
        episode_dir,
        topics=[topic],
        publish_root=publish_root,
        force=force,
    )
    descriptor = load_qrdf_preview_descriptor(
        target_root,
        topic=topic,
        media_api=media_api,
        timeline_api=timeline_api,
    )
    if descriptor is None:
        raise ValueError("QRDF preview generation did not publish a usable manifest")
    return descriptor


def ensure_qrdf_rgb_previews(
    episode_dir: Path,
    *,
    topics: Sequence[str],
    publish_root: Path | None = None,
    force: bool = False,
) -> Path:
    """Generate/reuse a complete QRDF topic set and return its preview root."""
    episode_path = Path(episode_dir).resolve()
    storage_root = storage_root_path()
    if not is_under_storage_root(episode_path, root=storage_root):
        raise ValueError("QRDF preview source is outside storage_root")
    target_root = (
        Path(publish_root) if publish_root else episode_path / "media" / "preview"
    ).resolve()
    if not is_under_storage_root(target_root, root=storage_root):
        raise ValueError("QRDF preview publish target is outside storage_root")
    with FileLock(str(_lock_path(episode_path, publish_root))):
        metadata = _load_preview_metadata(episode_path)
        staging_root: Path | None = None
        generation_path = episode_path
        should_publish = publish_root is not None
        try:
            if metadata.episode_id != episode_path.name:
                staging_root = _preview_view_staging_root(episode_path)
                generation_path = _create_identity_matched_preview_view(
                    episode_path,
                    staging_root,
                    metadata=metadata,
                )
                should_publish = True
            episode = Episode(generation_path)
            episode.generate_rgb_previews(
                camera_topics=list(dict.fromkeys(topics)),
                max_edge=DEFAULT_PREVIEW_MAX_LONG_EDGE_PX,
                force=force,
            )
            source_root = generation_path / "media" / "preview"
            if should_publish:
                target_root = publish_qrdf_preview_generation(source_root, target_root)
            _validate_manifest_artifacts(target_root, _load_manifest(target_root))
            return Path(target_root).resolve()
        finally:
            if staging_root is not None and staging_root.exists():
                _cleanup_preview_staging(staging_root)
