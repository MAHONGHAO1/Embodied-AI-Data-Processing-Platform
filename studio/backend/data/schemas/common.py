from datetime import datetime
from typing import Any, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    code: int = 200
    message: str = "Success"
    data: T | None = None


class PublishedQrdfEpisodeOut(BaseModel):
    """Safe, browse-oriented projection of an immutable published Episode."""

    id: int
    qrdf_id: int
    subject_type: Literal["whole_source", "derived_asset"]
    status: Literal["published"]
    output_profile: str
    published_at: str


class PublishedQrdfEpisodeList(BaseModel):
    total: int
    items: list[PublishedQrdfEpisodeOut]


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenData(BaseModel):
    token: str
    userInfo: dict[str, Any]


class WorkspaceCreate(BaseModel):
    workspace_name: str
    desc: str = ""
    creator: str = ""


class WorkspaceOut(BaseModel):
    id: int
    name: str
    description: str
    creator: str
    project_count: int = 0
    task_count: int = 0

    model_config = {"from_attributes": True}


class ProjectCreate(BaseModel):
    workspace_id: int
    name: str
    description: str = ""
    scene: str = ""


class PersonnelProfileCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=128)
    is_shared: bool = False


CollectionSourceKind = Literal[
    "robot", "camera", "workstation", "site_batch", "mobile_phone", "other"
]

COLLECTION_SOURCE_KIND_LABELS = {
    "robot": "机器人",
    "camera": "相机",
    "workstation": "工作站",
    "site_batch": "现场批次",
    "mobile_phone": "iPhone 手机",
    "other": "其他",
}


class PersonnelProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    is_active: bool | None = None

    model_config = {"extra": "forbid"}


class CollectionSourceUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=128)
    kind: CollectionSourceKind | None = None
    external_key: str | None = Field(default=None, max_length=128)
    is_active: bool | None = None

    model_config = {"extra": "forbid"}


class CollectionSourceCreate(BaseModel):
    label: str = Field(min_length=1, max_length=128)
    kind: CollectionSourceKind = "other"
    external_key: str | None = Field(default=None, max_length=128)
    is_shared: bool = False

    @property
    def kind_label(self) -> str:
        return COLLECTION_SOURCE_KIND_LABELS[self.kind]


class CollectionAttributionAssign(BaseModel):
    collection_source_id: int = Field(gt=0)
    collector_id: int = Field(gt=0)


class LegacyAttributionRequest(BaseModel):
    task_ids: list[int] = Field(default_factory=list)


class TaskCreate(BaseModel):
    workspace_id: int | None = None
    project_id: int | None = None
    pipeline_key: str = Field(default="", max_length=64)
    task_type: str = "collect"
    data_source: str = ""
    name: str = ""
    scene: str = ""
    remark: str = ""
    collection_source_id: int | None = Field(default=None, gt=0)
    collector_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_collection_attribution_pair(self):
        if (self.collection_source_id is None) != (self.collector_id is None):
            raise ValueError("collector_id and collection_source_id must be provided together")
        return self


WorkItemStatus = Literal[
    "pending",
    "assigned",
    "in_progress",
    "submitted",
    "accepted",
    "rejected",
    "cancelled",
    "stale",
]


class WorkItemTransition(BaseModel):
    target: WorkItemStatus
    note: str = Field(default="", max_length=2000)


class WorkItemAssignment(BaseModel):
    assignee_user_id: int = Field(gt=0)
    note: str = Field(default="", max_length=2000)


class WorkItemOut(BaseModel):
    id: int
    task_id: int
    workspace_id: int
    subject_type: str
    subject_id: str
    kind: str
    generation: int
    status: WorkItemStatus
    assignee_user_id: int | None
    version: int
    created_at: datetime
    updated_at: datetime


class WorkItemList(BaseModel):
    items: list[WorkItemOut]
    total: int


class WorkItemEventOut(BaseModel):
    id: int
    work_item_id: int
    event_type: str
    from_status: str | None
    to_status: str
    from_assignee_user_id: int | None
    to_assignee_user_id: int | None
    actor_id: int
    note: str
    created_at: datetime


class WorkItemEventList(BaseModel):
    items: list[WorkItemEventOut]
    total: int


class TaskTransit(BaseModel):
    action: str
    note: str = ""


class TaskClaim(BaseModel):
    user_id: str = ""
    lock_expire: int = Field(default=3600, ge=1, le=86400)


class UploadInit(BaseModel):
    file_md5: str
    total_chunks: int
    file_name: str
    task_id: int | None = None
    workspace_id: int | None = Field(default=None, gt=0)


class UploadMerge(BaseModel):
    upload_id: str
    file_md5: str


class BagImportRequest(BaseModel):
    folder_name: str
    project_id: int
    task_id: int | None = None
    task_name: str | None = None
    episode_limit: int | None = None
    auto_preprocess: bool = True


class OssScanRequest(BaseModel):
    bucket: str
    prefix: str = ""
    extension_filter: str = ".mcap,.zip,.tar,.tar.gz,.json"  # Comma-separated extension filter
    task_id: int


class OssImportRequest(BaseModel):
    bucket: str
    keys: list[str] = Field(min_length=1)
    task_id: int
    workspace_id: int | None = None
    project_id: int | None = None
    auto_preprocess: bool = False


class AuditSubmit(BaseModel):
    is_passed: bool
    reject_reason: str = ""


class CollectReviewSubmit(BaseModel):
    is_passed: bool
    reject_reason: str = ""


class AnnotationItem(BaseModel):
    tag: str
    start: float = 0
    end: float = 0
    bbox: list[Any] = Field(default_factory=list)


class DatasetFilters(BaseModel):
    tag: str | None = None
    quality: str | None = None
    scene: str | None = None
    workspace_id: int | None = None
    project_id: int | None = None
    keyword: str | None = None


class DatasetQuery(BaseModel):
    filters: DatasetFilters | None = None
    project_id: int | None = None
    scene: str | None = None
    quality: str | None = None
    keyword: str | None = None
    page: int = 1
    size: int = 20


class DatasetCreate(BaseModel):
    workspace_id: int | None = None
    project_id: int | None = None
    name: str = Field(max_length=256)
    qrdf_ids: list[int] = Field(default_factory=list)
    description: str = ""


class DatasetRevisionItemInput(BaseModel):
    """One immutable published Episode or logical time-ranged sample."""

    episode_id: int = Field(gt=0)
    sample_id: str | None = Field(default=None, min_length=1, max_length=128)
    annotation_revision_id: int | None = Field(default=None, gt=0)
    annotation_segment_id: str | None = Field(default=None, min_length=1, max_length=128)
    core_start_ns: int | None = None
    core_end_ns: int | None = None
    effective_start_ns: int | None = None
    effective_end_ns: int | None = None
    pre_roll_s: float = Field(default=0.0, ge=0, le=300)
    post_roll_s: float = Field(default=0.0, ge=0, le=300)
    task: str = Field(default="", max_length=2000)
    outcome: str = Field(default="", max_length=128)


class DatasetRevisionCreate(BaseModel):
    workspace_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=4000)
    filter_json: dict[str, Any] = Field(default_factory=dict)
    items: list[DatasetRevisionItemInput] = Field(default_factory=list)
    request_id: UUID | None = None


class DatasetContainerCreate(BaseModel):
    """Create a named Dataset without implicitly freezing a revision."""

    workspace_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=4000)


class DatasetRevisionForDatasetCreate(BaseModel):
    """Freeze a revision under an existing Dataset container."""

    filter_json: dict[str, Any] = Field(default_factory=dict)
    items: list[DatasetRevisionItemInput] = Field(default_factory=list)
    request_id: UUID | None = None


class DatasetRevisionExportCreate(BaseModel):
    export_profile: Literal["lerobot", "qrdf"] = "lerobot"


class DatasetRevisionCandidateTaskLabel(BaseModel):
    id: int
    name: str


class DatasetRevisionCandidateSource(BaseModel):
    id: int
    episode_uid: str
    modality: str
    duration_s: float
    timeline_available: bool
    timeline_start_ns: str | None = None
    timeline_end_ns: str | None = None
    created_at: str | None = None


class DatasetRevisionCandidateItem(BaseModel):
    episode_id: int
    episode_uid: str
    candidate_kind: Literal["full_source", "derived"]
    kind: Literal["source", "derived"]
    modality: str
    source_start_ns: str | None = None
    source_end_ns: str | None = None
    duration_s: float
    task_label: DatasetRevisionCandidateTaskLabel | None = None
    collector: dict[str, Any] | None = None
    device: dict[str, Any] | None = None
    preview_available: bool
    created_at: str | None = None


class DatasetRevisionCandidateGroup(BaseModel):
    source: DatasetRevisionCandidateSource
    candidates: list[DatasetRevisionCandidateItem]


class DatasetRevisionCandidateSummary(BaseModel):
    candidate_count: int
    source_count: int
    duration_s: float


class DatasetRevisionCandidateList(BaseModel):
    dataset_id: int
    workspace_id: int
    task_set_id: int
    summary: DatasetRevisionCandidateSummary
    groups: list[DatasetRevisionCandidateGroup]


class DatasetRevisionItemOut(BaseModel):
    """One UI-safe Dataset Revision member; export facts stay server-side."""

    position: int
    episode_id: int
    item_kind: Literal["episode", "sample"]
    sample_id: str | None = None
    core_start_ns: int | None = None
    core_end_ns: int | None = None
    effective_start_ns: int | None = None
    effective_end_ns: int | None = None
    task: str = ""
    outcome: str = ""


class DatasetRevisionOut(BaseModel):
    """UI projection of an immutable Dataset Revision, without its export manifest."""

    id: int
    dataset_id: int
    workspace_id: int
    name: str
    version: int
    status: Literal["active", "retired"]
    episode_count: int
    retired_at: str | None = None
    items: list[DatasetRevisionItemOut]


class DatasetRevisionList(BaseModel):
    items: list[DatasetRevisionOut]


class PreprocessBatchRequest(BaseModel):
    """Trigger batch preprocessing (concurrent scheduling via Celery)."""

    task_ids: list[int] = Field(min_length=1)
    max_concurrency: int = Field(default=4, ge=1, le=32)


class McapToQrdfRequest(BaseModel):
    """Pipeline 2: MCAP -> QRDF standardized conversion."""

    mcap_path: str | None = None
    folder_name: str | None = None
    file_name: str | None = None
    task_id: int | None = None
    output_dir: str | None = None
    episode_id: str | None = None
    task_name: str | None = None
    dataset_name: str | None = None
    image_workers: int | None = None
    generate_preview: bool = False
    auto_preprocess: bool = False
    async_mode: bool = False


class QrdfToLerobotRequest(BaseModel):
    """Pipeline 5: QRDF -> LeRobot training data export conversion."""

    storage_path: str | None = None
    qrdf_id: int | None = None
    task_id: int | None = None
    output_dir: str | None = None
    fps: float | None = None
    lerobot_version: str = "v3.0"
    image_size: str | None = None
    async_mode: bool = False
    export_template: str = "generic"


class ExportCreate(BaseModel):
    query_id: str | None = None
    dataset_id: int | None = None
    format: str = "lerobot"
    params: dict[str, Any] = Field(default_factory=dict)


class PackageQuery(BaseModel):
    filters: DatasetFilters = Field(default_factory=DatasetFilters)
    page: int = 1
    size: int = 20


class QrdfQuery(BaseModel):
    project_id: int | None = None
    workspace_id: int | None = None
    scene: str | None = None
    quality: str | None = None
    keyword: str | None = None
    page: int = 1
    size: int = 20


class ConfigUpdate(BaseModel):
    section: str
    data: dict[str, Any]
