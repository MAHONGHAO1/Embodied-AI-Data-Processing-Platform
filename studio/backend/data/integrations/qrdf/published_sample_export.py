"""QuicData adapter for exporting immutable PublishedSample manifests.

The adapter is the boundary between QuicData authorization and the vendored
QRDF exporter.  It accepts only a server-created revision manifest, materializes
the manifest's official artifacts, and emits a path-free archive sidecar.
"""

from __future__ import annotations

import hmac
import json
import re
import zipfile
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from qrdf.converters.published_sample_lerobot import (
    SliceExportOptions,
    SliceExportReport,
    SliceExportRequest,
    export_episode_slices_to_lerobot_v3,
)
from qrdf.reader.reader import QRDFReader

from data.integrations.qrdf.service import ensure_processable_dataset_path
from data.services.cloud_storage import materialize_for_processing
from data.services.published_sample_manifest import build_export_sidecar
from data.utils.checksums import legacy_batch_tree_sha256, tree_sha256

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SPLITS = frozenset({"train", "val", "test"})
_ITEM_KINDS = frozenset({"episode", "sample"})


@dataclass(frozen=True)
class PublishedSampleExportResult:
    """Files produced by one deterministic revision export."""

    archive_path: Path
    report: SliceExportReport
    sidecar: dict[str, Any]


def export_published_sample_revision(
    manifest: Mapping[str, Any],
    output_dir: str | Path,
    *,
    options: SliceExportOptions | None = None,
) -> PublishedSampleExportResult:
    """Export the server-frozen revision into a LeRobot v3 ZIP archive.

    Only official artifact URIs and checksums from the persisted JobRun
    manifest are accepted.  The URIs are never written to the output archive.
    """
    rows = _validated_manifest_rows(manifest)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    export_options = options or options_for_manifest(manifest)

    with ExitStack() as stack:
        roots: dict[str, Path] = {}
        for uri, checksum in _unique_packages(rows):
            local = stack.enter_context(materialize_for_processing(uri))
            if local is None or not local.exists():
                raise ValueError("dataset export manifest has no materializable official package")
            if not local.is_dir():
                raise ValueError("dataset export official package must be a directory")
            try:
                actual = tree_sha256(local)
            except (OSError, ValueError) as exc:
                raise ValueError("dataset export official package checksum is unavailable") from exc
            checksum_matches = hmac.compare_digest(actual, checksum)
            if not checksum_matches:
                legacy = legacy_batch_tree_sha256(local)
                checksum_matches = hmac.compare_digest(legacy, checksum)
            if not checksum_matches:
                raise ValueError("dataset export official package checksum changed")
            resolved_root = ensure_processable_dataset_path(str(local), source_uri=uri)
            dataset_root = Path(resolved_root) if resolved_root is not None else None
            if dataset_root is None or not dataset_root.is_dir():
                raise ValueError("dataset export official package is not a QRDF dataset")
            roots[uri] = dataset_root

        requests = build_slice_requests(manifest, roots)
        lerobot_root = output / "lerobot"
        report = export_episode_slices_to_lerobot_v3(
            requests,
            lerobot_root,
            options=export_options,
        )

    report_payload = {
        "lerobot_version": report.lerobot_version,
        "items": [asdict(item) for item in report.items],
        "input_manifest_sha256": report.input_manifest_sha256,
        "artifact_manifest_sha256": report.artifact_manifest_sha256,
        "options": _options_payload(export_options),
    }
    sidecar = build_export_sidecar(manifest, report_payload)
    (lerobot_root / "quicdata_manifest.json").write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (lerobot_root / "quicdata_export_report.json").write_text(
        json.dumps(report_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    archive_path = output / "published_sample_lerobot_v3.zip"
    _zip_tree(lerobot_root, archive_path)
    return PublishedSampleExportResult(archive_path, report, sidecar)


def build_slice_requests(
    manifest: Mapping[str, Any],
    roots: Mapping[str, Path],
) -> tuple[SliceExportRequest, ...]:
    """Resolve manifest rows to QRDF episode IDs and half-open ranges."""
    requests: list[SliceExportRequest] = []
    readers: dict[Path, QRDFReader] = {}
    for row in _validated_manifest_rows(manifest):
        uri = str(row["package_uri"])
        root = roots.get(uri)
        if root is None:
            raise ValueError("dataset export manifest package is unavailable")
        reader = readers.setdefault(root, QRDFReader(root))
        episode_id = _resolve_episode_id(reader, str(row["source_episode_id"]))
        episode = reader.load_episode(episode_id)
        start_ns, end_ns = _resolve_range(row, episode.metadata.timing)
        requests.append(
            SliceExportRequest(
                sample_id=str(row["sample_id"]),
                source_qrdf_path=root,
                source_fingerprint=str(row["source_fingerprint"]).lower(),
                source_episode_id=episode_id,
                start_ns=start_ns,
                end_ns=end_ns,
                task=str(row["task"]),
                split=str(row["split"]),
                position=int(row["position"]),
            )
        )
    return tuple(requests)


def options_for_manifest(
    manifest: Mapping[str, Any],
    overrides: Mapping[str, Any] | None = None,
) -> SliceExportOptions:
    """Build a bounded physical profile, inferring EGO vs manipulation."""
    rows = _validated_manifest_rows(manifest)
    modalities = {str(row.get("modality") or "").lower() for row in rows}
    inferred_mode = "ego_rgb" if modalities == {"ego"} else "manipulation"
    raw = dict(overrides or {})
    raw.setdefault("export_mode", inferred_mode)
    if raw.get("image_size") is not None:
        image_size = raw["image_size"]
        if not isinstance(image_size, (list, tuple)) or len(image_size) != 2:
            raise ValueError("export image_size is invalid")
        raw["image_size"] = (int(image_size[0]), int(image_size[1]))
    allowed = {
        "lerobot_version",
        "export_mode",
        "fps",
        "image_size",
        "max_time_delta_ms",
        "max_frames_per_shard",
        "max_sample_duration_s",
        "video_codec",
        "video_pixel_format",
        "video_crf",
        "video_gop_size",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError("export profile contains unsupported options")
    return SliceExportOptions(**raw)


def _validated_manifest_rows(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(manifest, Mapping):
        raise ValueError("dataset export manifest is unavailable")
    rows = manifest.get("items")
    if not isinstance(rows, list) or not rows:
        raise ValueError("dataset export manifest has no items")
    validated: list[dict[str, Any]] = []
    positions: set[int] = set()
    sample_ids: set[str] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ValueError("dataset export manifest item is invalid")
        row = dict(raw)
        uri = row.get("package_uri")
        checksum = str(row.get("package_checksum") or "").lower()
        split = row.get("split")
        kind = row.get("item_kind")
        position = row.get("position")
        sample_id = str(row.get("sample_id") or "").strip()
        fingerprint = str(row.get("source_fingerprint") or "").lower()
        task = str(row.get("task") or "").strip()
        if not isinstance(uri, str) or not uri:
            raise ValueError("dataset export package URI is unavailable")
        if not _SHA256.fullmatch(checksum):
            raise ValueError("dataset export package checksum is unavailable")
        if split not in _SPLITS or kind not in _ITEM_KINDS:
            raise ValueError("dataset export manifest item dimensions are invalid")
        if (
            not isinstance(position, int)
            or isinstance(position, bool)
            or position < 0
            or position in positions
        ):
            raise ValueError("dataset export item position is invalid")
        if not sample_id or "/" in sample_id or "\\" in sample_id or sample_id in sample_ids:
            raise ValueError("dataset export sample_id is invalid")
        if not _SHA256.fullmatch(fingerprint):
            raise ValueError("dataset export source fingerprint is invalid")
        if not str(row.get("source_episode_id") or "").strip() or not task:
            raise ValueError("dataset export sample context is incomplete")
        positions.add(position)
        sample_ids.add(sample_id)
        row["package_uri"] = uri
        row["package_checksum"] = checksum
        row["source_fingerprint"] = fingerprint
        row["sample_id"] = sample_id
        row["task"] = task
        validated.append(row)
    return sorted(validated, key=lambda item: int(item["position"]))


def _unique_packages(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    packages: dict[str, str] = {}
    for row in rows:
        uri = str(row["package_uri"])
        checksum = str(row["package_checksum"])
        previous = packages.setdefault(uri, checksum)
        if previous != checksum:
            raise ValueError("one official package URI has conflicting checksums")
    return list(packages.items())


def _resolve_episode_id(reader: QRDFReader, requested: str) -> str:
    episode_ids = reader.list_episodes()
    if requested in episode_ids:
        return requested
    metadata_matches = []
    for episode_id in episode_ids:
        episode = reader.load_episode(episode_id)
        if episode.metadata.episode_id == requested:
            metadata_matches.append(episode_id)
    if len(metadata_matches) == 1:
        return metadata_matches[0]
    if len(episode_ids) == 1:
        # Official publication currently emits one QRDF package per published
        # episode and renumbers its portable ID to episode_000001.
        return episode_ids[0]
    raise ValueError("source Episode is ambiguous in the official package")


def _resolve_range(row: Mapping[str, Any], timing: Any) -> tuple[int, int]:
    start = _optional_int(row.get("effective_start_ns"))
    end = _optional_int(row.get("effective_end_ns"))
    if start is None or end is None:
        start = _optional_int(getattr(timing, "start_timestamp_ns", None))
        end = _optional_int(getattr(timing, "end_timestamp_ns", None))
    if start is None or end is None or start >= end:
        raise ValueError("official QRDF episode timing is invalid")
    return start, end


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _options_payload(options: SliceExportOptions) -> dict[str, Any]:
    payload = asdict(options)
    if options.image_size is not None:
        payload["image_size"] = list(options.image_size)
    return payload


def _zip_tree(root: Path, archive_path: Path) -> None:
    if not root.is_dir():
        raise ValueError("LeRobot export root is unavailable")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive_path.with_suffix(archive_path.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            archive.write(path, path.relative_to(root).as_posix())
    temporary.replace(archive_path)


__all__ = [
    "PublishedSampleExportResult",
    "build_slice_requests",
    "export_published_sample_revision",
    "options_for_manifest",
]
