"""Episode object manifest stored on admission facts.

One list describes every storage object that makes up an admitted episode:
the raw data file plus the process objects produced by admission.  Each entry's
``path`` is its location inside a standard QRDF episode directory, so any
consumer can rebuild the episode by downloading every entry to its path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from data.infra.object_storage import StorageObjectRef

OBJECT_KINDS = frozenset(
    {
        "data",
        "metadata",
        "admission_report",
        "preview_manifest",
        "preview_video",
        "preview_timeline",
    }
)
_PREVIEW_MEDIA_KINDS = frozenset({"preview_video", "preview_timeline"})
_SINGLETON_KINDS = ("data", "metadata", "admission_report")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class EpisodeObjectsError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EpisodeObject:
    path: str
    role: str
    kind: str
    ref: dict[str, Any]
    topic: str | None = None

    def storage_ref(self) -> StorageObjectRef:
        return StorageObjectRef(
            self.ref["bucket_role"],
            self.ref["object_key"],
            self.ref.get("version_id"),
            self.ref["etag"],
            self.ref["size_bytes"],
            self.ref["sha256"],
        )

    def as_entry(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "path": self.path,
            "role": self.role,
            "kind": self.kind,
            "ref": dict(self.ref),
        }
        if self.topic is not None:
            entry["topic"] = self.topic
        return entry


def object_entry(
    *, path: str, kind: str, ref: dict[str, Any], topic: str | None = None
) -> dict[str, Any]:
    """Build one validated manifest entry; ``role`` always mirrors the bucket."""
    entry: dict[str, Any] = {
        "path": path,
        "role": str(ref.get("bucket_role") or ""),
        "kind": kind,
        "ref": {
            "bucket_role": ref.get("bucket_role"),
            "object_key": ref.get("object_key"),
            "version_id": ref.get("version_id"),
            "etag": ref.get("etag"),
            "size_bytes": ref.get("size_bytes"),
            "sha256": ref.get("sha256"),
        },
    }
    if topic is not None:
        entry["topic"] = topic
    return parse_objects([entry])[0].as_entry()


def safe_episode_path(value: Any) -> str:
    """Validate a path relative to an episode's directory; fail closed.

    Rejects anything that is not a plain relative POSIX path: non-strings,
    empty strings, backslashes, NUL bytes, absolute paths, paths with no
    parts (e.g. ``"."``, ``"./"``), and any ``.``/``..``/empty segment.
    """
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise EpisodeObjectsError("episode_object_path_unsafe")
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise EpisodeObjectsError("episode_object_path_unsafe")
    return path.as_posix()


def _valid_ref(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise EpisodeObjectsError("episode_object_ref_invalid")
    role = raw.get("bucket_role")
    key = raw.get("object_key")
    etag = raw.get("etag")
    version = raw.get("version_id")
    size = raw.get("size_bytes")
    sha = raw.get("sha256")
    if (
        role not in {"raw", "process"}
        or not isinstance(key, str)
        or not key
        or key.startswith("/")
        or not isinstance(etag, str)
        or not etag
        or (version is not None and (not isinstance(version, str) or not version))
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or not isinstance(sha, str)
        or _SHA256_RE.fullmatch(sha) is None
    ):
        raise EpisodeObjectsError("episode_object_ref_invalid")
    return {
        "bucket_role": role,
        "object_key": key,
        "version_id": version,
        "etag": etag,
        "size_bytes": size,
        "sha256": sha,
    }


def parse_objects(entries: Any) -> list[EpisodeObject]:
    """Validate a stored manifest; fail closed on anything unexpected."""
    if not isinstance(entries, list):
        raise EpisodeObjectsError("episode_objects_invalid")
    objects: list[EpisodeObject] = []
    seen: set[str] = set()
    for raw in entries:
        if not isinstance(raw, dict):
            raise EpisodeObjectsError("episode_objects_invalid")
        path = safe_episode_path(raw.get("path"))
        if path in seen:
            raise EpisodeObjectsError("episode_object_path_duplicate")
        seen.add(path)
        kind = raw.get("kind")
        if kind not in OBJECT_KINDS:
            raise EpisodeObjectsError("episode_object_kind_invalid")
        ref = _valid_ref(raw.get("ref"))
        if raw.get("role") != ref["bucket_role"]:
            raise EpisodeObjectsError("episode_object_role_mismatch")
        topic = raw.get("topic")
        if kind in _PREVIEW_MEDIA_KINDS:
            if not isinstance(topic, str) or not topic:
                raise EpisodeObjectsError("episode_object_topic_missing")
        elif topic is not None:
            raise EpisodeObjectsError("episode_object_topic_unexpected")
        objects.append(EpisodeObject(path, ref["bucket_role"], kind, ref, topic))
    return objects


def object_of_kind(objects: list[EpisodeObject], kind: str) -> EpisodeObject:
    matches = [item for item in objects if item.kind == kind]
    if len(matches) != 1:
        raise EpisodeObjectsError(f"episode_object_missing_{kind}")
    return matches[0]


def data_object(objects: list[EpisodeObject]) -> EpisodeObject:
    return object_of_kind(objects, "data")


def preview_streams(objects: list[EpisodeObject]) -> list[dict[str, Any]]:
    by_topic: dict[str, dict[str, Any]] = {}
    for item in objects:
        if item.kind not in _PREVIEW_MEDIA_KINDS:
            continue
        slot = "video" if item.kind == "preview_video" else "timeline"
        stream = by_topic.setdefault(item.topic, {"topic": item.topic})
        if slot in stream:
            raise EpisodeObjectsError("episode_object_preview_stream_duplicate")
        stream[slot] = item
    streams = [by_topic[topic] for topic in sorted(by_topic)]
    if any("video" not in stream or "timeline" not in stream for stream in streams):
        raise EpisodeObjectsError("episode_object_preview_stream_incomplete")
    return streams


_KIND_STANDARD_PATH = {
    "metadata": "metadata.json",
    "admission_report": "admission-report.json",
    "preview_manifest": "media/preview/manifest.json",
}
_PREVIEW_PATH_PREFIX = "media/preview/"


def require_verified_objects(objects: list[EpisodeObject]) -> None:
    """A verified manifest can rebuild a complete, playable QRDF episode.

    Every kind is bound to its standard episode path and storage role: the
    ``data`` object must be ``raw``; every other kind must be ``process`` and
    live at its fixed path (``metadata``, ``admission_report``,
    ``preview_manifest``) or under ``media/preview/`` (``preview_video``,
    ``preview_timeline``).
    """
    for kind in _SINGLETON_KINDS:
        object_of_kind(objects, kind)
    if data_object(objects).role != "raw":
        raise EpisodeObjectsError("episode_object_role_mismatch")
    for item in objects:
        if item.kind == "data":
            continue
        if item.role != "process":
            raise EpisodeObjectsError("episode_object_role_mismatch")
        standard_path = _KIND_STANDARD_PATH.get(item.kind)
        if standard_path is not None and item.path != standard_path:
            raise EpisodeObjectsError("episode_object_path_kind_mismatch")
        if item.kind in _PREVIEW_MEDIA_KINDS and not item.path.startswith(_PREVIEW_PATH_PREFIX):
            raise EpisodeObjectsError("episode_object_path_kind_mismatch")
    streams = preview_streams(objects)
    manifests = [item for item in objects if item.kind == "preview_manifest"]
    if streams and len(manifests) != 1:
        raise EpisodeObjectsError("episode_object_missing_preview_manifest")


def fact_objects(fact: Any) -> list[EpisodeObject]:
    return parse_objects(getattr(fact, "objects_json", None) or [])
