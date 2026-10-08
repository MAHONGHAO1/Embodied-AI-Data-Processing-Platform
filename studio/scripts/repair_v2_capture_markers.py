#!/usr/bin/env python3
"""Repair legacy QuicEgo V2 completion markers in OSS.

This is an operational script, intentionally kept outside the application
package.  It is safe by default: without ``--apply`` it only reports what it
would repair.  It never creates a missing ``complete.json`` and only replaces
an existing marker after the data and metadata objects have been re-checked.

Run it inside the UAT API container so the normal secret injection and runtime
OSS configuration are used, for example::

    /app/deploy/run-with-secrets.sh python /tmp/repair_v2_capture_markers.py \
        --prefix prod/raw/v2/sources --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _bootstrap_backend_path() -> None:
    """Allow the same file to run from the repo or from ``/tmp`` in Docker."""
    candidates = [
        os.environ.get("QUICDATA_BACKEND_ROOT", ""),
        "/app/backend",
        str(Path(__file__).resolve().parents[1] / "backend"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_dir() and candidate not in sys.path:
            sys.path.insert(0, candidate)


_bootstrap_backend_path()

# The QRDF SDK is vendored and is normally added to sys.path by the API
# bootstrap.  This script imports the same service modules directly, so run
# that small bootstrap explicitly when invoked outside ``data.main``.
from data.bootstrap import _ensure_vendor_path  # noqa: E402

_ensure_vendor_path()

from qrdf.exceptions import IncompatibleVersionError  # noqa: E402
from qrdf.models.episode import EpisodeMetadata  # noqa: E402

from data.database import ExternalOssImportScope, SessionLocal  # noqa: E402
from data.infra import oss_client  # noqa: E402
from data.integrations.qrdf.paths import validate_qrdf_external_episode_id  # noqa: E402
from data.services.capture_batch_import import (  # noqa: E402
    CAPTURE_COMPLETE_SCHEMA,
    CaptureSourcePath,
    _capture_source_path,
    _core_capture_metadata_payload,
    _marker_matches_source,
    _normalized_etag,
)

_LEGACY_FORMAT_FIELDS = frozenset({"format", "objects", "completed_at"})
_LEGACY_LIFECYCLE_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "device_id",
        "episode_id",
        "objects",
        "file_digest",
        "file_size",
        "recording_status",
        "integrity_status",
        "integrity_issues",
        "completed_at",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_OBJECT_NAMES = ["data.mcap", "metadata.json"]


@dataclass(frozen=True)
class RepairCandidate:
    path: CaptureSourcePath
    complete_key: str
    marker: dict[str, Any]
    data_info: oss_client.OSSObjectInfo
    metadata_info: oss_client.OSSObjectInfo
    complete_info: oss_client.OSSObjectInfo
    replacement: dict[str, Any]
    legacy_kind: str


def _normalize_prefix(value: str, bucket: str) -> str:
    if not isinstance(value, str):
        raise ValueError("prefix must be a string")
    prefix = value.strip().strip("/")
    if not prefix or "\\" in prefix or "\x00" in prefix:
        raise ValueError("prefix must be a non-empty OSS object prefix")
    parts = prefix.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ValueError("prefix contains an unsafe path component")
    if not any(
        tuple(parts[index : index + 3]) == ("raw", "v2", "sources") for index in range(len(parts))
    ):
        raise ValueError("prefix must contain raw/v2/sources")
    # Use the application's provider-path validation before issuing a listing.
    if not oss_client._provider_path_is_safe(bucket, f"{prefix}/.repair-listing"):
        raise ValueError("prefix is not allowed by the storage path policy")
    return prefix + "/"


def _episode_filter(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        validate_qrdf_external_episode_id(value)
    except ValueError as exc:
        raise ValueError("episode-id is not a valid QRDF external episode ID") from exc
    return value


def _require_enabled_scope(bucket: str, prefix: str) -> None:
    """Refuse writes outside an enabled QuicData external OSS scope."""
    requested = prefix.rstrip("/")
    db = SessionLocal()
    try:
        scopes = (
            db.query(ExternalOssImportScope)
            .filter(
                ExternalOssImportScope.bucket == bucket,
                ExternalOssImportScope.is_enabled.is_(True),
            )
            .all()
        )
    finally:
        db.close()
    for scope in scopes:
        prefixes = scope.prefixes_json if isinstance(scope.prefixes_json, list) else []
        for value in prefixes:
            if not isinstance(value, str):
                continue
            allowed = value.strip().strip("/")
            if allowed and (requested == allowed or requested.startswith(allowed + "/")):
                return
    raise ValueError(
        f"prefix is not covered by an enabled external OSS scope: oss://{bucket}/{requested}"
    )


def _safe_completed_at(marker: dict[str, Any]) -> str:
    value = marker.get("completed_at")
    if isinstance(value, str):
        value = value.strip()
        if (
            0 < len(value) <= 128
            and value.isascii()
            and all(character.isprintable() for character in value)
        ):
            return value
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _is_object_names(value: object) -> bool:
    return isinstance(value, list) and value == _OBJECT_NAMES


def _data_digest(info: oss_client.OSSObjectInfo) -> str | None:
    value = info.metadata.get("x-oss-meta-sha256")
    if isinstance(value, str) and _SHA256_RE.fullmatch(value.strip()):
        return value.strip().lower()
    return None


def _legacy_kind(
    marker: object,
    *,
    path: CaptureSourcePath,
    data_info: oss_client.OSSObjectInfo,
) -> str | None:
    if not isinstance(marker, dict):
        return None

    # V2 marker written by the first taskless uploader (7d678e3).  It has no
    # digest by design; the exact object HEADs and QRDF metadata are checked
    # separately before this shape is repaired.
    if (
        marker.get("format") == CAPTURE_COMPLETE_SCHEMA
        and set(marker).issubset(_LEGACY_FORMAT_FIELDS)
        and _is_object_names(marker.get("objects"))
    ):
        return "format-v2"

    # V2 marker written during the transition from the old lifecycle marker
    # (9abbdf0).  Do not accept a marker which says the recording was not
    # complete, or whose data identity cannot be tied to the current object.
    if (
        marker.get("schema_version") == 1
        and set(marker).issubset(_LEGACY_LIFECYCLE_FIELDS)
        and _is_object_names(marker.get("objects"))
        and marker.get("episode_id") in {None, path.episode_id}
        and marker.get("file_size") == data_info.size
        and marker.get("recording_status") in {None, "complete"}
        and marker.get("integrity_status") not in {"failed", "invalid"}
    ):
        digest = marker.get("file_digest")
        object_digest = _data_digest(data_info)
        if not isinstance(digest, str) or not digest.strip():
            return None
        if not _SHA256_RE.fullmatch(digest.strip()):
            return None
        if object_digest is None or digest.strip().lower() != object_digest:
            return None
        issues = marker.get("integrity_issues")
        if issues is not None:
            if not isinstance(issues, list):
                return None
            if any(not isinstance(item, str) or item.strip() for item in issues):
                return None
        return "lifecycle-v2"
    return None


def _validate_metadata(metadata_payload: object, path: CaptureSourcePath) -> None:
    try:
        metadata = EpisodeMetadata.model_validate(_core_capture_metadata_payload(metadata_payload))
        metadata.check_version_compatible()
    except IncompatibleVersionError as exc:
        raise ValueError("metadata QRDF version is incompatible") from exc
    except (TypeError, ValueError) as exc:
        raise ValueError("metadata JSON does not match the QRDF core schema") from exc
    if metadata.episode_id != path.episode_id:
        raise ValueError("metadata episode_id does not match the V2 directory")
    if metadata.data_file != "data.mcap":
        raise ValueError("metadata data_file is not data.mcap")


def _canonical_marker(
    path: CaptureSourcePath,
    *,
    marker: dict[str, Any],
    data_info: oss_client.OSSObjectInfo,
    metadata_info: oss_client.OSSObjectInfo,
) -> dict[str, Any]:
    return {
        "schema": CAPTURE_COMPLETE_SCHEMA,
        "schema_version": 1,
        "episode_id": path.episode_id,
        "ingest_mode": "trusted_offline",
        "objects": {
            "data.mcap": {
                "size": int(data_info.size),
                "etag": _normalized_etag(data_info.etag),
            },
            "metadata.json": {
                "size": int(metadata_info.size),
                "etag": _normalized_etag(metadata_info.etag),
            },
        },
        "completed_at": _safe_completed_at(marker),
    }


def _inspect_complete(bucket: str, complete_key: str) -> RepairCandidate | tuple[str, str] | None:
    path = _capture_source_path(complete_key, require_known_file=True)
    if path is None or path.file_name != "complete.json":
        return None
    try:
        validate_qrdf_external_episode_id(path.episode_id)
    except ValueError:
        return "skip", "episode ID is not a valid QRDF external ID"
    data_key = f"{path.parent_key}/data.mcap"
    metadata_key = f"{path.parent_key}/metadata.json"
    try:
        data_info = oss_client.object_info(bucket, data_key)
        metadata_info = oss_client.object_info(bucket, metadata_key)
        complete_info = oss_client.object_info(bucket, complete_key)
        if not data_info or not metadata_info or not complete_info:
            return "skip", "one or more package objects are missing"
        if data_info.size <= 0 or metadata_info.size <= 0 or complete_info.size <= 0:
            return "skip", "one or more package objects are empty"
        if not data_info.etag or not metadata_info.etag or not complete_info.etag:
            return "skip", "OSS HEAD did not provide all required ETags"
        marker = oss_client.read_json_object(bucket, complete_key, if_match=complete_info.etag)
        metadata_payload = oss_client.read_json_object(
            bucket, metadata_key, if_match=metadata_info.etag
        )
        if _marker_matches_source(
            marker,
            path=path,
            data_info=data_info,
            metadata_info=metadata_info,
        ):
            return "valid", "already matches the current V2 marker contract"
        _validate_metadata(metadata_payload, path)
        legacy_kind = _legacy_kind(marker, path=path, data_info=data_info)
        if legacy_kind is None:
            return "skip", "marker is invalid but not a recognized legacy V2 shape"
        replacement = _canonical_marker(
            path,
            marker=marker,
            data_info=data_info,
            metadata_info=metadata_info,
        )
        return RepairCandidate(
            path=path,
            complete_key=complete_key,
            marker=marker,
            data_info=data_info,
            metadata_info=metadata_info,
            complete_info=complete_info,
            replacement=replacement,
            legacy_kind=legacy_kind,
        )
    except (OSError, FileNotFoundError, ValueError, TypeError, KeyError) as exc:
        return "skip", str(exc)


def _iter_complete_keys(bucket: str, prefix: str, *, page_size: int, max_complete: int):
    token: str | None = None
    seen_tokens: set[str | None] = set()
    yielded = 0
    while True:
        if token in seen_tokens:
            raise RuntimeError("OSS listing cursor did not advance")
        seen_tokens.add(token)
        page = oss_client.list_prefix_page(
            bucket,
            prefix,
            continuation_token=token,
            max_keys=page_size,
        )
        for item in page.objects:
            key = item.get("key") if isinstance(item, dict) else None
            if not isinstance(key, str) or not key.endswith("/complete.json"):
                continue
            path = _capture_source_path(key, require_known_file=True)
            if path is None or path.file_name != "complete.json":
                continue
            yielded += 1
            if yielded > max_complete:
                raise RuntimeError(
                    f"more than {max_complete} complete.json objects matched; "
                    "narrow --prefix or raise --max-complete explicitly"
                )
            yield key
        token = page.next_token
        if token is None:
            return


def _write_marker(candidate: RepairCandidate, bucket: str) -> None:
    # Re-read the marker identity immediately before replacement.  If another
    # uploader changed it after the scan, do not overwrite that newer object.
    current = oss_client.object_info(bucket, candidate.complete_key)
    if current is None or _normalized_etag(current.etag) != _normalized_etag(
        candidate.complete_info.etag
    ):
        raise RuntimeError("complete.json changed after inspection; retry the scan")
    encoded = json.dumps(
        candidate.replacement,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > 64 * 1024:
        raise ValueError("replacement marker exceeds the JSON object limit")
    data_digest = _data_digest(candidate.data_info)
    metadata = {"x-oss-meta-sha256": data_digest} if data_digest else None
    fd, name = tempfile.mkstemp(prefix="quicstudio-v2-marker-", suffix=".json")
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        oss_client.upload_file(
            Path(name),
            bucket,
            candidate.complete_key,
            content_type="application/json",
            forbid_overwrite=False,
            metadata=metadata,
        )
        updated = oss_client.object_info(bucket, candidate.complete_key)
        if updated is None:
            raise RuntimeError("complete.json disappeared after repair")
        body = oss_client.read_json_object(bucket, candidate.complete_key, if_match=updated.etag)
        if not _marker_matches_source(
            body,
            path=candidate.path,
            data_info=candidate.data_info,
            metadata_info=candidate.metadata_info,
        ):
            raise RuntimeError("post-write marker verification failed")
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bucket",
        default=None,
        help="OSS bucket (default: configured raw bucket)",
    )
    parser.add_argument(
        "--prefix",
        required=True,
        help="authorized object prefix containing raw/v2/sources (for example prod/raw/v2/sources)",
    )
    parser.add_argument("--episode-id", help="repair one exact episode only")
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--max-complete", type=int, default=1000)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="replace recognized legacy markers; omitted means dry-run",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        bucket = args.bucket or oss_client.bucket_name("raw")
        prefix = _normalize_prefix(args.prefix, bucket)
        _require_enabled_scope(bucket, prefix)
        episode_filter = _episode_filter(args.episode_id)
        if not 1 <= args.page_size <= 1000:
            raise ValueError("page-size must be between 1 and 1000")
        if not 1 <= args.max_complete <= 100_000:
            raise ValueError("max-complete must be between 1 and 100000")
    except ValueError as exc:
        parser.error(str(exc))

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"mode={mode} bucket={bucket} prefix={prefix}")
    counts = {"valid": 0, "repairable": 0, "repaired": 0, "skipped": 0, "failed": 0}
    try:
        for complete_key in _iter_complete_keys(
            bucket,
            prefix,
            page_size=args.page_size,
            max_complete=args.max_complete,
        ):
            if episode_filter is not None:
                listed_path = _capture_source_path(complete_key, require_known_file=True)
                if listed_path is None or listed_path.episode_id != episode_filter:
                    continue
            inspected = _inspect_complete(bucket, complete_key)
            if inspected is None:
                continue
            if isinstance(inspected, tuple):
                status, reason = inspected
                if status == "valid":
                    counts["valid"] += 1
                else:
                    counts["skipped"] += 1
                    print(f"SKIP {complete_key}: {reason}")
                continue
            counts["repairable"] += 1
            print(
                f"REPAIR {inspected.legacy_kind} {complete_key} episode={inspected.path.episode_id}"
            )
            if not args.apply:
                continue
            try:
                _write_marker(inspected, bucket)
            except Exception as exc:  # report one object, continue bounded batch
                counts["failed"] += 1
                print(f"FAIL {complete_key}: {exc}")
                continue
            counts["repaired"] += 1
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    print("summary " + " ".join(f"{name}={value}" for name, value in counts.items()))
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
