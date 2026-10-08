from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def ui_field(
    default: Any,
    *,
    title: str,
    description: str,
    group: str,
    order: int,
    level: int = 0,
    editable_by: tuple[str, ...] = ("user", "developer", "admin"),
    ge: float | None = None,
    le: float | None = None,
) -> Any:
    return Field(
        default=default,
        title=title,
        description=description,
        ge=ge,
        le=le,
        json_schema_extra={
            "x-quic": {
                "group": group,
                "order": order,
                "level": level,
                "visibility": "advanced" if level == 1 else "basic",
                "editable": level < 2,
                "editable_by": list(editable_by),
            }
        },
    )


class TrainingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: int = ui_field(
        30000,
        title="训练步数",
        description="本次训练的优化器更新步数",
        group="training",
        order=10,
        ge=1,
        le=5_000_000,
    )
    batch_size: int = ui_field(
        4,
        title="批大小",
        description="每张卡的训练批大小",
        group="training",
        order=20,
        ge=1,
        le=512,
    )
    seed: int = ui_field(
        42,
        title="随机种子",
        description="用于保证可复现性",
        group="training",
        order=30,
        level=1,
        ge=0,
        le=2**31 - 1,
    )
    precision: Literal["bf16", "fp16", "fp32"] = ui_field(
        "bf16",
        title="计算精度",
        description="训练计算精度",
        group="training",
        order=40,
        level=1,
    )
    gradient_checkpointing: bool = ui_field(
        True,
        title="梯度检查点",
        description="以额外计算换取显存空间",
        group="training",
        order=50,
        level=1,
    )
    num_workers: int = ui_field(
        0,
        title="数据加载进程",
        description="数据加载子进程数；DLC 的 OSS 挂载默认使用 0 以提高兼容性",
        group="training",
        order=60,
        level=1,
        ge=0,
        le=64,
    )


class OptimizerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    learning_rate: float = ui_field(
        5e-5,
        title="学习率",
        description="优化器基础学习率",
        group="optimizer",
        order=10,
        ge=1e-8,
        le=1.0,
    )
    weight_decay: float = ui_field(
        0.01,
        title="权重衰减",
        description="AdamW 权重衰减",
        group="optimizer",
        order=20,
        level=1,
        ge=0,
        le=1,
    )
    warmup_steps: int = ui_field(
        500,
        title="预热步数",
        description="学习率预热步数",
        group="optimizer",
        order=30,
        level=1,
        ge=0,
        le=1_000_000,
    )


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_uri: str = Field(
        "oss://quictrain-artifacts/system-assigned",
        json_schema_extra={
            "x-quic": {
                "group": "runtime",
                "order": 10,
                "level": 2,
                "visibility": "system",
                "editable": False,
                "editable_by": ["system"],
            }
        },
    )


class BaseTrainConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    training: TrainingConfig = Field(default_factory=TrainingConfig)
    optimizer: OptimizerConfig = Field(default_factory=OptimizerConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)

    @model_validator(mode="after")
    def validate_warmup(self) -> BaseTrainConfig:
        if self.optimizer.warmup_steps >= self.training.steps:
            raise ValueError("warmup_steps 必须小于训练步数")
        return self


class ActConfig(BaseTrainConfig):
    chunk_size: int = ui_field(
        100,
        title="动作块长度",
        description="ACT 一次预测的动作步数",
        group="policy",
        order=10,
        ge=1,
        le=1000,
    )
    pretrained_backbone_weights: Literal["ResNet18_Weights.IMAGENET1K_V1"] | None = ui_field(
        None,
        title="视觉骨干预训练权重",
        description="默认不联网下载；仅在权重已缓存或训练网络可访问时启用",
        group="policy",
        order=20,
        level=1,
    )


class Pi05Config(BaseTrainConfig):
    # H20 acceptance used batch_size=1 with gradient checkpointing + action expert only.
    training: TrainingConfig = Field(default_factory=lambda: TrainingConfig(batch_size=1))
    pretrained_path: str = ui_field(
        "lerobot/pi05_base",
        title="预训练权重",
        description="本地/Fake 可用 Hub id；生产 Scheduler 会注入 CPFS 上的已验收 π0.5 权重目录",
        group="policy",
        order=5,
    )
    action_expert_only: bool = ui_field(
        True,
        title="仅训练动作专家",
        description="减少显存占用并保留视觉语言主干（H20 验收默认）",
        group="policy",
        order=10,
    )
    max_action_dim: int = ui_field(
        32,
        title="最大动作维度",
        description="动作映射后的最大维度",
        group="policy",
        order=20,
        level=1,
        ge=1,
        le=128,
    )


CONFIG_MODELS: dict[str, type[BaseTrainConfig]] = {
    "act": ActConfig,
    "pi05": Pi05Config,
}


def canonical_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(payload.encode()).hexdigest()}"


def _set_path(target: dict[str, Any], path: str, value: Any) -> None:
    cursor = target
    parts = path.split(".")
    for part in parts[:-1]:
        if part not in cursor or not isinstance(cursor[part], dict):
            raise KeyError(path)
        cursor = cursor[part]
    if parts[-1] not in cursor:
        raise KeyError(path)
    cursor[parts[-1]] = value


def _field_metadata(model: type[BaseModel], path: str) -> dict[str, Any]:
    current: type[BaseModel] = model
    parts = path.split(".")
    for index, part in enumerate(parts):
        field = current.model_fields.get(part)
        if field is None:
            raise KeyError(path)
        if index == len(parts) - 1:
            return (field.json_schema_extra or {}).get("x-quic", {})
        annotation = field.annotation
        if not isinstance(annotation, type) or not issubclass(annotation, BaseModel):
            raise KeyError(path)
        current = annotation
    raise KeyError(path)


def resolve_config(
    model_id: str,
    overrides: dict[str, Any],
    *,
    role: str = "user",
    preset: dict[str, Any] | None = None,
    runtime_injection: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    model = CONFIG_MODELS[model_id]
    defaults = model().model_dump(mode="json")
    resolved = deepcopy(defaults)

    for source in (preset or {}, overrides):
        for path, value in source.items():
            meta = _field_metadata(model, path)
            if source is overrides and (
                not meta.get("editable", True) or role not in meta.get("editable_by", [])
            ):
                raise PermissionError(path)
            _set_path(resolved, path, value)

    for path, value in (runtime_injection or {}).items():
        _set_path(resolved, path, value)

    # The published default is appropriate for normal training jobs, but it
    # becomes invalid for short smoke-test runs. Keep explicit user/preset/
    # runtime values strict; only make the implicit default compatible.
    explicit_paths = set(preset or {}) | set(overrides) | set(runtime_injection or {})
    if "optimizer.warmup_steps" not in explicit_paths:
        steps = int(_read_path(resolved, "training.steps"))
        warmup_steps = int(_read_path(resolved, "optimizer.warmup_steps"))
        if warmup_steps >= steps:
            _set_path(
                resolved,
                "optimizer.warmup_steps",
                min(warmup_steps, max(0, steps // 10)),
            )

    validated = model.model_validate(resolved).model_dump(mode="json")
    diff = [
        {"path": path, "default": _read_path(defaults, path), "value": value}
        for path, value in overrides.items()
    ]
    return validated, diff


def _read_path(value: dict[str, Any], path: str) -> Any:
    cursor: Any = value
    for part in path.split("."):
        cursor = cursor[part]
    return cursor
