"""P1 behavior_tags table-driven Tag dictionary CRUD, synchronizing with annotate_tags in-memory vocabulary."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from data.database import AnnotationRevision, BehaviorTag, CustomBehaviorAction, EgoEpisode
from data.services.annotate_tags import ACTION_TAGS as DEFAULT_ACTION_TAGS
from data.services.workspace_access import require_actor, require_workspace_actor

_TAG_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_DEFAULT_TAG_SNAPSHOT = tuple(
    (str(tag["action"]), str(tag["label"]), str(tag["color"])) for tag in DEFAULT_ACTION_TAGS
)


class CustomActionUnavailable(ValueError):
    """The custom action is missing or outside the actor's workspace scope."""


class CustomActionPromotionConflict(ValueError):
    """Another writer concurrently changed the platform promotion target."""


@dataclass(frozen=True)
class BehaviorVocabularyTag:
    id: int | None
    action: str
    label: str
    color: str
    sort_order: int
    is_active: bool

    def to_item(self) -> dict:
        return {
            "id": self.id,
            "action": self.action,
            "label": self.label,
            "color": self.color,
            "sort_order": self.sort_order,
            "is_active": self.is_active,
            "tag_key": self.action,
            "tag_label": self.label,
            "key": self.action,
        }

    def to_option(self) -> dict[str, str]:
        return {"action": self.action, "label": self.label, "color": self.color}


@dataclass(frozen=True)
class BehaviorVocabularySnapshot:
    tags: tuple[BehaviorVocabularyTag, ...]
    action_by_key: Mapping[str, BehaviorVocabularyTag]
    label_by_key: Mapping[str, str]
    label_to_key: Mapping[str, str]

    def items(self) -> list[dict]:
        return [tag.to_item() for tag in self.tags]

    def action_options(self) -> list[dict[str, str]]:
        return [tag.to_option() for tag in self.tags]


def build_vocabulary_snapshot(items: list[dict]) -> BehaviorVocabularySnapshot:
    tags = tuple(
        BehaviorVocabularyTag(
            id=item.get("id"),
            action=str(item["action"]),
            label=str(item["label"]),
            color=str(item["color"]),
            sort_order=int(item.get("sort_order", index)),
            is_active=bool(item.get("is_active", True)),
        )
        for index, item in enumerate(items)
    )
    return BehaviorVocabularySnapshot(
        tags=tags,
        action_by_key=MappingProxyType({tag.action: tag for tag in tags}),
        label_by_key=MappingProxyType({tag.action: tag.label for tag in tags}),
        label_to_key=MappingProxyType({tag.label: tag.action for tag in tags}),
    )


def _validate_tag_key(tag_key: str) -> str:
    key = (tag_key or "").strip().lower()
    if not key or not _TAG_KEY_RE.match(key):
        raise ValueError("tag_key 须为小写字母开头的字母/数字/下划线组合")
    return key


def _row_to_item(row: BehaviorTag) -> dict:
    return {
        "id": row.id,
        "action": row.tag_key,
        "label": row.tag_label,
        "color": row.color,
        "sort_order": row.sort_order,
        "is_active": row.is_active,
        "tag_key": row.tag_key,
        "tag_label": row.tag_label,
        "key": row.tag_key,
    }


def _default_tag_items() -> list[dict]:
    return [
        {
            "id": None,
            "action": action,
            "label": label,
            "color": color,
            "sort_order": sort_order,
            "is_active": True,
            "tag_key": action,
            "tag_label": label,
            "key": action,
        }
        for sort_order, (action, label, color) in enumerate(_DEFAULT_TAG_SNAPSHOT)
    ]


def _cache_tags(
    rows: list[BehaviorTag],
    *,
    table_is_empty: bool,
) -> list[dict[str, str]]:
    if rows:
        return [{"action": row.tag_key, "label": row.tag_label, "color": row.color} for row in rows]
    if not table_is_empty:
        return []
    return [
        {"action": action, "label": label, "color": color}
        for action, label, color in _DEFAULT_TAG_SNAPSHOT
    ]


def _replace_annotate_tags_cache(tags: list[dict[str, str]]) -> None:
    from data.services import annotate_tags as at

    action_by_key = {tag["action"]: tag for tag in tags}
    label_by_key = {tag["action"]: tag["label"] for tag in tags}
    label_to_key = {tag["label"]: tag["action"] for tag in tags}
    behavior_tags = [
        {"key": tag["action"], "label": tag["label"], "color": tag["color"], **tag} for tag in tags
    ]

    at.ACTION_TAGS[:] = tags
    at.ACTION_BY_KEY.clear()
    at.ACTION_BY_KEY.update(action_by_key)
    if at.TAG_BY_KEY is not at.ACTION_BY_KEY:
        at.TAG_BY_KEY.clear()
        at.TAG_BY_KEY.update(action_by_key)
    at.LABEL_BY_KEY.clear()
    at.LABEL_BY_KEY.update(label_by_key)
    at.LABEL_TO_KEY.clear()
    at.LABEL_TO_KEY.update(label_to_key)
    at.BEHAVIOR_TAGS[:] = behavior_tags


def _active_tag_state(db: Session) -> tuple[list[BehaviorTag], bool]:
    rows = db.query(BehaviorTag).order_by(BehaviorTag.sort_order.asc(), BehaviorTag.id.asc()).all()
    return [row for row in rows if row.is_active], not rows


def upsert_default_tags_for_write(db: Session) -> None:
    if db.query(BehaviorTag).count() > 0:
        return
    values = [
        {
            "tag_key": action,
            "tag_label": label,
            "color": color,
            "sort_order": sort_order,
            "is_active": True,
        }
        for sort_order, (action, label, color) in enumerate(_DEFAULT_TAG_SNAPSHOT)
    ]
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert

        statement = (
            insert(BehaviorTag).values(values).on_conflict_do_nothing(index_elements=["tag_key"])
        )
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert

        statement = (
            insert(BehaviorTag).values(values).on_conflict_do_nothing(index_elements=["tag_key"])
        )
    else:
        raise RuntimeError(f"unsupported database dialect: {dialect}")
    db.execute(statement)


def sync_annotate_tags_cache(db: Session) -> None:
    """Synchronize active behavior_tags entries into annotate_tags module global vocabulary."""
    rows, table_is_empty = _active_tag_state(db)
    _replace_annotate_tags_cache(_cache_tags(rows, table_is_empty=table_is_empty))


def get_active_vocabulary_snapshot(db: Session) -> BehaviorVocabularySnapshot:
    rows, table_is_empty = _active_tag_state(db)
    _replace_annotate_tags_cache(_cache_tags(rows, table_is_empty=table_is_empty))
    if rows:
        items = [_row_to_item(row) for row in rows]
    else:
        items = _default_tag_items() if table_is_empty else []
    return build_vocabulary_snapshot(items)


def list_active_tags(db: Session) -> list[dict]:
    return get_active_vocabulary_snapshot(db).items()


def create_tag(
    db: Session,
    *,
    tag_key: str,
    tag_label: str,
    color: str = "#8c8c8c",
    sort_order: int | None = None,
) -> dict:
    key = _validate_tag_key(tag_key)
    label = (tag_label or "").strip()
    if not label:
        raise ValueError("tag_label 不能为空")
    existing = db.query(BehaviorTag).filter(BehaviorTag.tag_key == key).first()
    if existing:
        if existing.is_active:
            raise ValueError(f"tag_key {key!r} 已存在")
        existing.tag_label = label
        existing.color = color or "#8c8c8c"
        if sort_order is not None:
            existing.sort_order = sort_order
        existing.is_active = True
        db.commit()
        db.refresh(existing)
        sync_annotate_tags_cache(db)
        return _row_to_item(existing)
    if sort_order is None:
        max_order = db.query(BehaviorTag.sort_order).order_by(BehaviorTag.sort_order.desc()).first()
        sort_order = (max_order[0] if max_order else -1) + 1
    row = BehaviorTag(
        tag_key=key,
        tag_label=label,
        color=color or "#8c8c8c",
        sort_order=sort_order,
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    sync_annotate_tags_cache(db)
    return _row_to_item(row)


def update_tag(
    db: Session,
    tag_key: str,
    *,
    tag_label: str | None = None,
    color: str | None = None,
    sort_order: int | None = None,
    is_active: bool | None = None,
) -> dict:
    key = _validate_tag_key(tag_key)
    row = db.query(BehaviorTag).filter(BehaviorTag.tag_key == key).first()
    if not row:
        raise ValueError(f"tag_key {key!r} 不存在")
    if tag_label is not None:
        label = tag_label.strip()
        if not label:
            raise ValueError("tag_label 不能为空")
        row.tag_label = label
    if color is not None:
        row.color = color or "#8c8c8c"
    if sort_order is not None:
        row.sort_order = sort_order
    if is_active is not None:
        row.is_active = is_active
    db.commit()
    db.refresh(row)
    sync_annotate_tags_cache(db)
    return _row_to_item(row)


def delete_tag(db: Session, tag_key: str) -> dict:
    """Soft delete: is_active=false."""
    key = _validate_tag_key(tag_key)
    row = db.query(BehaviorTag).filter(BehaviorTag.tag_key == key).first()
    if not row:
        raise ValueError(f"tag_key {key!r} 不存在")
    if not row.is_active:
        return _row_to_item(row)
    row.is_active = False
    db.commit()
    db.refresh(row)
    sync_annotate_tags_cache(db)
    return _row_to_item(row)


def promote_custom_action(
    db: Session,
    custom_action_id: int,
    *,
    tag_key: str,
    tag_label: str,
    actor_id: int,
    color: str = "#8c8c8c",
) -> dict:
    """Explicitly promote one accepted workspace custom action into BehaviorTag."""
    actor = require_actor(db, actor_id=actor_id)
    if actor.role != "admin":
        raise PermissionError("only an operator can promote custom actions")
    custom = (
        db.query(CustomBehaviorAction)
        .filter(CustomBehaviorAction.id == custom_action_id)
        .with_for_update()
        .one_or_none()
    )
    if custom is None:
        raise CustomActionUnavailable("custom action is unavailable")
    try:
        require_workspace_actor(db, actor_id=actor.id, workspace_id=custom.workspace_id)
    except PermissionError as exc:
        raise CustomActionUnavailable("custom action is unavailable") from exc
    revision = db.get(AnnotationRevision, custom.annotation_revision_id)
    if revision is None or revision.status != "accepted":
        raise ValueError("only an accepted annotation custom action can be promoted")
    episode = db.get(EgoEpisode, revision.ego_episode_id)
    if episode is None or episode.workspace_id != custom.workspace_id:
        raise ValueError("custom action workspace does not match its annotation revision")
    if custom.promoted_behavior_tag_id is not None:
        existing_promotion = db.get(BehaviorTag, custom.promoted_behavior_tag_id)
        if existing_promotion is None:
            raise ValueError("promoted behavior tag is unavailable")
        return _row_to_item(existing_promotion)

    key = _validate_tag_key(tag_key)
    label = (tag_label or "").strip()
    if not label:
        raise ValueError("tag_label 不能为空")
    try:
        tag = db.query(BehaviorTag).filter(BehaviorTag.tag_key == key).one_or_none()
        if tag is None:
            max_order = (
                db.query(BehaviorTag.sort_order).order_by(BehaviorTag.sort_order.desc()).first()
            )
            tag = BehaviorTag(
                tag_key=key,
                tag_label=label,
                color=color or "#8c8c8c",
                sort_order=(max_order[0] if max_order else -1) + 1,
                is_active=True,
            )
            db.add(tag)
            db.flush()
        elif not tag.is_active:
            tag.tag_label = label
            tag.color = color or "#8c8c8c"
            tag.is_active = True
        custom.promoted_behavior_tag_id = tag.id
        custom.promoted_by_user_id = actor.id
        custom.promoted_at = datetime.utcnow()
        db.commit()
    except (IntegrityError, OperationalError) as exc:
        db.rollback()
        if _is_custom_action_promotion_conflict(db, exc):
            raise CustomActionPromotionConflict(
                "custom action promotion changed concurrently; retry"
            ) from exc
        raise
    except Exception:
        db.rollback()
        raise
    db.refresh(tag)
    sync_annotate_tags_cache(db)
    return _row_to_item(tag)


def _is_custom_action_promotion_conflict(
    db: Session,
    exc: IntegrityError | OperationalError,
) -> bool:
    message = str(getattr(exc, "orig", exc)).lower()
    if isinstance(exc, OperationalError):
        return db.get_bind().dialect.name == "sqlite" and "locked" in message
    diagnostic = getattr(getattr(exc, "orig", None), "diag", None)
    constraint_name = str(getattr(diagnostic, "constraint_name", "")).lower()
    return "behavior_tags.tag_key" in message or constraint_name == "behavior_tags_tag_key_key"
