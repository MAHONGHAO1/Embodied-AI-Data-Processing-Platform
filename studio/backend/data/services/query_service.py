"""QRDF / data package multidimensional search (shared by 4.2 dataset/query and package/query)."""

import re
import uuid
from typing import Any

from sqlalchemy.orm import Session

from data.database import Annotation, QrdfData, QuerySession
from data.services.workspace_access import qrdf_access_filter
from data.utils.formatting import format_api_datetime
from data.utils.storage_uri import qrdf_storage_display_uri


def _quality_score(level: str) -> float:
    mapping = {"valid": 1.0, "warning": 0.7, "invalid": 0.3}
    return mapping.get(level or "valid", 0.5)


def _match_quality_filter(level: str, quality_filter: str) -> bool:
    if not quality_filter:
        return True
    score = _quality_score(level)
    m = re.match(r"^(>=|<=|>|<|=)?([\d.]+)$", quality_filter.strip())
    if not m:
        return level == quality_filter
    op, val_str = m.group(1) or "=", m.group(2)
    val = float(val_str)
    if op == ">":
        return score > val
    if op == ">=":
        return score >= val
    if op == "<":
        return score < val
    if op == "<=":
        return score <= val
    return abs(score - val) < 0.01


def _task_has_tag(db: Session, task_id: int, tag: str) -> bool:
    ann = (
        db.query(Annotation)
        .filter(Annotation.task_id == task_id)
        .order_by(Annotation.version.desc())
        .first()
    )
    if not ann or not ann.data_json:
        return False
    payload = ann.data_json
    items = payload if isinstance(payload, list) else list(payload.get("region_frames") or [])
    if isinstance(payload, dict):
        items.extend((payload.get("clip_descriptions") or {}).get("segments", {}).values())
    for item in items:
        if isinstance(item, dict):
            value = item.get("action") or item.get("tag") or ""
            if tag in value:
                return True
    return False


def search_packages(
    db: Session,
    filters: dict[str, Any],
    page: int = 1,
    size: int = 20,
    persist_session: bool = True,
    *,
    actor_id: int | None,
) -> dict:
    q = db.query(QrdfData).filter(qrdf_access_filter(db, actor_id=actor_id))
    if filters.get("workspace_id"):
        from data.services.workspace_scope import project_ids_for_workspace

        pids = project_ids_for_workspace(db, int(filters["workspace_id"]))
        if pids:
            q = q.filter(QrdfData.project_id.in_(pids))
        else:
            q = q.filter(QrdfData.id == -1)
    elif filters.get("project_id"):
        q = q.filter(QrdfData.project_id == filters["project_id"])
    if filters.get("scene"):
        q = q.filter(QrdfData.scene == filters["scene"])
    if filters.get("keyword"):
        q = q.filter(QrdfData.name.contains(filters["keyword"]))
    if filters.get("quality"):
        q = q.filter(QrdfData.quality_level.isnot(None))

    items = q.order_by(QrdfData.created_at.desc()).all()

    tag = filters.get("tag")
    quality = filters.get("quality")
    if tag or (quality and re.match(r"^(>=|<=|>|<)", quality or "")):
        filtered = []
        for item in items:
            if tag and not _task_has_tag(db, item.task_id, tag):
                meta_tags = (item.metadata_json or {}).get("tags") or []
                if tag not in meta_tags and tag not in item.name:
                    continue
            if quality and not _match_quality_filter(item.quality_level, quality):
                continue
            filtered.append(item)
        items = filtered
    elif quality:
        items = [i for i in items if _match_quality_filter(i.quality_level, quality)]

    total = len(items)
    start = (page - 1) * size
    page_items = items[start : start + size]
    preview = [_preview_item(i) for i in page_items]
    matched_ids = [i.id for i in items]

    result: dict[str, Any] = {
        "total_matched": total,
        "preview": preview,
        "matched_ids": matched_ids,
    }
    if persist_session:
        query_id = uuid.uuid4().hex[:16]
        session = QuerySession(
            id=query_id,
            filters_json=filters,
            matched_ids=matched_ids,
            total_matched=total,
            preview_json=preview,
        )
        db.add(session)
        db.commit()
        result["query_id"] = query_id
    return result


def _preview_item(item: QrdfData) -> dict:
    storage_uri = qrdf_storage_display_uri(item)
    return {
        "id": item.id,
        "task_id": item.task_id,
        "project_id": item.project_id,
        "name": item.name,
        "data_source": item.data_source,
        "scene": item.scene,
        "quality_level": item.quality_level,
        "storage_path": storage_uri,
        "storage_uri": storage_uri,
        "created_at": format_api_datetime(item.created_at),
    }
