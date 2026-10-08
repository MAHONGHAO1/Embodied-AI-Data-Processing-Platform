from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from quictrain_config import ActConfig, Pi05Config, canonical_hash


class DatasetVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    dataset_id: str
    name: str
    version: str
    status: Literal["REGISTERED", "MATERIALIZING", "READY", "FAILED", "DEPRECATED", "INVALID"] = (
        "READY"
    )
    format: str = "lerobot"
    format_version: str = "3.0"
    uri: str
    checksum: str
    materialized_uri: str | None = None
    repo_id: str | None = None
    revision: str | None = None
    episodes: int | None = Field(default=None, ge=0)
    frames: int | None = Field(default=None, ge=0)
    duration_hours: float | None = Field(default=None, ge=0)
    fps: float | None = Field(default=None, gt=0)
    robot_type: str | None = None
    camera_keys: list[str] | None = None
    action_dim: int | None = Field(default=None, ge=0)
    state_dim: int | None = Field(default=None, ge=0)
    language_tasks: bool | None = None


def missing_dataset_metadata(dataset: Any) -> list[str]:
    return [
        field
        for field in ("episodes", "frames", "fps", "camera_keys", "action_dim", "state_dim")
        if getattr(dataset, field) is None
    ]


class ResourceProfile(BaseModel):
    id: str
    name: str
    gpu_models: list[str]
    gpu_count: int
    vram_gb_min: int
    selectable: bool = True
    calibration_status: Literal["BENCHMARKED", "UNVERIFIED", "UNSUPPORTED"]
    warning: str | None = None
    # V1.1 hybrid: bind profile to a compute provider / pool.
    provider_id: Literal["fake", "aliyun_dlc"] = "fake"
    pool_id: str | None = None


class RecipeSpec(BaseModel):
    id: str
    name: str
    config_model: str
    approval_required: bool = False
    resource_profiles: list[ResourceProfile]


class ModelSpec(BaseModel):
    id: str
    version_id: str
    name: str
    version: str
    backend: Literal["lerobot"] = "lerobot"
    maturity: Literal["stable", "beta", "experimental", "internal"]
    description: str
    upstream_repo: str
    upstream_ref: str
    adapter_version: str
    image_uri: str
    image_digest: str
    supported_format_versions: list[str] = Field(default_factory=lambda: ["3.0"])
    min_cameras: int = 1
    max_cameras: int = 4
    max_action_dim: int = 128
    recipes: list[RecipeSpec]
    standard_metrics: dict[str, str]
    selectable: bool = True
    availability_message: str | None = None

    @property
    def schema(self) -> dict[str, Any]:
        models = {"act": ActConfig, "pi05": Pi05Config}
        return models[self.id].model_json_schema()

    @property
    def schema_hash(self) -> str:
        return canonical_hash(self.schema)


UPSTREAM_REPO = "https://github.com/huggingface/lerobot"
UPSTREAM_REF = "1396b9fab7aecddd10006c33c47a487ffdcb54b4"
LEROBOT_RUNTIME_IMAGE_URI = (
    "quic-robot-registry-vpc.cn-beijing.cr.aliyuncs.com/quicrobot/quictrain-lerobot-runtime"
)
LEROBOT_RUNTIME_IMAGE_DIGEST = (
    "sha256:385f4e8b44269ebb01b71408087beb4709a09c47988859ffce2089e80b922b08"
)


MODEL_REGISTRY: dict[str, ModelSpec] = {
    "act": ModelSpec(
        id="act",
        version_id="mv_act_20260716",
        name="ACT",
        version="1.0.0",
        maturity="stable",
        description="轻量、稳定的双臂模仿学习基线，也是平台 Canary。",
        upstream_repo=UPSTREAM_REPO,
        upstream_ref=UPSTREAM_REF,
        adapter_version="0.3.0",
        image_uri=LEROBOT_RUNTIME_IMAGE_URI,
        image_digest=LEROBOT_RUNTIME_IMAGE_DIGEST,
        max_action_dim=64,
        recipes=[
            RecipeSpec(
                id="fine_tune",
                name="Fine-tune",
                config_model="quictrain_config.ActConfig",
                resource_profiles=[
                    ResourceProfile(
                        id="act-local-sim",
                        name="本地模拟",
                        gpu_models=["LOCAL"],
                        gpu_count=1,
                        vram_gb_min=0,
                        calibration_status="BENCHMARKED",
                        warning="FakeProvider / 本地开发：不调度真实 GPU 与云端存储。",
                        provider_id="fake",
                        pool_id="local-sim",
                    ),
                    # Prefer DLC: 4090 (OSS+UserVpc) and H20 (CPFS). DSW is ops/interactive only.
                    ResourceProfile(
                        id="act-4090-standard",
                        name="4090 ×1（推荐 · DLC）",
                        gpu_models=["RTX 4090"],
                        gpu_count=1,
                        vram_gb_min=48,
                        selectable=True,
                        calibration_status="UNVERIFIED",
                        warning="推荐。PAI-DLC → Quota4090_48G（quota16ktslmgp8r）。DSW Nv4093/Nv4094 Running 会占满同配额。",
                        provider_id="aliyun_dlc",
                        pool_id="cn-beijing-4090",
                    ),
                    ResourceProfile(
                        id="act-4090-x2",
                        name="4090 ×2",
                        gpu_models=["RTX 4090"],
                        gpu_count=2,
                        vram_gb_min=48,
                        selectable=True,
                        calibration_status="UNVERIFIED",
                        warning="PAI-DLC · Quota4090 多卡（2）。",
                        provider_id="aliyun_dlc",
                        pool_id="cn-beijing-4090",
                    ),
                    ResourceProfile(
                        id="act-4090-x4",
                        name="4090 ×4",
                        gpu_models=["RTX 4090"],
                        gpu_count=4,
                        vram_gb_min=48,
                        selectable=True,
                        calibration_status="UNVERIFIED",
                        warning="PAI-DLC · Quota4090 多卡（4）。",
                        provider_id="aliyun_dlc",
                        pool_id="cn-beijing-4090",
                    ),
                    ResourceProfile(
                        id="act-4090-x8",
                        name="4090 ×8",
                        gpu_models=["RTX 4090"],
                        gpu_count=8,
                        vram_gb_min=48,
                        selectable=False,
                        calibration_status="UNSUPPORTED",
                        warning="8 卡暂不开放；请使用 ×1/×2/×4。",
                        provider_id="aliyun_dlc",
                        pool_id="cn-beijing-4090",
                    ),
                    # H20 is the working CPFS path (Lingjun direct bmcpfs://).
                    ResourceProfile(
                        id="act-h20-standard",
                        name="H20 ×1（DLC）",
                        gpu_models=["H20"],
                        gpu_count=1,
                        vram_gb_min=40,
                        selectable=True,
                        calibration_status="UNVERIFIED",
                        warning="PAI-DLC → H20-5（quota1lnd98qodrh）。与 DSW quic-train-5000 同配额。",
                        provider_id="aliyun_dlc",
                        pool_id="cn-beijing-h20-8",
                    ),
                    ResourceProfile(
                        id="act-h20-x2",
                        name="H20 ×2",
                        gpu_models=["H20"],
                        gpu_count=2,
                        vram_gb_min=40,
                        selectable=True,
                        calibration_status="UNVERIFIED",
                        warning="PAI-DLC · H20-5 多卡（2）。",
                        provider_id="aliyun_dlc",
                        pool_id="cn-beijing-h20-8",
                    ),
                    ResourceProfile(
                        id="act-h20-x4",
                        name="H20 ×4",
                        gpu_models=["H20"],
                        gpu_count=4,
                        vram_gb_min=40,
                        selectable=True,
                        calibration_status="UNVERIFIED",
                        warning="PAI-DLC · H20-5 多卡（4）。",
                        provider_id="aliyun_dlc",
                        pool_id="cn-beijing-h20-8",
                    ),
                    ResourceProfile(
                        id="act-h20-x8",
                        name="H20 ×8",
                        gpu_models=["H20"],
                        gpu_count=8,
                        vram_gb_min=40,
                        selectable=False,
                        calibration_status="UNSUPPORTED",
                        warning="8 卡暂不开放；请使用 ×1/×2/×4。",
                        provider_id="aliyun_dlc",
                        pool_id="cn-beijing-h20-8",
                    ),
                ],
            )
        ],
        standard_metrics={
            "loss": "train/loss",
            "learning_rate": "train/lr",
            "throughput": "train/samples_per_second",
        },
    ),
    "pi05": ModelSpec(
        id="pi05",
        version_id="mv_pi05_20260717_cpfs",
        name="π0.5",
        version="1.0.0",
        maturity="beta",
        description="LeRobot 内置 π0.5 VLA 微调，支持动作专家训练。",
        selectable=True,
        upstream_repo=UPSTREAM_REPO,
        upstream_ref=UPSTREAM_REF,
        adapter_version="0.3.0",
        image_uri=LEROBOT_RUNTIME_IMAGE_URI,
        image_digest=LEROBOT_RUNTIME_IMAGE_DIGEST,
        recipes=[
            RecipeSpec(
                id="fine_tune",
                name="Fine-tune",
                config_model="quictrain_config.Pi05Config",
                resource_profiles=[
                    ResourceProfile(
                        id="pi05-local-sim",
                        name="本地模拟",
                        gpu_models=["LOCAL"],
                        gpu_count=1,
                        vram_gb_min=0,
                        calibration_status="BENCHMARKED",
                        warning="FakeProvider / 本地开发：不调度真实 GPU 与云端存储。",
                        provider_id="fake",
                        pool_id="local-sim",
                    ),
                    ResourceProfile(
                        id="pi05-4090-standard",
                        name="4090 ×1（推荐 · DLC）",
                        gpu_models=["RTX 4090"],
                        gpu_count=1,
                        vram_gb_min=48,
                        selectable=True,
                        calibration_status="UNVERIFIED",
                        warning="推荐。PAI-DLC → Quota4090_48G；π0.5 请求 Memory=128Gi（避免 Creating policy SIGKILL）。",
                        provider_id="aliyun_dlc",
                        pool_id="cn-beijing-4090",
                    ),
                    ResourceProfile(
                        id="pi05-4090-x2",
                        name="4090 ×2",
                        gpu_models=["RTX 4090"],
                        gpu_count=2,
                        vram_gb_min=48,
                        selectable=True,
                        calibration_status="UNVERIFIED",
                        warning="PAI-DLC · Quota4090 多卡（2）。",
                        provider_id="aliyun_dlc",
                        pool_id="cn-beijing-4090",
                    ),
                    ResourceProfile(
                        id="pi05-4090-x4",
                        name="4090 ×4",
                        gpu_models=["RTX 4090"],
                        gpu_count=4,
                        vram_gb_min=48,
                        selectable=True,
                        calibration_status="UNVERIFIED",
                        warning="PAI-DLC · Quota4090 多卡（4）。",
                        provider_id="aliyun_dlc",
                        pool_id="cn-beijing-4090",
                    ),
                    ResourceProfile(
                        id="pi05-4090-x8",
                        name="4090 ×8",
                        gpu_models=["RTX 4090"],
                        gpu_count=8,
                        vram_gb_min=48,
                        selectable=False,
                        calibration_status="UNSUPPORTED",
                        warning="8 卡暂不开放；请使用 ×1/×2/×4。",
                        provider_id="aliyun_dlc",
                        pool_id="cn-beijing-4090",
                    ),
                    # Legacy id kept for catalog continuity; now selectable 4090×1 alias.
                    ResourceProfile(
                        id="pi05-4090-debug",
                        name="4090 ×1（兼容）",
                        gpu_models=["RTX 4090"],
                        gpu_count=1,
                        vram_gb_min=48,
                        selectable=False,
                        calibration_status="UNVERIFIED",
                        warning="已由 pi05-4090-standard 替代；勿再选用。",
                        provider_id="aliyun_dlc",
                        pool_id="cn-beijing-4090",
                    ),
                    ResourceProfile(
                        id="pi05-h20-standard",
                        name="H20 ×1（DLC）",
                        gpu_models=["H20"],
                        gpu_count=1,
                        vram_gb_min=40,
                        selectable=True,
                        calibration_status="BENCHMARKED",
                        warning="PAI-DLC → H20-5（quota1lnd98qodrh）。",
                        provider_id="aliyun_dlc",
                        pool_id="cn-beijing-h20-8",
                    ),
                    ResourceProfile(
                        id="pi05-h20-x2",
                        name="H20 ×2",
                        gpu_models=["H20"],
                        gpu_count=2,
                        vram_gb_min=40,
                        selectable=True,
                        calibration_status="UNVERIFIED",
                        warning="PAI-DLC · H20-5 多卡（2）。",
                        provider_id="aliyun_dlc",
                        pool_id="cn-beijing-h20-8",
                    ),
                    ResourceProfile(
                        id="pi05-h20-x4",
                        name="H20 ×4",
                        gpu_models=["H20"],
                        gpu_count=4,
                        vram_gb_min=40,
                        selectable=True,
                        calibration_status="UNVERIFIED",
                        warning="PAI-DLC · H20-5 多卡（4）。",
                        provider_id="aliyun_dlc",
                        pool_id="cn-beijing-h20-8",
                    ),
                    ResourceProfile(
                        id="pi05-h20-x8",
                        name="H20 ×8",
                        gpu_models=["H20"],
                        gpu_count=8,
                        vram_gb_min=40,
                        selectable=False,
                        calibration_status="UNSUPPORTED",
                        warning="8 卡暂不开放；请使用 ×1/×2/×4。",
                        provider_id="aliyun_dlc",
                        pool_id="cn-beijing-h20-8",
                    ),
                ],
            )
        ],
        standard_metrics={
            "loss": "train/loss",
            "learning_rate": "train/lr",
            "gradient_norm": "train/grad_norm",
        },
    ),
}


def get_model(model_id_or_version_id: str) -> ModelSpec:
    if model_id_or_version_id in MODEL_REGISTRY:
        return MODEL_REGISTRY[model_id_or_version_id]
    for model in MODEL_REGISTRY.values():
        if model.version_id == model_id_or_version_id:
            return model
    raise KeyError(model_id_or_version_id)


def compatibility_issues(dataset: DatasetVersion, model: ModelSpec) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    missing = missing_dataset_metadata(dataset)
    if missing:
        issues.append(
            {
                "severity": "BLOCKER",
                "code": "DATASET_METADATA_REQUIRED",
                "field": missing[0],
                "message": "数据版本的训练元数据尚未验证。",
                "remediation": "提供实际数据集的帧数、采样率、相机与动作/状态字段。",
            }
        )
    if dataset.status != "READY":
        issues.append(
            {
                "severity": "BLOCKER",
                "code": "DATASET_NOT_READY",
                "field": "status",
                "message": "数据版本不是 READY 状态。",
                "remediation": "返回数据平台修复并发布新版本。",
            }
        )
    if dataset.format != "lerobot" or dataset.format_version not in model.supported_format_versions:
        issues.append(
            {
                "severity": "BLOCKER",
                "code": "FORMAT_UNSUPPORTED",
                "field": "format_version",
                "message": f"{model.name} 不支持 {dataset.format} {dataset.format_version}。",
                "remediation": "输出 LeRobot 3.0 不可变版本。",
            }
        )
    if dataset.camera_keys is not None and not (
        model.min_cameras <= len(dataset.camera_keys) <= model.max_cameras
    ):
        issues.append(
            {
                "severity": "BLOCKER",
                "code": "CAMERA_COUNT_MISMATCH",
                "field": "camera_keys",
                "message": f"需要 {model.min_cameras}-{model.max_cameras} 路相机。",
                "remediation": "调整数据字段或选择其他模型。",
            }
        )
    for field in ("action_dim", "state_dim"):
        if getattr(dataset, field) == 0:
            issues.append(
                {
                    "severity": "BLOCKER",
                    "code": "POLICY_FEATURE_MISSING",
                    "field": field,
                    "message": f"{model.name} 需要真实的动作和状态字段；数据集缺少 {field}。",
                    "remediation": "选择含动作/状态观测的数据集，不可为视频样本伪造字段。",
                }
            )
    if dataset.action_dim is not None and dataset.action_dim > model.max_action_dim:
        issues.append(
            {
                "severity": "BLOCKER",
                "code": "ACTION_DIM_TOO_LARGE",
                "field": "action_dim",
                "message": f"动作维度 {dataset.action_dim} 超过上限 {model.max_action_dim}。",
                "remediation": "提供明确的动作字段映射。",
            }
        )
    return issues
