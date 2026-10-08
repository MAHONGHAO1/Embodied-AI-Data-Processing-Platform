"""Collection label dictionary operations."""

from __future__ import annotations

from sqlalchemy.orm import Session

from data.models.collection_config import COLLECTION_LABEL_CATEGORIES, CollectionLabel


def create_collection_label(
    db: Session,
    *,
    category: str,
    name: str,
    description: str = "",
) -> CollectionLabel:
    if category not in COLLECTION_LABEL_CATEGORIES:
        raise ValueError(f"unsupported label category: {category}")
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("label name is required")
    label = CollectionLabel(
        category=category,
        name=cleaned,
        description=description.strip(),
        is_active=True,
    )
    db.add(label)
    db.flush()
    return label


def deactivate_collection_label(db: Session, *, label_id: int) -> CollectionLabel:
    label = db.get(CollectionLabel, label_id)
    if label is None:
        raise ValueError("label does not exist")
    label.is_active = False
    db.flush()
    return label
