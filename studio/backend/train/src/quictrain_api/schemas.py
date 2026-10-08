from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ResourceSelection(BaseModel):
    mode: Literal["AUTO", "MANUAL"] = "AUTO"
    profile: str | None = None


class JobValidationRequest(BaseModel):
    project_id: str = "prj_robot_arm"
    dataset_version_id: str
    model_version_id: str
    recipe_id: str = "fine_tune"
    preset_id: str | None = None
    config_overrides: dict[str, Any] = Field(default_factory=dict)
    resource_selection: ResourceSelection = Field(default_factory=ResourceSelection)


class JobCreateRequest(JobValidationRequest):
    client_request_id: str
    display_name: str | None = None


class DatasetUpsertEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    event_type: Literal["dataset_version.published", "dataset_version.deprecated"]
    occurred_at: str
    dataset_version: dict[str, Any]


class DatasetRegisterRequest(BaseModel):
    """Browser / operator registration of an immutable dataset version (OSS or CPFS URI)."""

    model_config = ConfigDict(extra="forbid")

    client_request_id: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=240)
    uri: str = Field(min_length=8, max_length=1024)
    version: str = Field(default="v1", min_length=1, max_length=128)
    dataset_id: str | None = Field(default=None, min_length=1, max_length=240)
    checksum: str | None = Field(default=None, min_length=1, max_length=512)
    # READY = schedulable immediately (4090 OSS direct mount / pre-staged CPFS).
    # REGISTERED = wait for OSS→CPFS materialization before scheduling.
    status: Literal["READY", "REGISTERED"] = "REGISTERED"
    request_materialization: bool = False
    format: str = "lerobot"
    format_version: str = "3.0"
    episodes: int | None = Field(default=None, ge=0)
    frames: int | None = Field(default=None, ge=0)
    duration_hours: float | None = Field(default=None, ge=0.0)
    fps: float | None = Field(default=None, gt=0)
    robot_type: str | None = Field(default=None, min_length=1, max_length=128)
    camera_keys: list[str] | None = None
    # Zero means the feature is known to be absent; None means it has not been verified.
    action_dim: int | None = Field(default=None, ge=0)
    state_dim: int | None = Field(default=None, ge=0)
    language_tasks: bool | None = None


class MembershipWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str | None = None
    email: str | None = None
    display_name: str | None = None
    role: Literal["viewer", "operator", "admin"] = "viewer"
    # Optional initial/rotated password for local multi-user login (never returned by API).
    password: str | None = Field(default=None, min_length=8, max_length=128)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=240)
    password: str = Field(min_length=1, max_length=128)


class ProjectPolicyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrent_jobs: int | None = Field(default=None, ge=1, le=256)
    max_gpus: int | None = Field(default=None, ge=1, le=256)
    max_runtime_seconds: int | None = Field(default=None, ge=60, le=7 * 24 * 3600)
    provider_disabled: bool | None = None


class StructuredError(BaseModel):
    code: str
    message: str
    field: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    trace_id: str
    retryable: bool = False
