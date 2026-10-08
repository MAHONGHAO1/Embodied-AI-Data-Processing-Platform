"""Normalized resource identity and stable conflict contracts."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError


@dataclass(frozen=True)
class ResourceConflict:
    code: str
    message: str


WORKSPACE_NAME_CONFLICT = ResourceConflict(
    "workspace_name_exists",
    "数采工作空间名称不可重复",
)
TASK_SET_NAME_CONFLICT = ResourceConflict(
    "task_set_name_exists",
    "相同数采工作空间下任务集名称不可重复",
)
DATASET_NAME_CONFLICT = ResourceConflict(
    "dataset_name_exists",
    "相同数采工作空间下数据集名称不可重复",
)
COLLECTOR_NAME_CONFLICT = ResourceConflict(
    "collector_name_exists",
    "该数采工作空间内采集员名称重复，请重新输入",
)
DEVICE_SERIAL_CONFLICT = ResourceConflict(
    "device_serial_exists",
    "设备SN号重复请重新输入",
)


def normalized_name(value: str, *, maximum: int = 128) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("resource name is required")
    if len(name) > maximum:
        raise ValueError("resource name is too long")
    return name


def normalized_serial(value: str, *, maximum: int = 128) -> str:
    return normalized_name(value, maximum=maximum).upper()


def normalized_key(value: str) -> str:
    return str(value or "").strip().lower()


def conflict_detail(conflict: ResourceConflict) -> dict[str, str]:
    return {"code": conflict.code, "message": conflict.message}


def is_constraint_conflict(exc: IntegrityError, constraint_name: str) -> bool:
    """Return true only when PostgreSQL identifies the expected constraint."""
    diagnostic = getattr(getattr(exc, "orig", None), "diag", None)
    actual = getattr(diagnostic, "constraint_name", None)
    if actual:
        return actual == constraint_name
    return constraint_name in str(getattr(exc, "orig", exc))


def allocate_legacy_names(
    rows: Iterable[tuple[int, str]],
    *,
    maximum: int = 128,
) -> dict[int, str]:
    """Allocate deterministic display names without stealing existing suffixes."""
    ordered = sorted(
        ((int(row_id), normalized_name(name, maximum=maximum)) for row_id, name in rows)
    )
    first_by_key: dict[str, tuple[int, str]] = {}
    for row_id, name in ordered:
        first_by_key.setdefault(normalized_key(name), (row_id, name))

    reserved = set(first_by_key)
    allocated: dict[int, str] = {}
    next_suffix: dict[str, int] = {}
    for row_id, name in ordered:
        key = normalized_key(name)
        first_id, canonical_name = first_by_key[key]
        if row_id == first_id:
            allocated[row_id] = name
            continue
        suffix = next_suffix.get(key, 2)
        while True:
            marker = f"_{suffix}"
            candidate = f"{canonical_name[: maximum - len(marker)]}{marker}"
            candidate_key = normalized_key(candidate)
            suffix += 1
            if candidate_key in reserved:
                continue
            reserved.add(candidate_key)
            next_suffix[key] = suffix
            allocated[row_id] = candidate
            break
    return allocated
