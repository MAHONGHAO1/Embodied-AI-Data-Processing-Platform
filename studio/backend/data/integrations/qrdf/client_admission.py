"""Client-precheck admission: trust the re-judged client report, verify previews only.

The raw MCAP is never downloaded here.  Its identity is proven by a provider
HEAD (size, etag, version) and its digest is the one the client declared; a
provider-reported SHA-256 that disagrees with the declaration falls back to
server mode.  MCAP and preview parts of client_admission sources must all carry
Content-MD5, but the declared MCAP digest is still the client's claim, not a
server measurement.  Only the preview files are downloaded and checked against
the declared fingerprints.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from data.infra.object_storage import ObjectStorageError, StorageObjectRef
from data.integrations.qrdf.admission import (
    QRDFAdmissionResult,
    _is_rgb_episode,
    _preview_artifacts_valid,
    _sha256_file,
)
from data.services.client_admission import (
    NON_BLOCKING_ISSUE_CODES,
    PREVIEW_MANIFEST_PATH,
    ClientReportInvalid,
    client_admission_capabilities,
    judge_client_report,
)
from data.services.episode_objects import object_entry


class ClientAdmissionFallback(Exception):
    """The client result cannot be used; this attempt reruns in server mode."""

    def __init__(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = dict(detail or {})


class _DeclaredEpisode:
    """The part of a qrdf ``Episode`` preview validation reads, without an MCAP."""

    def __init__(self, path: Path) -> None:
        from qrdf.models.episode import EpisodeMetadata

        self._path = path
        self._metadata = EpisodeMetadata.load(path / "metadata.json")

    def list_streams(self) -> list[Any]:
        return [stream for device in self._metadata.devices for stream in device.streams]

    def list_topics(self) -> list[str]:
        return [str(topic.name) for topic in self._metadata.topics]

    def load_rgb_preview_manifest(self) -> Any:
        from qrdf.models import PreviewManifest

        manifest_path = self._path / "media" / "preview" / "manifest.json"
        if not manifest_path.is_file():
            return None
        return PreviewManifest.load(manifest_path)


def _gate(declared: dict[str, Any]) -> None:
    capabilities = client_admission_capabilities()
    if not capabilities["enabled"]:
        raise ClientAdmissionFallback("client_admission_disabled")
    if declared.get("qrdf_version") not in capabilities["accepted_qrdf_versions"]:
        raise ClientAdmissionFallback("client_qrdf_version_not_accepted")
    if declared.get("policy_version") not in capabilities["accepted_policy_versions"]:
        raise ClientAdmissionFallback("client_policy_version_not_accepted")


def _head(provider: Any, ref: StorageObjectRef, *, unreadable: str) -> StorageObjectRef:
    """HEAD one object; a missing or unreadable object means the client result is unusable."""
    try:
        return provider.head(ref)
    except ObjectStorageError as exc:  # includes StorageObjectNotFound
        raise ClientAdmissionFallback(unreadable, {"error_type": type(exc).__name__}) from exc


def _preview_heads(provider: Any, files: list[dict[str, Any]]) -> dict[str, StorageObjectRef]:
    heads: dict[str, StorageObjectRef] = {}
    for item in files:
        stored = item.get("object")
        if not isinstance(stored, dict) or stored.get("bucket_role") != "process":
            raise ClientAdmissionFallback("client_preview_object_missing")
        ref = StorageObjectRef(
            "process",
            str(stored["object_key"]),
            stored.get("version_id"),
            str(stored.get("etag") or ""),
            int(stored["size_bytes"]),
            None,
        )
        head = _head(provider, ref, unreadable="client_preview_object_missing")
        if (
            head.size_bytes != int(item["size_bytes"])
            or not head.etag
            or (ref.etag and head.etag != ref.etag)
        ):
            raise ClientAdmissionFallback("client_preview_object_mismatch")
        heads[str(item["path"])] = head
    return heads


def _publish(
    process_provider: Any, local: Path, prefix: str, relative: str, kind: str
) -> dict[str, Any]:
    sha256 = _sha256_file(local)
    ref = StorageObjectRef(
        "process", f"{prefix}/{relative}", None, "", local.stat().st_size, sha256
    )
    persisted = process_provider.put_worker_object(ref, str(local))
    return object_entry(path=relative, kind=kind, ref={**asdict(persisted), "sha256": sha256})


def admit_client_prechecked(
    scratch: Path,
    *,
    state: dict[str, Any],
    source_ref: StorageObjectRef,
    source_fingerprint: str,
    provider: Any,
    process_provider: Any,
    check_owned: Callable[[], None],
) -> tuple[QRDFAdmissionResult, dict[str, Any]]:
    """Admit one source from its client admission, or raise ``ClientAdmissionFallback``."""
    declared = dict(state["client_admission"])
    _gate(declared)
    try:
        integrity_status, issues = judge_client_report(declared.get("report"))
    except ClientReportInvalid as exc:
        raise ClientAdmissionFallback("client_report_invalid") from exc
    client_report = dict(declared["report"])
    report: dict[str, Any] = {
        "ok": False,
        "error_count": sum(issue["severity"] == "ERROR" for issue in issues),
        "warning_count": sum(issue["severity"] == "WARNING" for issue in issues),
        "issues": issues,
        "media": {},
        "media_validation": client_report.get("media_validation"),
        "qrdf_runtime": {"sdk_version": str(declared["qrdf_version"]), "producer": "client"},
        "source_fingerprint": source_fingerprint,
        "integrity_source": "client",
        "client_admission": {
            "qrdf_version": declared["qrdf_version"],
            "policy_version": declared["policy_version"],
            "report": client_report,
            "judgement": {
                "integrity_status": integrity_status,
                "non_blocking_issue_codes": sorted(NON_BLOCKING_ISSUE_CODES),
            },
        },
    }
    if integrity_status == "failed":
        # The server's own verdict: a blocking error is final, never a fallback.
        failed = QRDFAdmissionResult(
            ok=False,
            integrity_status="failed",
            preview_status="not_applicable",
            output_verification_status="failed",
            report=report,
            issues=tuple(issues),
            qrdf_profile="qrdf",
        )
        return failed, dict(asdict(source_ref))

    check_owned()
    raw = _head(provider, source_ref, unreadable="client_source_object_mismatch")
    if (
        raw.size_bytes != source_ref.size_bytes
        or not raw.etag
        or (source_ref.etag and raw.etag != source_ref.etag)
        or (source_ref.version_id and raw.version_id != source_ref.version_id)
        # A provider-measured digest outranks the declaration; never overwrite it.
        or (raw.sha256 and raw.sha256 != str(state["declared_sha256"]))
    ):
        raise ClientAdmissionFallback("client_source_object_mismatch")
    files = [dict(item) for item in declared.get("files") or []]
    heads = _preview_heads(provider, files)
    declared_sha256 = {str(item["path"]): str(item["sha256"]) for item in files}
    metadata_text = str(state["metadata_text"])
    (scratch / "metadata.json").write_text(metadata_text, encoding="utf-8")
    episode = _DeclaredEpisode(scratch)
    kinds: dict[str, tuple[str, str | None]] = {}
    if not files:
        if _is_rgb_episode(episode):
            raise ClientAdmissionFallback("client_preview_missing")
        preview_status, media_facts = "not_applicable", {"status": "not_applicable"}
    else:
        check_owned()
        for item in files:
            destination = scratch / str(item["path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                provider.download_file(heads[str(item["path"])], str(destination))
            except Exception as exc:  # noqa: BLE001 - any provider failure means no usable preview.
                raise ClientAdmissionFallback("client_preview_object_missing") from exc
            if (
                destination.is_symlink()
                or destination.stat().st_size != int(item["size_bytes"])
                or _sha256_file(destination) != item["sha256"]
            ):
                raise ClientAdmissionFallback("client_preview_hash_mismatch")
        expected_source = {
            "data_mcap_sha256": str(state["declared_sha256"]),
            "metadata_sha256": hashlib.sha256(metadata_text.encode("utf-8")).hexdigest(),
        }
        valid, media_facts = _preview_artifacts_valid(
            scratch, episode, expected_source=expected_source
        )
        if not valid:
            raise ClientAdmissionFallback("client_preview_invalid", media_facts)
        kinds[PREVIEW_MANIFEST_PATH] = ("preview_manifest", None)
        for stream in media_facts["streams"]:
            kinds[f"media/preview/{stream['video']}"] = ("preview_video", stream["topic"])
            kinds[f"media/preview/{stream['timeline']}"] = ("preview_timeline", stream["topic"])
        if set(kinds) != set(heads):
            raise ClientAdmissionFallback("client_preview_files_mismatch")
        preview_status = "ready"

    report["ok"] = True
    report["media"] = media_facts
    report_path = scratch / "admission-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    prefix = f"process/v2/qrdf/{uuid4().hex}"
    objects = [
        _publish(process_provider, scratch / "metadata.json", prefix, "metadata.json", "metadata"),
        _publish(
            process_provider, report_path, prefix, "admission-report.json", "admission_report"
        ),
    ]
    for path in sorted(kinds):
        kind, topic = kinds[path]
        objects.append(
            object_entry(
                path=path,
                kind=kind,
                topic=topic,
                ref={**asdict(heads[path]), "sha256": declared_sha256[path]},
            )
        )
    verified_source = {
        **asdict(raw),
        "sha256": str(state["declared_sha256"]),
        "relative_path": str(state["data_path"]),
    }
    admitted = QRDFAdmissionResult(
        ok=True,
        integrity_status="passed",
        preview_status=preview_status,
        output_verification_status="verified",
        report=report,
        issues=tuple(issues),
        qrdf_profile="qrdf",
        objects=tuple(objects),
    )
    return admitted, verified_source
