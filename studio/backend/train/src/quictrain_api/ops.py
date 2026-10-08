"""Project quota and provider disable checks (V1 ops floor)."""

from __future__ import annotations

from quictrain_core import JobState
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from quictrain_api.capacity import capacity_snapshot
from quictrain_api.db import JobRecord, ProjectPolicyRecord, ResourceProfileRecord
from quictrain_api.errors import ServiceError
from quictrain_api.settings import get_settings

ACTIVE_COUNTS = {
    JobState.QUEUED.value,
    JobState.SUBMITTING.value,
    JobState.PROVISIONING.value,
    JobState.RUNNING.value,
    JobState.ORPHANED.value,
    JobState.CANCEL_REQUESTED.value,
}


def get_or_create_policy(session: Session, project_id: str) -> ProjectPolicyRecord:
    settings = get_settings()
    policy = session.get(ProjectPolicyRecord, project_id)
    if policy is None:
        policy = ProjectPolicyRecord(
            project_id=project_id,
            max_concurrent_jobs=settings.default_max_concurrent_jobs,
            max_gpus=settings.default_max_gpus,
            max_runtime_seconds=settings.default_max_runtime_seconds,
            provider_disabled=False,
        )
        session.add(policy)
        session.flush()
    return policy


def enforce_submission_limits(
    session: Session,
    *,
    project_id: str,
    resource_profile_id: str,
) -> None:
    settings = get_settings()
    if settings.provider_disabled:
        raise ServiceError(
            "PROVIDER_DISABLED",
            "训练 Provider 已被紧急禁用。",
            status_code=503,
            retryable=True,
        )
    policy = get_or_create_policy(session, project_id)
    if policy.provider_disabled:
        raise ServiceError(
            "PROVIDER_DISABLED",
            "该项目的训练 Provider 已禁用。",
            status_code=503,
            retryable=True,
        )

    active = (
        session.scalar(
            select(func.count())
            .select_from(JobRecord)
            .where(JobRecord.project_id == project_id, JobRecord.state.in_(ACTIVE_COUNTS))
        )
        or 0
    )
    if active >= policy.max_concurrent_jobs:
        raise ServiceError(
            "PROJECT_JOB_QUOTA_EXCEEDED",
            (
                f"项目并发任务已达上限 {policy.max_concurrent_jobs}"
                f"（当前活跃 {active}：排队/提交/准备/运行中均计入）。"
                "请等待任务结束，或取消卡住的任务后再提交；管理员可调高项目 policy。"
            ),
            status_code=429,
            details={
                "active": active,
                "limit": policy.max_concurrent_jobs,
                "active_states": sorted(ACTIVE_COUNTS),
            },
            retryable=True,
        )

    profile = session.get(ResourceProfileRecord, resource_profile_id)
    gpu_count = profile.gpu_count if profile is not None else 1
    active_gpus = 0
    for job in session.scalars(
        select(JobRecord).where(
            JobRecord.project_id == project_id, JobRecord.state.in_(ACTIVE_COUNTS)
        )
    ):
        job_profile = session.get(ResourceProfileRecord, job.resource_profile_id)
        active_gpus += job_profile.gpu_count if job_profile is not None else 1
    if active_gpus + gpu_count > policy.max_gpus:
        raise ServiceError(
            "PROJECT_GPU_QUOTA_EXCEEDED",
            f"项目 GPU 配额不足（上限 {policy.max_gpus}）。",
            status_code=429,
            details={
                "active_gpus": active_gpus,
                "requested": gpu_count,
                "limit": policy.max_gpus,
            },
            retryable=True,
        )


def max_runtime_seconds_for_job(session: Session, project_id: str) -> int:
    policy = get_or_create_policy(session, project_id)
    return policy.max_runtime_seconds


def idle_resource_stop_policy() -> dict[str, object]:
    """Guardrails for stopping idle DSW / resource groups.

    QuicTrain never auto-stops resource groups by default. H20-5 resource group
    and H20 DSW instances are additionally hard-protected. Any future
    StopInstance / resource-group close path MUST call ``may_stop_idle_resource``.
    """

    settings = get_settings()
    return {
        "auto_stop_idle_resources": settings.auto_stop_idle_resources,
        "auto_stop_resource_groups": settings.auto_stop_resource_groups,
        "idle_minutes_before_stop": settings.idle_minutes_before_stop,
        "protect_h20_dsw": settings.protect_h20_dsw,
        "protect_h20_resource_groups": settings.protect_h20_resource_groups,
        "allow_h20_dsw_stop": settings.allow_h20_dsw_stop,
        "dsw_stop_protected_families": [
            item.strip() for item in settings.dsw_stop_protected_families.split(",") if item.strip()
        ],
        "dsw_stop_protected_resource_ids": [
            item.strip()
            for item in settings.dsw_stop_protected_resource_ids.split(",")
            if item.strip()
        ],
        "dedicated_dsw_instance_id": settings.dedicated_dsw_instance_id,
        "dedicated_dsw_name": settings.dedicated_dsw_name,
        "default_behavior": "never_auto_stop",
        "h20_policy": "never_stop_h20_dsw_or_resource_group_by_default",
    }


def may_stop_idle_resource(
    *,
    idle_minutes: float,
    resource_group: bool = False,
    family: str | None = None,
    resource_id: str | None = None,
) -> tuple[bool, str]:
    """Return whether an idle cloud resource may be stopped under current policy."""

    settings = get_settings()
    family_l = (family or "").strip().lower()
    resource = (resource_id or "").strip()
    protected_families = {
        item.strip().lower()
        for item in settings.dsw_stop_protected_families.split(",")
        if item.strip()
    }
    protected_quotas = {
        item.strip() for item in settings.dsw_stop_protected_resource_ids.split(",") if item.strip()
    }
    if settings.protect_h20_resource_groups and (
        resource_group or family_l == "h20" or resource in protected_quotas
    ):
        return False, "H20 resource groups / quotas are protected and must not be closed"
    if settings.protect_h20_dsw and (
        family_l in protected_families or resource in protected_quotas
    ):
        return False, "H20 DSW instances are protected and must not be auto-stopped"
    if resource_group and not settings.auto_stop_resource_groups:
        return False, "resource groups are never auto-stopped by default"
    if not settings.auto_stop_idle_resources:
        return False, "auto-stop of idle resources is disabled (default)"
    required = max(0, int(settings.idle_minutes_before_stop))
    if idle_minutes < required:
        return (
            False,
            f"idle {idle_minutes:.1f}m < required {required}m before stop is allowed",
        )
    return True, "idle threshold met and auto-stop enabled"


__all__ = [
    "capacity_snapshot",
    "enforce_submission_limits",
    "get_or_create_policy",
    "idle_resource_stop_policy",
    "max_runtime_seconds_for_job",
    "may_stop_idle_resource",
]
