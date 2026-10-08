"""Workspace-scoped collection dashboard aggregation over package facts."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import case, exists, func, literal, select
from sqlalchemy.orm import Session

from data.database import CollectionDevice, PersonnelProfile
from data.models.collection_config import CollectionLabel
from data.models.collection_core import CollectionProject, CollectionTask, CollectionTaskLabel
from data.models.data_package import DataPackage

GRANULARITIES = ("hour", "day", "month")
BASES = ("all", "valid")
MAX_RANGE_DAYS = 366
MAX_HOUR_RANGE_DAYS = 7
DEFAULT_RANGE_DAYS = 30
_BUCKET_FORMATS = {"hour": "%Y-%m-%d %H:00", "day": "%Y-%m-%d", "month": "%Y-%m"}


class DashboardFilterError(ValueError):
    """Raised for unsupported or out-of-scope dashboard filters."""


@dataclass(frozen=True)
class DashboardFilters:
    workspace_id: int
    start_date: date
    end_date: date
    project_ids: tuple[int, ...]
    task_ids: tuple[int, ...]
    scene_label_ids: tuple[int, ...]
    purpose_label_ids: tuple[int, ...]
    granularity: str
    basis: str
    tz: str

    @property
    def has_task_filters(self) -> bool:
        return bool(self.task_ids or self.scene_label_ids or self.purpose_label_ids)


def _ids(values) -> tuple[int, ...]:
    try:
        return tuple(sorted({int(value) for value in values or ()}))
    except (TypeError, ValueError) as exc:
        raise DashboardFilterError("filter ids must be integers") from exc


def resolve_filters(
    db: Session,
    *,
    workspace_id: int,
    start_date: date | None = None,
    end_date: date | None = None,
    project_ids=(),
    task_ids=(),
    scene_label_ids=(),
    purpose_label_ids=(),
    granularity: str = "day",
    basis: str = "valid",
    tz: str = "Asia/Shanghai",
    now: datetime | None = None,
) -> DashboardFilters:
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise DashboardFilterError(f"unknown tz: {tz}") from exc
    if granularity not in GRANULARITIES:
        raise DashboardFilterError(f"granularity must be one of {', '.join(GRANULARITIES)}")
    if basis not in BASES:
        raise DashboardFilterError(f"basis must be one of {', '.join(BASES)}")
    today = (now or datetime.now(timezone.utc)).astimezone(zone).date()
    end = end_date or today
    start = start_date or end - timedelta(days=DEFAULT_RANGE_DAYS - 1)
    if start > end:
        raise DashboardFilterError("start_date must not be after end_date")
    span = (end - start).days + 1
    if span > MAX_RANGE_DAYS:
        raise DashboardFilterError(f"date range must not exceed {MAX_RANGE_DAYS} days")
    if granularity == "hour" and span > MAX_HOUR_RANGE_DAYS:
        raise DashboardFilterError(f"hour granularity allows at most {MAX_HOUR_RANGE_DAYS} days")
    filters = DashboardFilters(
        int(workspace_id),
        start,
        end,
        _ids(project_ids),
        _ids(task_ids),
        _ids(scene_label_ids),
        _ids(purpose_label_ids),
        granularity,
        basis,
        tz,
    )
    _validate_scope(db, filters)
    return filters


def _validate_scope(db: Session, filters: DashboardFilters) -> None:
    def owned(model, ids) -> set[int]:
        if not ids:
            return set()
        return set(
            db.scalars(
                select(model.id).where(
                    model.id.in_(ids), model.workspace_id == filters.workspace_id
                )
            )
        )

    if set(filters.project_ids) - owned(CollectionProject, filters.project_ids):
        raise DashboardFilterError("project_ids must belong to the workspace")
    if set(filters.task_ids) - owned(CollectionTask, filters.task_ids):
        raise DashboardFilterError("task_ids must belong to the workspace")
    for category, ids in (
        ("scene", filters.scene_label_ids),
        ("purpose", filters.purpose_label_ids),
    ):
        found = (
            set(
                db.scalars(
                    select(CollectionLabel.id).where(
                        CollectionLabel.id.in_(ids), CollectionLabel.category == category
                    )
                )
            )
            if ids
            else set()
        )
        if set(ids) - found:
            raise DashboardFilterError(f"{category}_label_ids must be existing {category} labels")


def _utc_bounds(start: date, end_inclusive: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    def utc(day: date) -> datetime:
        return (
            datetime.combine(day, time(), tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
        )

    return utc(start), utc(end_inclusive + timedelta(days=1))


def _bucket_labels(filters: DashboardFilters) -> list[str]:
    fmt = _BUCKET_FORMATS[filters.granularity]
    if filters.granularity == "month":
        labels = []
        year, month = filters.start_date.year, filters.start_date.month
        while (year, month) <= (filters.end_date.year, filters.end_date.month):
            labels.append(date(year, month, 1).strftime(fmt))
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return labels
    labels = []
    step = timedelta(hours=1) if filters.granularity == "hour" else timedelta(days=1)
    cursor = datetime.combine(filters.start_date, time())
    stop = datetime.combine(filters.end_date + timedelta(days=1), time())
    while cursor < stop:
        labels.append(cursor.strftime(fmt))
        cursor += step
    return labels


def _number(value: Any) -> float | int:
    if value is None:
        return 0
    return float(value) if isinstance(value, Decimal) else value


class _Board:
    def __init__(self, db: Session, filters: DashboardFilters, now: datetime | None):
        self.db, self.filters = db, filters
        zone = ZoneInfo(filters.tz)
        self.range = _utc_bounds(filters.start_date, filters.end_date, zone)
        today = (now or datetime.now(timezone.utc)).astimezone(zone).date()
        self.today = _utc_bounds(today, today, zone)
        self.labels = _bucket_labels(filters)
        tasks = select(CollectionTask.id).where(CollectionTask.workspace_id == filters.workspace_id)
        if filters.project_ids:
            tasks = tasks.where(CollectionTask.collection_project_id.in_(filters.project_ids))
        if filters.task_ids:
            tasks = tasks.where(CollectionTask.id.in_(filters.task_ids))
        for ids in (filters.scene_label_ids, filters.purpose_label_ids):
            if ids:
                tasks = tasks.where(
                    exists().where(
                        CollectionTaskLabel.collection_task_id == CollectionTask.id,
                        CollectionTaskLabel.collection_label_id.in_(ids),
                    )
                )
        self.task_ids = tasks
        self.package_scope = (
            DataPackage.workspace_id == filters.workspace_id,
            DataPackage.collection_task_id.in_(tasks),
        )
        project_scope = [CollectionProject.workspace_id == filters.workspace_id]
        if filters.project_ids:
            project_scope.append(CollectionProject.id.in_(filters.project_ids))
        if filters.has_task_filters:
            project_scope.append(
                CollectionProject.id.in_(
                    select(CollectionTask.collection_project_id).where(CollectionTask.id.in_(tasks))
                )
            )
        self.project_scope, self.task_scope = tuple(project_scope), (CollectionTask.id.in_(tasks),)

    def scalar(self, expr, *conditions):
        return self.db.execute(select(expr).where(*conditions)).scalar()

    def between(self, column, bounds):
        return column >= bounds[0], column < bounds[1]

    def trend(self, column, measure=None, *conditions):
        bucket = func.date_trunc(
            self.filters.granularity, func.timezone(self.filters.tz, func.timezone("UTC", column))
        ).label("bucket")
        value = (measure if measure is not None else literal(1)).label("value")
        inner = (
            select(bucket, value).where(*conditions, *self.between(column, self.range)).subquery()
        )
        aggregate = func.count() if measure is None else func.sum(inner.c.value)
        rows = self.db.execute(select(inner.c.bucket, aggregate).group_by(inner.c.bucket)).all()
        values = {
            row[0].strftime(_BUCKET_FORMATS[self.filters.granularity]): row[1] for row in rows
        }
        return [{"bucket": label, "value": _number(values.get(label, 0))} for label in self.labels]

    def counted(self, created_at, scope):
        return {
            "total": int(
                self.scalar(func.count(), *scope, *self.between(created_at, self.range)) or 0
            ),
            "today": int(
                self.scalar(func.count(), *scope, *self.between(created_at, self.today)) or 0
            ),
            "trend": self.trend(created_at, None, *scope),
        }

    def measured(self, column, *extra):
        captured = DataPackage.captured_started_at
        conditions = (*self.package_scope, column.is_not(None), *extra)
        return (
            _number(
                self.scalar(func.sum(column), *conditions, *self.between(captured, self.range))
            ),
            _number(
                self.scalar(func.sum(column), *conditions, *self.between(captured, self.today))
            ),
            self.trend(captured, column, *conditions),
        )


def data_board(
    db: Session, filters: DashboardFilters, *, now: datetime | None = None
) -> dict[str, Any]:
    board = _Board(db, filters, now)
    valid = filters.basis == "valid"
    duration_column = (
        DataPackage.intake_valid_duration_s if valid else DataPackage.captured_duration_s
    )
    size_column = DataPackage.intake_valid_size_bytes if valid else DataPackage.captured_size_bytes
    status = DataPackage.status
    status_row = db.execute(
        select(
            func.count(),
            func.sum(case((status == "pending_assignment", 1), else_=0)),
            func.sum(case((status == "assigned", 1), else_=0)),
            func.sum(case((status == "voided", 1), else_=0)),
        ).where(*board.package_scope, *board.between(DataPackage.created_at, board.range))
    ).one()
    total, pending, assigned, voided = (int(value or 0) for value in status_row)
    frozen = (*board.package_scope, DataPackage.intake_valid_duration_s.is_not(None))
    captured = DataPackage.captured_started_at
    duration_total, duration_today, duration_trend = board.measured(duration_column)
    size_total, size_today, size_trend = board.measured(size_column)
    pending_review = board.scalar(
        func.sum(DataPackage.captured_duration_s),
        *board.package_scope,
        DataPackage.captured_duration_s.is_not(None),
        DataPackage.intake_valid_duration_s.is_(None),
        *board.between(captured, board.range),
    )
    incomplete = board.scalar(
        func.count(),
        *board.package_scope,
        DataPackage.upload_completed_at.is_not(None),
        DataPackage.captured_duration_s.is_(None),
    )
    return {
        "workspace_id": filters.workspace_id,
        "filters": {
            "start_date": filters.start_date.isoformat(),
            "end_date": filters.end_date.isoformat(),
            "granularity": filters.granularity,
            "basis": filters.basis,
            "tz": filters.tz,
            "project_ids": list(filters.project_ids),
            "task_ids": list(filters.task_ids),
            "scene_label_ids": list(filters.scene_label_ids),
            "purpose_label_ids": list(filters.purpose_label_ids),
        },
        "projects": board.counted(CollectionProject.created_at, board.project_scope),
        "tasks": board.counted(CollectionTask.created_at, board.task_scope),
        "packages": {
            "total": total,
            "today": int(
                board.scalar(func.count(), *frozen, *board.between(captured, board.today)) or 0
            ),
            "trend": board.trend(captured, None, *frozen),
            "status_counts": {
                "pending_assignment": pending,
                "assigned": assigned,
                "other": total - pending - assigned - voided,
                "voided": voided,
            },
        },
        "duration": {
            "basis": filters.basis,
            "total_s": float(duration_total),
            "today_s": float(duration_today),
            "pending_review_duration_s": float(_number(pending_review)),
            "trend": [{"bucket": p["bucket"], "value": float(p["value"])} for p in duration_trend],
        },
        "size": {
            "basis": filters.basis,
            "total_bytes": int(size_total),
            "today_bytes": int(size_today),
            "trend": [{"bucket": p["bucket"], "value": int(p["value"])} for p in size_trend],
        },
        "incomplete_packages": int(incomplete or 0),
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def data_board_csv(payload: dict[str, Any]) -> str:
    basis = "有效" if payload["duration"]["basis"] == "valid" else "全部"
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        ["时间", "项目数", "任务数", "数据包数", f"时长（秒，{basis}）", f"大小（字节，{basis}）"]
    )
    series = [
        payload["projects"]["trend"],
        payload["tasks"]["trend"],
        payload["packages"]["trend"],
        payload["duration"]["trend"],
        payload["size"]["trend"],
    ]
    for index, point in enumerate(series[0]):
        writer.writerow([point["bucket"], *(values[index]["value"] for values in series)])
    return "\ufeff" + buffer.getvalue()


def _filters_json(filters: DashboardFilters) -> dict[str, Any]:
    return {
        "start_date": filters.start_date.isoformat(),
        "end_date": filters.end_date.isoformat(),
        "granularity": filters.granularity,
        "basis": filters.basis,
        "tz": filters.tz,
        "project_ids": list(filters.project_ids),
        "task_ids": list(filters.task_ids),
        "scene_label_ids": list(filters.scene_label_ids),
        "purpose_label_ids": list(filters.purpose_label_ids),
    }


def _package_breakdown(
    db: Session,
    board: _Board,
    *,
    key_column,
    label_model,
    label_column,
    measure_column,
) -> list[dict[str, Any]]:
    rows = db.execute(
        select(key_column, func.sum(measure_column))
        .where(
            *board.package_scope,
            *board.between(DataPackage.captured_started_at, board.range),
            measure_column.is_not(None),
        )
        .group_by(key_column)
        .order_by(key_column)
    ).all()
    ids = {int(row[0]) for row in rows if row[0] is not None}
    labels = {}
    if ids:
        labels = {
            int(row[0]): row[1]
            for row in db.execute(
                select(label_model.id, label_column).where(label_model.id.in_(ids))
            ).all()
        }
    result = []
    for key, value in rows:
        key_id = int(key) if key is not None else None
        result.append(
            {
                "id": key_id,
                "name": labels.get(key_id, "未归属" if key_id is None else str(key_id)),
                "duration_s": float(_number(value)),
            }
        )
    return result


def capacity_board(
    db: Session, filters: DashboardFilters, *, now: datetime | None = None
) -> dict[str, Any]:
    board = _Board(db, filters, now)
    captured = DataPackage.captured_started_at
    package_conditions = (*board.package_scope, *board.between(captured, board.range))
    package_total = int(board.scalar(func.count(), *package_conditions) or 0)
    project_rows = db.execute(
        select(CollectionProject.id, CollectionProject.name, func.count(DataPackage.id))
        .join(DataPackage, DataPackage.collection_project_id == CollectionProject.id)
        .where(*package_conditions)
        .group_by(CollectionProject.id, CollectionProject.name)
        .order_by(CollectionProject.name, CollectionProject.id)
    ).all()
    task_rows = db.execute(
        select(CollectionTask.id, CollectionTask.name, func.count(DataPackage.id))
        .join(DataPackage, DataPackage.collection_task_id == CollectionTask.id)
        .where(*package_conditions)
        .group_by(CollectionTask.id, CollectionTask.name)
        .order_by(CollectionTask.name, CollectionTask.id)
    ).all()

    def distribution(rows):
        return [
            {
                "id": int(row[0]),
                "name": row[1],
                "packages": int(row[2] or 0),
                "share": (int(row[2] or 0) / package_total if package_total else 0),
            }
            for row in rows
        ]

    project_names = {int(row[0]): row[1] for row in project_rows}
    task_names = {int(row[0]): row[1] for row in task_rows}

    task_targets = {
        int(row[0]): float(row[1] or 0) * 3600
        for row in db.execute(
            select(CollectionTask.id, CollectionTask.target_duration_hours).where(*board.task_scope)
        ).all()
    }
    task_valid_rows = db.execute(
        select(DataPackage.collection_task_id, func.sum(DataPackage.intake_valid_duration_s))
        .where(
            *package_conditions,
            DataPackage.intake_valid_duration_s.is_not(None),
        )
        .group_by(DataPackage.collection_task_id)
    ).all()
    task_valid = {int(row[0]): float(_number(row[1])) for row in task_valid_rows}
    task_completion = [
        {
            "id": task_id,
            "name": task_names.get(task_id, str(task_id)),
            "target_s": target,
            "valid_s": task_valid.get(task_id, 0),
            "completion": task_valid.get(task_id, 0) / target if target else 0,
        }
        for task_id, target in sorted(task_targets.items())
    ]
    project_ids_for_tasks = {
        int(row[0]): int(row[1])
        for row in db.execute(
            select(CollectionTask.id, CollectionTask.collection_project_id).where(*board.task_scope)
        ).all()
    }
    project_targets: dict[int, float] = {}
    for task_id, target in task_targets.items():
        project_id = project_ids_for_tasks[task_id]
        project_targets[project_id] = project_targets.get(project_id, 0) + target
    project_valid_rows = db.execute(
        select(DataPackage.collection_project_id, func.sum(DataPackage.intake_valid_duration_s))
        .where(
            *package_conditions,
            DataPackage.intake_valid_duration_s.is_not(None),
        )
        .group_by(DataPackage.collection_project_id)
    ).all()
    project_valid = {int(row[0]): float(_number(row[1])) for row in project_valid_rows}
    project_completion = [
        {
            "id": project_id,
            "name": project_names.get(project_id, str(project_id)),
            "target_s": target,
            "valid_s": project_valid.get(project_id, 0),
            "completion": project_valid.get(project_id, 0) / target if target else 0,
        }
        for project_id, target in sorted(project_targets.items())
    ]

    raw_total, _, raw_trend = board.measured(DataPackage.captured_duration_s)
    valid_total, _, valid_trend = board.measured(DataPackage.intake_valid_duration_s)
    return {
        "workspace_id": filters.workspace_id,
        "filters": _filters_json(filters),
        "projects": distribution(project_rows),
        "tasks": distribution(task_rows),
        "completion": {"projects": project_completion, "tasks": task_completion},
        "duration": {
            "raw_total_s": float(raw_total),
            "valid_total_s": float(valid_total),
            "raw_trend": raw_trend,
            "valid_trend": valid_trend,
        },
        "personnel": _package_breakdown(
            db,
            board,
            key_column=DataPackage.operator_collector_id,
            label_model=PersonnelProfile,
            label_column=PersonnelProfile.name,
            measure_column=DataPackage.intake_valid_duration_s,
        ),
        "devices": _package_breakdown(
            db,
            board,
            key_column=DataPackage.collection_device_id,
            label_model=CollectionDevice,
            label_column=CollectionDevice.name,
            measure_column=DataPackage.intake_valid_duration_s,
        ),
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def efficiency_board(
    db: Session, filters: DashboardFilters, *, now: datetime | None = None
) -> dict[str, Any]:
    board = _Board(db, filters, now)
    captured = DataPackage.captured_started_at
    seven_start = max(filters.start_date, filters.end_date - timedelta(days=6))
    seven_range = _utc_bounds(seven_start, filters.end_date, ZoneInfo(filters.tz))
    conditions = (
        *board.package_scope,
        captured >= seven_range[0],
        captured < seven_range[1],
        DataPackage.intake_valid_duration_s.is_not(None),
    )
    rows = db.execute(
        select(DataPackage.operator_collector_id, func.sum(DataPackage.intake_valid_duration_s))
        .where(*conditions)
        .group_by(DataPackage.operator_collector_id)
        .order_by(func.sum(DataPackage.intake_valid_duration_s).desc())
    ).all()
    ids = {int(row[0]) for row in rows if row[0] is not None}
    names = (
        {
            int(row[0]): row[1]
            for row in db.execute(
                select(PersonnelProfile.id, PersonnelProfile.name).where(
                    PersonnelProfile.id.in_(ids)
                )
            ).all()
        }
        if ids
        else {}
    )
    output = [
        {
            "id": int(row[0]) if row[0] is not None else None,
            "name": names.get(int(row[0]), "未归属") if row[0] is not None else "未归属",
            "valid_duration_s": float(_number(row[1])),
        }
        for row in rows
    ]
    person_bucket = func.date_trunc(
        filters.granularity,
        func.timezone(filters.tz, func.timezone("UTC", captured)),
    ).label("bucket")
    person_rows = db.execute(
        select(
            DataPackage.operator_collector_id,
            person_bucket,
            func.sum(DataPackage.intake_valid_duration_s),
        )
        .where(*conditions)
        .group_by(DataPackage.operator_collector_id, person_bucket)
        .order_by(person_bucket, DataPackage.operator_collector_id)
    ).all()
    person_trend = [
        {
            "id": int(row[0]) if row[0] is not None else None,
            "name": names.get(int(row[0]), "未归属") if row[0] is not None else "未归属",
            "bucket": row[1].strftime(_BUCKET_FORMATS[filters.granularity]),
            "valid_duration_s": float(_number(row[2])),
        }
        for row in person_rows
    ]
    return {
        "workspace_id": filters.workspace_id,
        "filters": _filters_json(filters),
        "top5": output[:5],
        "bottom5": list(reversed(output[-5:])) if output else [],
        "trend": board.trend(captured, DataPackage.intake_valid_duration_s, *conditions),
        "person_trend": person_trend,
        "personnel": output,
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def csv_filename(filters: DashboardFilters) -> str:
    return (
        f"collection-data-board_{filters.start_date.isoformat()}_{filters.end_date.isoformat()}.csv"
    )
