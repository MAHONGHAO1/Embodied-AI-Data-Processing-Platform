"""Dataset construction, version control, and lineage tracing (P1)."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from data.database import Dataset, QrdfData
from data.integrations.qrdf.service import list_topic_stats
from data.utils.formatting import format_api_datetime
from data.utils.storage_uri import qrdf_storage_display_uri


def _qrdf_trace_item(q: QrdfData) -> dict[str, Any]:
    storage_uri = qrdf_storage_display_uri(q)
    return {
        "qrdf_id": q.id,
        "task_id": q.task_id,
        "name": q.name,
        "data_source": q.data_source,
        "scene": q.scene,
        "quality_level": q.quality_level,
        "storage_path": storage_uri,
        "storage_uri": storage_uri,
        "created_at": format_api_datetime(q.created_at),
    }


def aggregate_dataset_distributions(qrdf_records: list[QrdfData]) -> dict[str, Any]:
    scene_counter: Counter[str] = Counter()
    topic_counter: Counter[str] = Counter()
    source_counter: Counter[str] = Counter()

    for record in qrdf_records:
        if record.scene:
            scene_counter[record.scene] += 1
        if record.data_source:
            source_counter[record.data_source] += 1
        for item in list_topic_stats(record.storage_path):
            topic_counter[item.get("name", "")] += int(item.get("frame_count") or 0)

    return {
        "scene_distribution": dict(scene_counter),
        "topic_distribution": dict(topic_counter),
        "data_source_distribution": dict(source_counter),
    }


def build_lineage_snapshot(
    qrdf_records: list[QrdfData],
    *,
    operator: str,
    version: str,
    qrdf_ids: list[int],
    note: str = "",
) -> dict[str, Any]:
    data_sources = sorted({q.data_source for q in qrdf_records if q.data_source})
    return {
        "version": version,
        "created_at": format_api_datetime(datetime.utcnow()),
        "created_by": operator,
        "qrdf_ids": qrdf_ids,
        "qrdf_snapshot": [_qrdf_trace_item(q) for q in qrdf_records],
        "data_sources": data_sources,
        "note": note,
    }


def next_dataset_version(db: Session, project_id: int, name: str) -> str:
    """Increment minor version number for same-name dataset under the same project."""
    siblings = (
        db.query(Dataset)
        .filter(Dataset.project_id == project_id, Dataset.name == name)
        .order_by(Dataset.id.desc())
        .all()
    )
    if not siblings:
        return "v1.0.0"
    latest = siblings[0]
    current = (latest.version or "v1.0.0").lstrip("v")
    parts = current.split(".")
    try:
        major, minor, patch = (
            (int(parts[0]), int(parts[1]), int(parts[2]))
            if len(parts) >= 3
            else (1, 0, len(siblings))
        )
    except ValueError:
        return f"v1.0.{len(siblings)}"
    return f"v{major}.{minor}.{patch + 1}"


def enrich_dataset_stats(
    qrdf_records: list[QrdfData], base_stats: dict[str, Any]
) -> dict[str, Any]:
    distributions = aggregate_dataset_distributions(qrdf_records)
    return {**base_stats, **distributions}


def build_dataset_detail(ds: Dataset, qrdf_items: list[QrdfData]) -> dict[str, Any]:
    lineage = ds.lineage_json or {}
    versions = lineage.get("versions") or []
    return {
        "id": ds.id,
        "name": ds.name,
        "description": ds.description,
        "project_id": ds.project_id,
        "version": ds.version,
        "created_by": ds.created_by or lineage.get("created_by"),
        "created_at": format_api_datetime(ds.created_at),
        "stats": ds.stats_json,
        "qrdf_ids": ds.qrdf_ids,
        "data_sources": lineage.get("data_sources")
        or sorted({q.data_source for q in qrdf_items if q.data_source}),
        "qrdf_list": [_qrdf_trace_item(q) for q in qrdf_items],
        "lineage": lineage,
        "versions": versions,
        "current_version": lineage.get("current_version") or ds.version,
    }
