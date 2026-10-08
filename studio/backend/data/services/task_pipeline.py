"""Server-owned intake and navigation identities for Task source pipelines."""

from __future__ import annotations

from dataclasses import dataclass

from data.database import Task


@dataclass(frozen=True)
class TaskPipeline:
    key: str
    label: str
    task_type: str
    data_source: str
    intake_route: str
    task_route_template: str
    generic_create_allowed: bool


PIPELINES: dict[str, TaskPipeline] = {
    "ego_staged_v1": TaskPipeline(
        "ego_staged_v1",
        "EGO 采集",
        "ego",
        "ego",
        "/data-import",
        "/tasks/{task_id}",
        True,
    ),
    "teleop_v1": TaskPipeline(
        "teleop_v1",
        "遥操作 QRDF",
        "collect",
        "teleop",
        "/data-import",
        "/tasks/{task_id}",
        True,
    ),
    "qrdf_generic_v1": TaskPipeline(
        "qrdf_generic_v1",
        "标准 QRDF",
        "collect",
        "qrdf",
        "/data-import",
        "/tasks/{task_id}",
        True,
    ),
}

_TELEOP_DATA_SOURCES = {"teleop", "teleoperation", "umi", "umi_teleop"}


def public_pipeline_items() -> list[dict[str, object]]:
    return [
        {
            "key": pipeline.key,
            "label": pipeline.label,
            "intake_route": pipeline.intake_route,
            "task_route_template": pipeline.task_route_template,
            "generic_create_allowed": pipeline.generic_create_allowed,
        }
        for pipeline in PIPELINES.values()
    ]


def resolve_pipeline(key: str) -> TaskPipeline | None:
    return PIPELINES.get(str(key or "").strip())


def pipeline_key_for_task(task: Task) -> str:
    metadata = task.metadata_json if isinstance(task.metadata_json, dict) else {}
    metadata_key = str(metadata.get("pipeline_key") or "").strip()
    if metadata_key in PIPELINES:
        return metadata_key
    if task.task_type == "ego":
        return "ego_staged_v1"
    if str(task.data_source or "").strip().lower() in _TELEOP_DATA_SOURCES:
        return "teleop_v1"
    return "legacy_v1"
