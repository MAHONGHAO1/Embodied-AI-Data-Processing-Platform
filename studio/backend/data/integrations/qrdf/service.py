"""QRDF SDK adapter layer: wraps vendored qrdf package for QuicData business modules."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from qrdf.reader.reader import QRDFReader
from qrdf.validator.validator import QRDFValidator

from data.config import settings
from data.integrations.qrdf.lerobot_export import convert_dataset_extended
from data.integrations.qrdf.paths import resolve_legacy_qrdf_preview_file
from data.integrations.qrdf.preview_export import (
    _pick_camera_topic,
    export_episode_preview,
    infer_dataset_fps,
    mp4_is_browser_ready,
)
from data.utils.storage_paths import resolve_storage_path
from data.utils.storage_uri import is_cloud_uri

_validator = QRDFValidator()
_EPISODE_ID_PATTERN = re.compile(r"^episode_\d{6}$")

INCOMPATIBLE_QRDF_FORMAT = (
    "数据格式不兼容标准 QRDF：需包含 dataset.json 与 episodes/ 目录，"
    "或为单 episode 包（episode_XXXXXX/metadata.json + data.mcap）"
)


def _find_single_episode_dir(root: Path) -> Path | None:
    """Detect single episode package directory (metadata.json in episode directory, no dataset.json)."""
    if _EPISODE_ID_PATTERN.match(root.name) and (root / "metadata.json").is_file():
        return root
    if not root.is_dir():
        return None
    # Flat single package: decompressed root is the episode itself (metadata.json + data.mcap)
    if (root / "metadata.json").is_file() and (
        (root / "data.mcap").is_file() or any(root.glob("*.mcap"))
    ):
        # Exclude if already a standard dataset (containing episodes/)
        if not (root / "episodes").is_dir() and not (root / "dataset.json").is_file():
            return root
    matches = [
        p
        for p in root.iterdir()
        if p.is_dir() and _EPISODE_ID_PATTERN.match(p.name) and (p / "metadata.json").is_file()
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _resolve_episode_id_for_promote(episode_dir: Path) -> str:
    """Determine episode_id when promoting a single episode to standard layout."""
    if _EPISODE_ID_PATTERN.match(episode_dir.name):
        return episode_dir.name
    try:
        meta = json.loads((episode_dir / "metadata.json").read_text(encoding="utf-8"))
        eid = str(meta.get("episode_id") or "").strip()
        if _EPISODE_ID_PATTERN.match(eid):
            return eid
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return "episode_000001"


def materialize_single_episode_as_dataset(
    episode_dir: Path,
    *,
    source_key: str | None = None,
) -> Path:
    """Copy a single-episode package into standard QRDF dataset layout (dataset.json + episodes/).

    Read-only compatibility paths do not modify the original directory; when preprocessing requires a standard layout, it writes to hot/qrdf_promote/.
    """
    digest = hashlib.sha256(f"{source_key or ''}::{episode_dir.resolve()}".encode()).hexdigest()[
        :16
    ]
    dest_root = Path(settings.storage_root) / "hot" / "qrdf_promote" / digest
    episode_id = _resolve_episode_id_for_promote(episode_dir)
    dest_ep = dest_root / "episodes" / episode_id
    manifest_path = dest_root / "dataset.json"

    if manifest_path.is_file() and dest_ep.is_dir() and any(dest_ep.iterdir()):
        return dest_root

    if dest_root.exists():
        shutil.rmtree(dest_root, ignore_errors=True)
    dest_ep.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(episode_dir, dest_ep)

    # If target dir name is already episode_id but source is flat package, backfill episode_id in metadata
    meta_file = dest_ep / "metadata.json"
    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if not meta.get("episode_id"):
                meta["episode_id"] = episode_id
                meta_file.write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    manifest = {
        "qrdf_version": "0.1",
        "dataset_name": f"promoted_{episode_id}",
        "episode_count": 1,
        "episodes": [episode_id],
        "splits": {"train": [episode_id], "val": [], "test": []},
        "storage": {"container": "mcap", "timestamp_unit": "ns"},
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return dest_root


def ensure_processable_dataset_path(
    storage_path: str | None,
    *,
    source_uri: str | None = None,
) -> Path | None:
    """Resolve to a standard QRDF dataset path ready for preprocessing; single-episode packages are automatically promoted in layout."""
    layout = classify_storage_layout(storage_path, source_uri=source_uri)
    if layout["kind"] == "standard" and layout.get("root"):
        resolved = _resolve_standard_dataset_root(layout["root"])
        return resolved
    if layout["kind"] == "single_episode" and layout.get("episode_dir"):
        return materialize_single_episode_as_dataset(
            layout["episode_dir"],
            source_key=str(source_uri or storage_path or ""),
        )
    return None


def _resolve_standard_dataset_root(root: Path) -> Path | None:
    if (root / "dataset.json").exists() or (root / "episodes").is_dir():
        return root
    if root.is_dir():
        for child in root.iterdir():
            if child.is_dir() and (
                (child / "dataset.json").exists() or (child / "episodes").is_dir()
            ):
                return child
    return None


def _ensure_zip_extracted(zip_path: Path, *, source_uri: str | None = None) -> Path | None:
    from data.services.cloud_storage import zip_extract_hot_dir

    cache_source = source_uri or zip_path
    extract_dir = zip_extract_hot_dir(cache_source)
    if extract_dir.exists():
        if any(extract_dir.iterdir()):
            return extract_dir
        extract_dir.rmdir()

    # Legacy compatibility: extracted directory was previously placed next to cloud mirror zip ({stem}_qrdf)
    legacy_dir = zip_path.parent / f"{zip_path.stem}_qrdf"
    if legacy_dir.exists() and any(legacy_dir.iterdir()):
        return legacy_dir

    try:
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)
        # In cleanup phase, look up cache dir by source to avoid long-term accumulation in hot/zip_extract.
        (extract_dir / ".source_ref").write_text(str(cache_source), encoding="utf-8")
    except zipfile.BadZipFile:
        if extract_dir.exists() and not any(extract_dir.iterdir()):
            extract_dir.rmdir()
        return None
    return extract_dir


def _zip_cache_source_uri(
    storage_path: str | None, source_uri: str | None = None
) -> str | Path | None:
    """Stable identifier for zip extraction cache: prioritize oss:// for alignment with cleanup_zip_extract_staging."""
    if source_uri and is_cloud_uri(source_uri):
        return source_uri
    if storage_path and is_cloud_uri(storage_path):
        return storage_path
    return None


def _resolve_content_root(
    storage_path: str | None, *, source_uri: str | None = None
) -> Path | None:
    """Resolve content root: only accepts sandbox paths from resolve_storage_path, disallows fallback to absolute paths."""
    path = resolve_storage_path(storage_path)
    if not path:
        return None
    if path.is_file() and path.suffix.lower() == ".zip":
        cache_uri = _zip_cache_source_uri(storage_path, source_uri)
        return _ensure_zip_extracted(path, source_uri=cache_uri)
    return path


def classify_storage_layout(
    storage_path: str | None, *, source_uri: str | None = None
) -> dict[str, Any]:
    """Identify storage layout: standard QRDF / single-episode package / incompatible (read-only, does not modify directory)."""
    root = _resolve_content_root(storage_path, source_uri=source_uri)
    if not root:
        return {
            "kind": "missing",
            "root": None,
            "episode_dir": None,
            "error": "存储路径无效或数据文件不存在",
        }

    standard = _resolve_standard_dataset_root(root)
    if standard:
        return {"kind": "standard", "root": standard, "episode_dir": None, "error": ""}

    episode_dir = _find_single_episode_dir(root)
    if episode_dir:
        return {"kind": "single_episode", "root": root, "episode_dir": episode_dir, "error": ""}

    return {
        "kind": "incompatible",
        "root": root,
        "episode_dir": None,
        "error": INCOMPATIBLE_QRDF_FORMAT,
    }


def resolve_dataset_path(storage_path: str | None, *, source_uri: str | None = None) -> Path | None:
    """Resolve storage_path to a standard QRDF dataset directory (returns None for single-episode package)."""
    layout = classify_storage_layout(storage_path, source_uri=source_uri)
    if layout["kind"] == "standard":
        return layout["root"]
    return None


def _extract_zip_dataset(zip_path: Path) -> Path | None:
    return resolve_dataset_path(str(zip_path))


def _load_episode_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _summarize_episode_dir(episode_dir: Path, *, allow_mcap: bool = True) -> dict[str, Any]:
    """Summarize episode from metadata.json / metrics.json; fallback to parsing MCAP only when metrics are missing and allowed."""
    from data.integrations.qrdf.episode_metrics import (
        load_metrics_json,
        pick_camera_topic_from_names,
        topic_names_from_episode_dir,
    )

    meta = _load_episode_json(episode_dir / "metadata.json")
    timing = meta.get("timing") if isinstance(meta.get("timing"), dict) else {}
    duration = timing.get("duration_s")
    topics = topic_names_from_episode_dir(episode_dir)
    metrics = load_metrics_json(episode_dir)
    stored_counts = (
        metrics.get("message_count") if isinstance(metrics.get("message_count"), dict) else None
    )
    topic_stats: dict[str, int] = dict(stored_counts) if stored_counts else {}
    source = (
        "metrics"
        if stored_counts
        else ("metadata" if topics or duration is not None else "unknown")
    )

    if not topic_stats and allow_mcap:
        try:
            from qrdf.reader.episode import Episode

            episode = Episode(episode_dir)
            return _summarize_episode(episode, allow_mcap=True)
        except Exception:
            pass

    camera_topic = pick_camera_topic_from_names(topics)
    if camera_topic and camera_topic in topic_stats:
        frame_count = topic_stats[camera_topic]
    elif topic_stats:
        frame_count = max(topic_stats.values())
    else:
        frame_count = 0

    preview_path = resolve_legacy_qrdf_preview_file(episode_dir)
    episode_id = str(meta.get("episode_id") or episode_dir.name)
    return {
        "episode_id": episode_id,
        "duration_sec": duration,
        "frame_count": frame_count,
        "topics": topics,
        "topic_stats": topic_stats,
        "preview_file": str(preview_path) if preview_path and preview_path.is_file() else None,
        "stats_source": source,
        "stats_complete": bool(stored_counts) or (allow_mcap and bool(topic_stats)),
    }


def _summarize_episode(episode, *, allow_mcap: bool = True) -> dict[str, Any]:
    from data.integrations.qrdf.episode_metrics import (
        episode_topic_names,
        load_metrics_json,
        pick_camera_topic_from_names,
    )

    episode_id = episode.episode_id
    meta = episode.metadata
    duration = meta.timing.duration_s if meta and meta.timing else None
    topics = episode_topic_names(episode)
    metrics = load_metrics_json(episode.path)
    stored_counts = (
        metrics.get("message_count") if isinstance(metrics.get("message_count"), dict) else None
    )
    if stored_counts:
        topic_stats = dict(stored_counts)
        source = "metrics"
    elif allow_mcap:
        topic_stats = {t: episode.mcap_reader.message_count(t) for t in topics}
        source = "mcap"
    else:
        topic_stats = {}
        source = "metadata"
    camera_topic = pick_camera_topic_from_names(topics) or (
        _pick_camera_topic(episode) if allow_mcap else None
    )
    frame_count = (
        topic_stats.get(camera_topic, 0)
        if camera_topic
        else (max(topic_stats.values()) if topic_stats else 0)
    )
    preview_path = resolve_legacy_qrdf_preview_file(episode.path)
    return {
        "episode_id": episode_id,
        "duration_sec": duration,
        "frame_count": frame_count,
        "topics": topics,
        "topic_stats": topic_stats,
        "preview_file": str(preview_path) if preview_path and preview_path.is_file() else None,
        "stats_source": source,
        "stats_complete": bool(stored_counts) or (allow_mcap and bool(topic_stats)),
    }


def _list_episode_dirs(storage_path: str | None) -> list[Path]:
    """Scan directory / manifest only, do not open MCAP."""
    layout = classify_storage_layout(storage_path)
    if layout["kind"] == "single_episode" and layout.get("episode_dir"):
        return [layout["episode_dir"]]
    if layout["kind"] != "standard" or not layout.get("root"):
        return []
    root: Path = layout["root"]
    episodes_root = root / "episodes"
    ids: list[str] = []
    manifest = _load_episode_json(root / "dataset.json")
    raw_eps = manifest.get("episodes")
    if isinstance(raw_eps, list) and raw_eps:
        for item in raw_eps:
            if isinstance(item, str) and item.strip():
                ids.append(item.strip())
            elif isinstance(item, dict) and item.get("episode_id"):
                ids.append(str(item["episode_id"]))
    elif episodes_root.is_dir():
        ids = sorted(
            p.name
            for p in episodes_root.iterdir()
            if p.is_dir() and _EPISODE_ID_PATTERN.match(p.name)
        )
    dirs: list[Path] = []
    for eid in ids:
        ep_dir = episodes_root / eid
        if ep_dir.is_dir():
            dirs.append(ep_dir)
    return dirs


def _load_episode_for_storage(storage_path: str | None, episode_id: str):
    from qrdf.reader.episode import Episode

    layout = classify_storage_layout(storage_path)
    if layout["kind"] == "single_episode":
        episode_dir = layout["episode_dir"]
        if not episode_dir:
            return None
        if episode_id and episode_dir.name != episode_id:
            return None
        return Episode(episode_dir)
    reader = get_reader(storage_path)
    if not reader:
        return None
    try:
        return reader.load_episode(episode_id)
    except Exception:
        return None


def get_reader(storage_path: str | None) -> QRDFReader | None:
    dataset_path = resolve_dataset_path(storage_path)
    if not dataset_path:
        return None
    return QRDFReader(dataset_path)


def get_dataset_summary(storage_path: str | None, *, allow_mcap: bool = True) -> dict[str, Any]:
    """Read dataset.json manifest and episode statistics (defaults to prioritizing metadata, optional fallback to MCAP)."""
    layout = classify_storage_layout(storage_path)
    if layout["kind"] == "single_episode":
        episode_dir = layout["episode_dir"]
        assert episode_dir is not None
        meta = _load_episode_json(episode_dir / "metadata.json")
        task = meta.get("task") if isinstance(meta.get("task"), dict) else {}
        episodes = list_episodes(storage_path, allow_mcap=allow_mcap)
        return {
            "dataset_path": str(episode_dir),
            "dataset_name": task.get("name") or episode_dir.name,
            "episode_count": 1,
            "qrdf_version": meta.get("qrdf_version"),
            "total_duration_sec": round(sum(e.get("duration_sec") or 0 for e in episodes), 3),
            "layout": "single_episode",
        }

    dataset_path = resolve_dataset_path(storage_path)
    if not dataset_path:
        return {}
    summary: dict[str, Any] = {"dataset_path": str(dataset_path)}
    manifest = _load_episode_json(dataset_path / "dataset.json")
    if manifest:
        ep_ids = manifest.get("episodes") if isinstance(manifest.get("episodes"), list) else []
        summary.update(
            {
                "dataset_name": manifest.get("dataset_name"),
                "domains": manifest.get("domains") or [],
                "tasks": manifest.get("tasks") or [],
                "robots": manifest.get("robots") or [],
                "episode_count": manifest.get("episode_count") or len(ep_ids),
                "qrdf_version": manifest.get("qrdf_version"),
            }
        )
    else:
        reader = QRDFReader(dataset_path)
        if reader.manifest:
            m = reader.manifest
            summary.update(
                {
                    "dataset_name": m.dataset_name,
                    "domains": m.domains,
                    "tasks": m.tasks,
                    "robots": m.robots,
                    "episode_count": m.episode_count or len(reader.list_episodes()),
                    "qrdf_version": m.qrdf_version,
                }
            )
        else:
            summary["episode_count"] = len(reader.list_episodes())
    episodes = list_episodes(storage_path, allow_mcap=allow_mcap)
    if not summary.get("episode_count"):
        summary["episode_count"] = len(episodes)
    summary["total_duration_sec"] = round(
        sum(e.get("duration_sec") or 0 for e in episodes),
        3,
    )
    return summary


def get_scene(storage_path: str | None, fallback: str | None = None) -> str | None:
    """Infer scene from SDK metadata."""
    summary = get_dataset_summary(storage_path)
    if summary.get("domains"):
        return summary["domains"][0]
    reader = get_reader(storage_path)
    if not reader:
        return fallback
    for ep_id in reader.list_episodes():
        try:
            ep = reader.load_episode(ep_id)
            if ep.metadata.scene and ep.metadata.scene.name:
                return ep.metadata.scene.name
            if ep.metadata.task and ep.metadata.task.name:
                return ep.metadata.task.name
        except Exception:
            continue
    return fallback


def validate_dataset(storage_path: str | None) -> dict[str, Any]:
    """Run QRDF Validator."""
    layout = classify_storage_layout(storage_path)
    if layout["kind"] == "incompatible":
        return {
            "level": "invalid",
            "ok": False,
            "score": 0.0,
            "warnings": [],
            "errors": [layout["error"]],
            "issues": [],
            "dataset_path": str(layout["root"]) if layout.get("root") else None,
        }
    if layout["kind"] == "single_episode":
        episodes = list_episodes(storage_path)
        return {
            "level": "warning",
            "ok": True,
            "score": 0.8,
            "warnings": ["单 episode 包，非标准 QRDF dataset 目录结构"],
            "errors": [],
            "issues": [],
            "dataset_path": str(layout["episode_dir"]),
            "episode_count": len(episodes),
        }

    dataset_path = resolve_dataset_path(storage_path)
    if not dataset_path:
        return {
            "level": "unknown",
            "ok": False,
            "score": 0.0,
            "warnings": ["未找到可校验的 QRDF 数据集路径"],
            "errors": [],
            "issues": [],
            "dataset_path": None,
        }

    report = _validator.validate_dataset(dataset_path)
    warnings = [i.message for i in report.issues if i.severity == "WARNING"]
    errors = [i.message for i in report.issues if i.severity == "ERROR"]
    level = "valid" if report.ok else ("warning" if report.error_count == 0 else "invalid")
    score = max(0.0, 1.0 - report.error_count * 0.2 - report.warning_count * 0.05)

    return {
        "level": level,
        "ok": report.ok,
        "score": round(score, 2),
        "warnings": warnings,
        "errors": errors,
        "issues": [
            {
                "severity": i.severity,
                "code": i.code,
                "message": i.message,
                "path": i.path,
                "topic": i.topic,
            }
            for i in report.issues
        ],
        "dataset_path": str(dataset_path),
    }


def list_episodes(storage_path: str | None, *, allow_mcap: bool = True) -> list[dict[str, Any]]:
    """List episodes: prioritize metadata.json / metrics.json, fallback to MCAP when metrics are missing."""
    episode_dirs = _list_episode_dirs(storage_path)
    if episode_dirs:
        episodes: list[dict[str, Any]] = []
        for ep_dir in episode_dirs:
            try:
                episodes.append(_summarize_episode_dir(ep_dir, allow_mcap=allow_mcap))
            except Exception:
                episodes.append(
                    {"episode_id": ep_dir.name, "stats_complete": False, "stats_source": "unknown"}
                )
        return episodes

    # Legacy fallback: call SDK when directory scanning fails
    layout = classify_storage_layout(storage_path)
    if layout["kind"] == "single_episode":
        episode = _load_episode_for_storage(storage_path, "")
        if not episode:
            return []
        try:
            return [_summarize_episode(episode, allow_mcap=allow_mcap)]
        except Exception:
            episode_dir = layout["episode_dir"]
            return [
                {
                    "episode_id": episode_dir.name if episode_dir else "episode_000001",
                    "stats_complete": False,
                }
            ]

    reader = get_reader(storage_path)
    if not reader:
        return []

    episodes = []
    for episode_id in reader.list_episodes():
        try:
            episode = reader.load_episode(episode_id)
            episodes.append(_summarize_episode(episode, allow_mcap=allow_mcap))
        except Exception:
            episodes.append({"episode_id": episode_id, "stats_complete": False})
    return episodes


def storage_path_abs(storage_path: str | None) -> str | None:
    """Resolve display path corresponding to storage_path (prioritizes oss:// for cloud, otherwise local absolute path)."""
    from data.utils.storage_uri import is_cloud_uri, to_display_storage_uri

    if storage_path and is_cloud_uri(storage_path):
        return storage_path
    display = to_display_storage_uri(storage_path)
    if display and is_cloud_uri(display):
        return display
    path = resolve_dataset_path(storage_path) or resolve_storage_path(storage_path)
    if not path:
        return display or None
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def list_dataset_files(storage_path: str | None, *, max_files: int = 300) -> list[dict[str, Any]]:
    """Return public metadata of QRDF files without exposing server filesystem paths."""

    canonical = storage_path
    dataset_path = resolve_dataset_path(storage_path)
    root_path = dataset_path or resolve_storage_path(storage_path)
    if not root_path or not root_path.exists():
        return []

    base = dataset_path or root_path
    files: list[dict[str, Any]] = []
    try:
        for item in sorted(base.rglob("*")):
            if not item.is_file():
                continue
            rel = item.relative_to(base).as_posix()
            public_item = {
                "relative_path": rel,
                "size_bytes": item.stat().st_size,
            }
            if canonical and canonical.startswith(("oss://", "nas://")):
                public_item["path_display"] = f"{canonical.rstrip('/')}/{rel.lstrip('/')}"
            files.append(public_item)
            if len(files) >= max_files:
                files.append(
                    {
                        "relative_path": f"...（仅展示前 {max_files} 个文件）",
                        "size_bytes": 0,
                        "truncated": True,
                    }
                )
                break
    except Exception:
        return files
    return files


def list_topic_stats(storage_path: str | None, *, allow_mcap: bool = True) -> list[dict[str, Any]]:
    """Summarize message count per topic: prioritize metrics.json / metadata.json, fallback to MCAP when metrics are missing."""
    from data.integrations.qrdf.episode_metrics import (
        load_metrics_json,
        topic_names_from_episode_dir,
    )

    stats: dict[str, int] = {}
    for ep_dir in _list_episode_dirs(storage_path):
        metrics = load_metrics_json(ep_dir)
        stored = (
            metrics.get("message_count") if isinstance(metrics.get("message_count"), dict) else None
        )
        if stored:
            for topic, count in stored.items():
                try:
                    stats[str(topic)] = stats.get(str(topic), 0) + int(count)
                except (TypeError, ValueError):
                    continue
            continue
        for topic in topic_names_from_episode_dir(ep_dir):
            stats.setdefault(topic, 0)
        if allow_mcap:
            try:
                from qrdf.reader.episode import Episode

                episode = Episode(ep_dir)
                for topic in episode.list_topics():
                    stats[topic] = stats.get(topic, 0) + episode.mcap_reader.message_count(topic)
            except Exception:
                continue

    if stats or not allow_mcap:
        return [{"name": name, "frame_count": count} for name, count in sorted(stats.items())]

    # Fallback to legacy path when directory-level summarization fails
    layout = classify_storage_layout(storage_path)
    if layout["kind"] == "single_episode":
        episode = _load_episode_for_storage(storage_path, "")
        if not episode:
            return []
        mcap_stats = {t: episode.mcap_reader.message_count(t) for t in episode.list_topics()}
        return [{"name": name, "frame_count": count} for name, count in sorted(mcap_stats.items())]

    reader = get_reader(storage_path)
    if not reader:
        return []

    for episode_id in reader.list_episodes():
        try:
            episode = reader.load_episode(episode_id)
            for topic in episode.list_topics():
                stats[topic] = stats.get(topic, 0) + episode.mcap_reader.message_count(topic)
        except Exception:
            continue

    return [{"name": name, "frame_count": count} for name, count in sorted(stats.items())]


def _preview_cache_dir(task_id: int | None) -> Path | None:
    if task_id is None:
        return None
    return Path(settings.storage_root) / "previews" / f"task{task_id}"


def _preview_from_episode_dir(
    episode_dir: Path,
    *,
    generate: bool,
    task_id: int | None = None,
) -> Path | None:
    from qrdf.reader.episode import Episode

    try:
        episode = Episode(episode_dir)
    except Exception:
        return None
    existing = resolve_legacy_qrdf_preview_file(episode.path)
    if existing.is_file() and mp4_is_browser_ready(existing):
        return existing
    if generate:
        try:
            return export_episode_preview(episode.path, existing)
        except Exception:
            cache_dir = _preview_cache_dir(task_id)
            if cache_dir is None:
                cache_key = hashlib.md5(
                    str(episode_dir).encode(), usedforsecurity=False
                ).hexdigest()[:12]
                cache_dir = Path(settings.storage_root) / "previews" / cache_key
            cache_dir.mkdir(parents=True, exist_ok=True)
            out = cache_dir / f"{episode.episode_id}_preview.mp4"
            if not out.is_file() or not mp4_is_browser_ready(out):
                export_episode_preview(episode.path, out)
            if out.is_file():
                return out
    elif existing.is_file():
        return existing
    return None


def resolve_preview_file(
    storage_path: str | None,
    *,
    generate: bool = True,
    task_id: int | None = None,
) -> Path | None:
    """Resolve or generate preview MP4 (browser-playable faststart version)."""
    layout = classify_storage_layout(storage_path)
    if layout["kind"] == "single_episode" and layout["episode_dir"]:
        return _preview_from_episode_dir(layout["episode_dir"], generate=generate, task_id=task_id)

    reader = get_reader(storage_path)
    if not reader:
        file_path = resolve_storage_path(storage_path)
        if file_path and file_path.is_file() and file_path.suffix.lower() in {".mp4", ".webm"}:
            return file_path
        return None

    for episode_id in reader.list_episodes():
        try:
            episode = reader.load_episode(episode_id)
            existing = resolve_legacy_qrdf_preview_file(episode.path)
            if existing.is_file() and mp4_is_browser_ready(existing):
                return existing
            if generate:
                try:
                    return export_episode_preview(episode.path, existing)
                except Exception:
                    cache_dir = _preview_cache_dir(task_id)
                    if cache_dir is None:
                        cache_key = hashlib.md5(
                            str(resolve_dataset_path(storage_path)).encode(), usedforsecurity=False
                        ).hexdigest()[:12]
                        cache_dir = Path(settings.storage_root) / "previews" / cache_key
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    out = cache_dir / f"{episode_id}_preview.mp4"
                    if not out.is_file() or not mp4_is_browser_ready(out):
                        export_episode_preview(episode.path, out)
                    if out.is_file():
                        return out
            elif existing.is_file():
                return existing
        except Exception:
            continue
    return None


PREVIEWABLE_SUFFIXES = {".mp4", ".webm", ".jpg", ".jpeg", ".png", ".gif"}

_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
}


def get_preview_status(storage_path: str | None, *, generate: bool = True) -> dict[str, Any]:
    """Check preview availability and return explicit data format errors (rather than auth-related notices)."""
    if not storage_path:
        return {"available": False, "error": "暂无存储路径，无法预览"}

    file_path = resolve_storage_path(storage_path)
    if file_path and file_path.is_file():
        suffix = file_path.suffix.lower()
        if suffix == ".zip":
            try:
                if not zipfile.is_zipfile(file_path):
                    return {"available": False, "error": "文件不是有效的 ZIP 压缩包，无法预览"}
            except Exception:
                return {"available": False, "error": "ZIP 文件损坏或格式无效，无法预览"}
        if suffix in PREVIEWABLE_SUFFIXES and file_path.is_file():
            return {
                "available": True,
                "path": str(file_path),
                "media_type": _MEDIA_TYPES.get(suffix, "application/octet-stream"),
            }

    dataset_path = resolve_dataset_path(storage_path)
    layout = classify_storage_layout(storage_path)
    if layout["kind"] == "incompatible":
        return {"available": False, "error": layout["error"]}
    if not dataset_path and layout["kind"] != "single_episode":
        if file_path and file_path.is_file():
            suffix = file_path.suffix.lower()
            if suffix in {".mcap", ".json"}:
                return {
                    "available": False,
                    "error": f"原始数据（{suffix}）需先完成预处理入库后才能预览",
                }
            if suffix == ".zip":
                return {"available": False, "error": INCOMPATIBLE_QRDF_FORMAT}
            return {"available": False, "error": "数据格式无法解析为 QRDF 数据集，无法预览"}
        return {"available": False, "error": "存储路径无效或数据文件不存在，无法预览"}

    preview_path = resolve_preview_file(storage_path, generate=generate)
    if not preview_path or not preview_path.is_file():
        return {
            "available": False,
            "error": "QRDF 数据中未找到可预览视频（preview.mp4），请检查数据格式是否完整",
        }

    suffix = preview_path.suffix.lower()
    if suffix not in PREVIEWABLE_SUFFIXES:
        return {
            "available": False,
            "error": f"预览文件格式不支持（{suffix}），仅支持 mp4/webm/图片",
        }

    return {
        "available": True,
        "path": str(preview_path),
        "media_type": _MEDIA_TYPES.get(suffix, "application/octet-stream"),
    }


def export_lerobot(
    storage_paths: list[str],
    output_dir: Path,
    *,
    fps: float | None = None,
    export_template: str = "generic",
    lerobot_version: str = "v3.0",
    on_progress: Callable[[int], None] | None = None,
    split_assignments: list[str] | None = None,
) -> Path:
    """QRDF to LeRobot conversion and zip packaging (defaults to dataset native fps, no frame dropping).

    Legacy callers omit ``split_assignments`` and retain the flat
    ``dataset_NNN`` archive layout. When supplied, converted data is placed
    under its declared train/val/test directory rather than only recording a
    split in metadata external to the converted records.
    """
    if split_assignments is not None:
        if len(split_assignments) != len(storage_paths):
            raise ValueError("split assignments must match QRDF storage paths")
        if any(split not in {"train", "val", "test"} for split in split_assignments):
            raise ValueError("split assignments must be train, val, or test")
        resolved_paths = [resolve_dataset_path(storage_path) for storage_path in storage_paths]
        if any(dataset_path is None for dataset_path in resolved_paths):
            raise ValueError("revision export contains an unresolvable QRDF storage path")
    else:
        resolved_paths = None
    output_dir.mkdir(parents=True, exist_ok=True)
    work_root = output_dir / "lerobot_work"
    if work_root.exists():
        shutil.rmtree(work_root)
    work_root.mkdir(parents=True, exist_ok=True)

    converted = 0
    total = len(storage_paths) or 1
    for idx, storage_path in enumerate(storage_paths):
        dataset_path = (
            resolved_paths[idx]
            if resolved_paths is not None
            else resolve_dataset_path(storage_path)
        )
        if not dataset_path:
            continue
        target_root = work_root if split_assignments is None else work_root / split_assignments[idx]
        target = target_root / f"dataset_{idx:03d}"
        dataset_fps = fps if fps is not None and fps > 0 else infer_dataset_fps(dataset_path)
        convert_dataset_extended(
            dataset_path,
            target,
            fps=dataset_fps,
            lerobot_version=lerobot_version,
            export_template=export_template,
        )
        converted += 1
        if on_progress:
            on_progress(20 + int(55 * converted / total))

    if converted == 0:
        raise ValueError("没有可导出的 QRDF 数据集")

    if on_progress:
        on_progress(85)

    zip_path = output_dir / "lerobot_export.zip"
    if zip_path.exists():
        zip_path.unlink()
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", work_root)

    if on_progress:
        on_progress(95)

    return zip_path


def compute_dataset_stats(qrdf_storage_paths: list[str]) -> dict[str, Any]:
    """Aggregate statistics across multiple QRDF assets (for dataset construction)."""
    total_episodes = 0
    total_duration = 0.0
    valid_count = 0
    for path in qrdf_storage_paths:
        eps = list_episodes(path)
        total_episodes += len(eps)
        total_duration += sum(e.get("duration_sec") or 0 for e in eps)
        q = validate_dataset(path)
        if q.get("level") == "valid":
            valid_count += 1
    n = len(qrdf_storage_paths) or 1
    return {
        "episode_count": total_episodes,
        "total_duration_sec": round(total_duration, 3),
        "quality_valid_ratio": round(valid_count / n, 2) if qrdf_storage_paths else 0,
    }


def compute_dataset_stats_from_records(qrdf_records: list[Any]) -> dict[str, Any]:
    """Quickly summarize statistics based on ingested QRDF records (avoids blocking entire service during dataset creation)."""
    total_episodes = 0
    total_duration = 0.0
    valid_count = 0
    for q in qrdf_records:
        meta = getattr(q, "metadata_json", None) or {}
        episodes = meta.get("episodes")
        if isinstance(episodes, list) and episodes:
            total_episodes += len(episodes)
            total_duration += sum(
                float(e.get("duration_sec") or 0) for e in episodes if isinstance(e, dict)
            )
        else:
            summary = meta.get("sdk_summary") or meta.get("summary") or {}
            total_episodes += int(meta.get("episode_count") or summary.get("episode_count") or 1)
            total_duration += float(
                meta.get("total_duration_sec") or summary.get("total_duration_sec") or 0
            )
        if (getattr(q, "quality_level", None) or "").lower() == "valid":
            valid_count += 1
    n = len(qrdf_records) or 1
    return {
        "episode_count": total_episodes,
        "total_duration_sec": round(total_duration, 3),
        "quality_valid_ratio": round(valid_count / n, 2) if qrdf_records else 0,
        "stats_source": "qrdf_records",
    }
