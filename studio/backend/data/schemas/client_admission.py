"""Duance <-> Studio client-admission contract.

Request models are enforced by the upload-session router; response models
describe the JSON the router returns.  ``docs/COLLECTION_UPLOAD_API.md`` holds
one named example for each shape and the contract tests validate them here.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SHA256_PATTERN = r"^[0-9a-f]{64}$"
CONTENT_MD5_PATTERN = r"^[A-Za-z0-9+/]{22}==$"


class ClientAdmissionCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    accepted_qrdf_versions: list[str]
    accepted_policy_versions: list[str]
    non_blocking_issue_codes: list[str]


class UploadCapabilitiesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_admission: ClientAdmissionCapabilities


class ClientAdmissionIssue(BaseModel):
    """One QRDF ValidationIssue as the client serialized it."""

    model_config = ConfigDict(extra="allow")

    severity: Literal["ERROR", "WARNING", "INFO"]
    code: str = Field(min_length=1, max_length=128)
    message: str | None = Field(default=None, max_length=4096)
    path: str | None = Field(default=None, max_length=1024)
    topic: str | None = Field(default=None, max_length=512)


class ClientAdmissionReport(BaseModel):
    """The client's validation report; ``ok`` is recorded but never trusted."""

    model_config = ConfigDict(extra="allow")

    ok: bool
    issues: list[ClientAdmissionIssue] = Field(max_length=1000)
    media_validation: dict[str, Any] | None


class ClientAdmissionFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=SHA256_PATTERN)


class ClientAdmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    qrdf_version: str = Field(min_length=1, max_length=32)
    policy_version: str = Field(min_length=1, max_length=32)
    report: ClientAdmissionReport
    files: list[ClientAdmissionFile] = Field(max_length=256)


class DeclaredPreviewFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: str = Field(pattern=SHA256_PATTERN)
    path: str
    size_bytes: int = Field(gt=0)


class DeclaredSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(pattern=SHA256_PATTERN)
    package_uid: str
    episode_id: str
    preview_files: list[DeclaredPreviewFile] | None = None


class DeclarationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: str
    declared_packages: int
    declared_sources: int
    sources: list[DeclaredSource]


class SignedPartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: str
    part_number: int = Field(ge=1)
    content_length: int = Field(gt=0)
    method: Literal["PUT"]
    url: str
    expires_in: int
    headers: dict[str, str]


class SourceAdmissionStatus(BaseModel):
    """One entry of ``qrdf_facts.source_admission.sources`` on package detail."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(pattern=SHA256_PATTERN)
    episode_id: int | None = None
    status: Literal["queued", "running", "ready", "failed"]
    attempt: int | None = None
    error_code: str | None = None
    integrity_source: Literal["client", "server"] | None = None
    client_admission_fallback: str | None = None
