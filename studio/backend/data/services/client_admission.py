"""Server rules for Duance client admission.

Capabilities, declaration normalization, preview file identities and the
server's own re-judgement of a client report live here so the router, the
upload intake, the parse job and the admission worker share one definition.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from data.config import settings

# Issue codes that never block intake: frame alignment and robot channel
# readiness are deferred to batch-build QC.  This is the one table both the
# client (via capabilities) and the server use to decide integrity.
NON_BLOCKING_ISSUE_CODES = frozenset(
    {
        "NO_VALID_TRAINING_FRAMES",
        "NO_VALID_EGO_FRAMES",
        "NO_STATE_TOPIC",
        "NO_ACTION_TOPIC",
    }
)


def client_admission_capabilities() -> dict[str, object]:
    """The ``client_admission`` block of the upload capabilities response."""
    return {
        "enabled": bool(settings.accept_client_admission),
        "accepted_qrdf_versions": list(settings.client_admission_qrdf_versions),
        "accepted_policy_versions": list(settings.client_admission_policy_versions),
        "non_blocking_issue_codes": sorted(NON_BLOCKING_ISSUE_CODES),
    }


PREVIEW_MANIFEST_PATH = "media/preview/manifest.json"
MAX_CLIENT_REPORT_BYTES = 1024 * 1024
_PATH_COMPONENT_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}")


class ClientAdmissionDeclarationError(ValueError):
    """A declared client admission block is rejected before any upload starts."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _preview_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        raise ClientAdmissionDeclarationError("client_admission_path_unsafe")
    parts = value.split("/")
    # Empty, ".", ".." and hidden segments all fail the component pattern.
    if any(_PATH_COMPONENT_RE.fullmatch(part) is None for part in parts):
        raise ClientAdmissionDeclarationError("client_admission_path_unsafe")
    if len(parts) < 3 or parts[0] != "media" or parts[1] != "preview":
        raise ClientAdmissionDeclarationError("client_admission_path_outside_preview")
    return value


def normalize_client_admission(raw: Any) -> dict[str, Any]:
    """Validate a schema-checked client admission block for durable storage."""
    if not isinstance(raw, dict) or not isinstance(raw.get("files"), list):
        raise ClientAdmissionDeclarationError("client_admission_invalid")
    report = raw.get("report")
    encoded = json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_CLIENT_REPORT_BYTES:
        raise ClientAdmissionDeclarationError("client_admission_report_too_large")
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw["files"]:
        path = _preview_path(item.get("path"))
        if path in seen:
            raise ClientAdmissionDeclarationError("client_admission_path_duplicate")
        seen.add(path)
        files.append(
            {"path": path, "size_bytes": int(item["size_bytes"]), "sha256": str(item["sha256"])}
        )
    if files and PREVIEW_MANIFEST_PATH not in seen:
        raise ClientAdmissionDeclarationError("client_admission_manifest_missing")
    return {
        "qrdf_version": str(raw["qrdf_version"]),
        "policy_version": str(raw["policy_version"]),
        "report": report,
        "files": sorted(files, key=lambda item: item["path"]),
    }


def preview_file_id(source_id: str, path: str) -> str:
    """Opaque upload identity of one preview file, stable across declaration replays."""
    return hashlib.sha256(f"{source_id}:{path}".encode()).hexdigest()


def preview_object_key(*, workspace_id: int, upload_session_id: str, path: str) -> str:
    """Server-owned process-bucket key; the file name keeps the provider's type inference."""
    return (
        f"process/v2/workspaces/{workspace_id}/collection-uploads/"
        f"{upload_session_id}/{uuid4().hex}/{PurePosixPath(path).name}"
    )


_SEVERITIES = frozenset({"ERROR", "WARNING", "INFO"})


class ClientReportInvalid(ValueError):
    """A stored client report cannot be judged; the worker falls back to server mode."""


def judge_client_report(report: Any) -> tuple[str, list[dict[str, Any]]]:
    """Re-judge client issues with the server's own table; the client ``ok`` is ignored."""
    if not isinstance(report, dict) or not isinstance(report.get("issues"), list):
        raise ClientReportInvalid("client_report_invalid")
    issues: list[dict[str, Any]] = []
    for raw in report["issues"]:
        if (
            not isinstance(raw, dict)
            or raw.get("severity") not in _SEVERITIES
            or not isinstance(raw.get("code"), str)
            or not raw["code"]
        ):
            raise ClientReportInvalid("client_report_invalid")
        issues.append(
            {
                "severity": raw["severity"],
                "code": raw["code"],
                "message": str(raw.get("message") or ""),
                "path": raw.get("path") if isinstance(raw.get("path"), str) else None,
                "topic": raw.get("topic") if isinstance(raw.get("topic"), str) else None,
            }
        )
    blocking = any(
        issue["severity"] == "ERROR" and issue["code"] not in NON_BLOCKING_ISSUE_CODES
        for issue in issues
    )
    return ("failed" if blocking else "passed"), issues


def client_admission_state(result: dict[str, Any], *, source: dict[str, Any]) -> dict[str, Any]:
    """What the admission worker needs from one declared source's client admission."""
    declared = source.get("client_admission")
    if not isinstance(declared, dict):
        return {}
    uploads = result.get("oss_multipart_files") or {}
    files: list[dict[str, Any]] = []
    for item in declared.get("files") or []:
        upload = uploads.get(item.get("file_id"))
        identity = (
            upload.get("provider_identity")
            if isinstance(upload, dict) and upload.get("completion_state") == "completed"
            else None
        )
        files.append(
            {
                "path": item["path"],
                "size_bytes": item["size_bytes"],
                "sha256": item["sha256"],
                "object": dict(identity) if isinstance(identity, dict) else None,
            }
        )
    return {
        "client_admission": {
            "qrdf_version": declared.get("qrdf_version"),
            "policy_version": declared.get("policy_version"),
            "report": declared.get("report"),
            "files": files,
        }
    }
