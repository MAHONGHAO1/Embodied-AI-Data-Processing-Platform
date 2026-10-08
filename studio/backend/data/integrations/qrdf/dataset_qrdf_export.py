"""Export immutable DatasetRevision manifests as canonical QRDF archives."""

from __future__ import annotations

import hmac
import json
import shutil
import zipfile
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qrdf.models.dataset import DatasetManifest
from qrdf.models.episode import EpisodeMetadata
from qrdf.reader.reader import QRDFReader
from qrdf.validator.validator import QRDFValidator

from data.integrations.qrdf.published_sample_export import (
    _unique_packages,
    _validated_manifest_rows,
)
from data.integrations.qrdf.service import classify_storage_layout
from data.services.cloud_storage import materialize_for_processing, verified_publish_snapshot
from data.services.published_sample_manifest import build_export_sidecar, canonical_manifest_hash
from data.utils.checksums import legacy_batch_tree_sha256, tree_sha256


@dataclass(frozen=True)
class QrdfDatasetExportResult:
    archive_path: Path
    item_count: int
    dataset_manifest_sha256: str
    source_manifest_sha256: str


@dataclass(frozen=True)
class _VerifiedQrdfPackage:
    dataset_root: Path | None = None
    episode_path: Path | None = None


def export_qrdf_revision(
    manifest: Mapping[str, Any],
    output_dir: str | Path,
) -> QrdfDatasetExportResult:
    """Build a portable QRDF dataset from full immutable published Episodes."""
    rows = _validated_manifest_rows(manifest)
    if any(row["item_kind"] != "episode" for row in rows):
        raise ValueError("QRDF export supports full published Episodes only")

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    dataset_root = output / "qrdf"
    if dataset_root.exists():
        raise ValueError("QRDF export staging already exists")
    episodes_root = dataset_root / "episodes"
    episodes_root.mkdir(parents=True)

    source_map: list[dict[str, Any]] = []
    with ExitStack() as stack:
        packages: dict[str, _VerifiedQrdfPackage] = {}
        for uri, checksum in _unique_packages(rows):
            local = stack.enter_context(materialize_for_processing(uri))
            if local is None:
                raise ValueError("dataset export official package is unavailable")
            snapshot = stack.enter_context(verified_publish_snapshot(local))
            packages[uri] = _verified_qrdf_package(
                snapshot,
                source_uri=str(snapshot),
                checksum=checksum,
            )

        for index, row in enumerate(rows, start=1):
            exported_id = f"episode_{index:06d}"
            package = packages[str(row["package_uri"])]
            if package.episode_path is not None:
                source_path = package.episode_path
            elif package.dataset_root is not None:
                reader = QRDFReader(package.dataset_root)
                source_id = _resolve_episode_id(reader, str(row["source_episode_id"]))
                source_path = reader.load_episode(source_id).path
            else:
                raise ValueError("dataset export official package is not a QRDF dataset")
            destination = episodes_root / exported_id
            _copy_episode(source_path, destination, exported_id=exported_id)
            source_map.append(
                {
                    "position": int(row["position"]),
                    "exported_episode_id": exported_id,
                    "episode_id": int(row["episode_id"]),
                    "published_episode_id": int(row["published_episode_id"]),
                    "source_episode_id": str(row["source_episode_id"]),
                    "source_fingerprint": str(row["source_fingerprint"]),
                }
            )

    dataset_manifest = DatasetManifest(
        dataset_name=str(
            manifest.get("name") or f"dataset-{manifest.get('dataset_id') or 'export'}"
        ),
        description=f"QuicData Dataset Revision {manifest.get('version') or ''}".strip(),
    )
    for row in source_map:
        dataset_manifest.add_episode(str(row["exported_episode_id"]), split="train")
    dataset_manifest.save(dataset_root / "dataset.json")

    report = QRDFValidator().validate_canonical_dataset(dataset_root)
    if report.error_count:
        raise ValueError("exported QRDF dataset failed canonical validation")
    sidecar = build_export_sidecar(
        manifest,
        {
            "export_profile": "qrdf",
            "items": source_map,
            "source_manifest_sha256": canonical_manifest_hash(manifest),
        },
    )
    (dataset_root / "quicdata_manifest.json").write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    dataset_manifest_sha256 = tree_sha256(dataset_root)
    archive_path = output / "dataset_revision_qrdf.zip"
    _zip_tree(dataset_root, archive_path)
    return QrdfDatasetExportResult(
        archive_path=archive_path,
        item_count=len(rows),
        dataset_manifest_sha256=dataset_manifest_sha256,
        source_manifest_sha256=canonical_manifest_hash(manifest),
    )


def _verified_qrdf_package(
    local: Path | None,
    *,
    source_uri: str,
    checksum: str,
) -> _VerifiedQrdfPackage:
    if local is None or not local.is_dir() or local.is_symlink():
        raise ValueError("dataset export official package must be a directory")
    actual = tree_sha256(local)
    if not hmac.compare_digest(actual, checksum):
        legacy = legacy_batch_tree_sha256(local)
        if not hmac.compare_digest(legacy, checksum):
            raise ValueError("dataset export official package checksum changed")
    layout = classify_storage_layout(str(local), source_uri=source_uri)
    if layout["kind"] == "standard" and layout.get("root") is not None:
        return _VerifiedQrdfPackage(dataset_root=_contained_directory(local, layout["root"]))
    if layout["kind"] == "single_episode" and layout.get("episode_dir") is not None:
        return _VerifiedQrdfPackage(episode_path=_contained_directory(local, layout["episode_dir"]))
    raise ValueError("dataset export official package is not a QRDF dataset")


def _contained_directory(root: Path, candidate: object) -> Path:
    resolved_root = Path(root).resolve()
    resolved = Path(candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("dataset export QRDF content escaped its verified package") from exc
    if not resolved.is_dir() or resolved.is_symlink():
        raise ValueError("dataset export QRDF content is unsafe")
    return resolved


def _resolve_episode_id(reader: QRDFReader, requested: str) -> str:
    episode_ids = reader.list_episodes()
    if requested in episode_ids:
        return requested
    matches = [
        episode_id
        for episode_id in episode_ids
        if reader.load_episode(episode_id).metadata.episode_id == requested
    ]
    if len(matches) == 1:
        return matches[0]
    if len(episode_ids) == 1:
        return episode_ids[0]
    raise ValueError("source Episode is ambiguous in the official package")


def _copy_episode(source: Path, destination: Path, *, exported_id: str) -> None:
    source = Path(source)
    if source.is_symlink() or not source.is_dir():
        raise ValueError("QRDF Episode source is unsafe")
    shutil.copytree(source, destination, symlinks=False)
    metadata_path = destination / "metadata.json"
    metadata = EpisodeMetadata.load(metadata_path)
    metadata.episode_id = exported_id
    metadata.save(metadata_path)


def _zip_tree(root: Path, archive_path: Path) -> None:
    temporary = archive_path.with_suffix(archive_path.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError("QRDF export contains a symlink")
            if path.is_file():
                archive.write(path, path.relative_to(root).as_posix())
    temporary.replace(archive_path)


__all__ = ["QrdfDatasetExportResult", "export_qrdf_revision"]
