"""Persistent PostgreSQL security audit events with compatibility serializers."""

from __future__ import annotations

import calendar
import hashlib
import json
import logging
from datetime import datetime
from typing import Any

from data.security.patterns import (
    ABSOLUTE_PATH_FRAGMENT,
    CLOUD_ACCESS_KEY_VALUE,
    SENSITIVE_URI_REFERENCE,
    contains_absolute_path_fragment,
    contains_cloud_access_key_value,
    contains_sensitive_uri_reference,
    is_absolute_path,
)
from data.security.redact import redact_secrets

logger = logging.getLogger("quicdata.audit")

KNOWN_EVENTS = frozenset(
    {
        "auth.login.success",
        "auth.login.fail",
        "auth.token.refresh",
        "auth.token.revoke",
        "auth.password.change",
        "auth.bootstrap_admin",
        "auth.role.change",
        "auth.user.status.change",
        "config.read",
        "config.write",
        "platform_settings.read",
        "platform_settings.ai.write",
        "platform_settings.oss_scope.create",
        "platform_settings.oss_scope.write",
        "file.download",
        "file.preview",
        "file.direct_url",
        "episode.artifact.download",
        "export.create",
        "export.download",
        "storage.cleanup",
        "security.path.denied",
        "workspace.member.grant",
        "workspace.member.list",
        "workspace.member.revoke",
        "batch.create",
        "native_lerobot_dataset.register",
        "native_lerobot_dataset.copy.dispatch",
        "native_lerobot_dataset.copy.retry",
        "native_lerobot_dataset.oss_uri.read",
        "native_lerobot_dataset.source.reauthorize",
        "native_lerobot_dataset.archive",
        "native_lerobot_direct.declare",
        "native_lerobot_direct.retry",
        "import.create",
        "collection.intake.review",
        "collection.upload.create",
        "collection.upload.cancel",
        "task_label.create",
        "dataset.create",
        "dataset.revision.create",
        "dataset.revision.export",
        "dataset.revision.delivery_uri.read",
        "dataset.revision.retire",
        "governance.batch.create",
        "governance.stage.retry",
        "governance.qc.drop",
        "annotation.assign",
        "annotation.reassign",
        "annotation.submit",
        "review.approve",
        "review.return",
        "review.reassign",
        "asset.publish",
        "catalog.dataset.create",
        "catalog.version.create",
        "catalog.version.archive",
        "catalog.version.export",
    }
)

_PRIVATE_DETAIL_MARKERS = (
    "absolute_path",
    "authorization",
    "credential",
    "exception",
    "header",
    "raw_uri",
    "signed_url",
    "stack",
    "traceback",
)


def _hashed_token(kind: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{kind}:{digest}"


def _sanitize_string(value: str) -> str:
    stripped = value.strip()
    if is_absolute_path(stripped):
        return _hashed_token("path", stripped)
    sanitized = value
    if contains_cloud_access_key_value(sanitized):
        sanitized = CLOUD_ACCESS_KEY_VALUE.sub(
            lambda match: _hashed_token("credential", match.group(0)),
            sanitized,
        )
    sanitized = SENSITIVE_URI_REFERENCE.sub(
        lambda match: _hashed_token("uri", match.group(0)),
        sanitized,
    )
    sanitized = ABSOLUTE_PATH_FRAGMENT.sub(
        lambda match: _hashed_token("path", match.group(0)),
        sanitized,
    )
    return sanitized


def _contains_sensitive_fragment(value: str) -> bool:
    return (
        contains_cloud_access_key_value(value)
        or contains_absolute_path_fragment(value)
        or contains_sensitive_uri_reference(value)
    )


def _log_audit_failure(operation: str, error_code: str, error: Exception) -> None:
    logger.error(
        "security audit operation failed operation=%s error_code=%s error_type=%s",
        operation,
        error_code,
        type(error).__name__,
    )


def _safe_detail_value(value: Any) -> Any:
    if isinstance(value, dict):
        entries: list[tuple[str, Any, bool]] = []
        safe_keys: set[str] = set()
        for key, item in value.items():
            raw_key = str(key)
            if any(marker in raw_key.lower() for marker in _PRIVATE_DETAIL_MARKERS):
                continue
            sensitive = _contains_sensitive_fragment(raw_key)
            entries.append((raw_key, item, sensitive))
            if not sensitive:
                safe_keys.add(raw_key)

        sanitized: dict[str, Any] = {}
        for raw_key, item, sensitive in entries:
            base_key = _hashed_token("redacted_key", raw_key) if sensitive else raw_key
            safe_key = base_key
            suffix = 2
            while safe_key in sanitized or (sensitive and safe_key in safe_keys):
                safe_key = f"{base_key}:{suffix}"
                suffix += 1
            sanitized[safe_key] = _safe_detail_value(item)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [_safe_detail_value(item) for item in value]
    if isinstance(value, str):
        return _sanitize_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return type(value).__name__


def _sanitize_detail(detail: dict[str, Any] | None) -> dict[str, Any]:
    sanitized = _safe_detail_value(redact_secrets(detail or {}))
    return sanitized if isinstance(sanitized, dict) else {}


def _sanitize_resource_type(value: str) -> str:
    if _contains_sensitive_fragment(value):
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return f"sha256-{digest[:57]}"
    return value[:64]


def _sanitize_resource_id(value: str) -> str:
    if _contains_sensitive_fragment(value):
        return _hashed_token("sha256", value)
    return value[:128]


def _resource_parts(resource: str | None) -> tuple[str | None, str | None]:
    if not resource:
        return None, None
    stripped = resource.strip()
    if is_absolute_path(stripped):
        return "path", hashlib.sha256(stripped.encode("utf-8")).hexdigest()
    if SENSITIVE_URI_REFERENCE.fullmatch(stripped):
        digest = hashlib.sha256(stripped.encode("utf-8")).hexdigest()
        return "uri", digest
    resource_type, separator, resource_id = stripped.partition(":")
    if separator and resource_type and resource_id:
        return _sanitize_resource_type(resource_type), _sanitize_resource_id(resource_id)
    if _contains_sensitive_fragment(stripped):
        return "resource", _hashed_token("sha256", stripped)
    return None, stripped[:128]


def _result_for_event(event: str) -> str:
    if event.endswith(".fail") or event.endswith(".denied"):
        return "failure"
    return "success"


def _safe_resource(resource: str | None) -> str | None:
    resource_type, resource_id = _resource_parts(resource)
    if resource_type and resource_id:
        return f"{resource_type}:{resource_id}"
    return resource_id


def _safe_stored_resource(resource_type: str | None, resource_id: str | None) -> str | None:
    if resource_type and resource_id:
        safe_type = _sanitize_resource_type(str(resource_type))
        safe_id = _sanitize_resource_id(str(resource_id))
        return f"{safe_type}:{safe_id}"
    return _safe_resource(str(resource_id)) if resource_id else None


def _actor_id(db, actor: str | None) -> int | None:
    import data.database as database

    if not actor:
        return None
    if actor.isdigit():
        return int(actor) if db.get(database.User, int(actor)) is not None else None
    user = db.query(database.User).filter(database.User.email == actor).one_or_none()
    return int(user.id) if user is not None else None


def _persist_event(payload: dict[str, Any]) -> None:
    """Append one sanitized event to the relational audit fact table."""
    import data.database as database

    db = None
    try:
        db = database.SessionLocal()
        resource_type, resource_id = _resource_parts(payload.get("resource"))
        detail = _sanitize_detail(dict(payload.get("detail") or {}))
        workspace_id = detail.get("workspace_id")
        correlation_id = str(detail.get("correlation_id") or "")[:64]
        db.add(
            database.SecurityAuditEvent(
                action=str(payload["event"])[:64],
                actor_id=_actor_id(db, payload.get("actor")),
                workspace_id=int(workspace_id) if str(workspace_id or "").isdigit() else None,
                resource_type=resource_type,
                resource_id=resource_id,
                result=_result_for_event(str(payload["event"])),
                occurred_at=datetime.utcfromtimestamp(int(payload["ts"])),
                detail_json=detail,
                correlation_id=correlation_id,
            )
        )
        db.commit()
    except Exception as error:
        _log_audit_failure("persist", "AUDIT_PERSIST_FAILED", error)
        if db is not None:
            try:
                db.rollback()
            except Exception as rollback_error:
                _log_audit_failure("rollback", "AUDIT_ROLLBACK_FAILED", rollback_error)
    finally:
        if db is not None:
            try:
                db.close()
            except Exception as close_error:
                _log_audit_failure("close", "AUDIT_CLOSE_FAILED", close_error)


def _serialize_event(row: Any, actor: str | None) -> dict[str, Any]:
    return {
        "event": row.action,
        "ts": calendar.timegm(row.occurred_at.utctimetuple()),
        "actor": actor,
        "resource": _safe_stored_resource(row.resource_type, row.resource_id),
        "detail": _sanitize_detail(row.detail_json),
    }


def list_recent_audit_events(*, limit: int = 50, event: str | None = None) -> list[dict[str, Any]]:
    import data.database as database

    limit = max(1, min(int(limit), 200))
    db = None
    try:
        db = database.SessionLocal()
        query = db.query(database.SecurityAuditEvent, database.User.email).outerjoin(
            database.User,
            database.User.id == database.SecurityAuditEvent.actor_id,
        )
        if event:
            query = query.filter(database.SecurityAuditEvent.action == event)
        rows = query.order_by(
            database.SecurityAuditEvent.occurred_at.desc(),
            database.SecurityAuditEvent.id.desc(),
        ).limit(limit)
        return [_serialize_event(row, actor) for row, actor in rows]
    except Exception as error:
        _log_audit_failure("query", "AUDIT_QUERY_FAILED", error)
        return []
    finally:
        if db is not None:
            try:
                db.close()
            except Exception as close_error:
                _log_audit_failure("close_query", "AUDIT_QUERY_CLOSE_FAILED", close_error)


def clear_memory_audit_for_tests() -> None:
    """Compatibility test helper; audit storage itself is PostgreSQL/SQLite."""
    import data.database as database

    db = database.SessionLocal()
    try:
        db.query(database.SecurityAuditEvent).delete()
        db.commit()
    finally:
        db.close()


def emit_audit_event(
    event: str,
    *,
    actor: str | None = None,
    resource: str | None = None,
    detail: dict[str, Any] | None = None,
    level: str = "info",
    evaluate_anomaly: bool = True,
) -> dict[str, Any]:
    """Log and persist a sanitized security audit event."""
    safe_detail = _sanitize_detail(detail)
    if event not in KNOWN_EVENTS:
        safe_detail["unknown_event"] = True
    payload: dict[str, Any] = {
        "event": event,
        "ts": calendar.timegm(datetime.utcnow().utctimetuple()),
        "actor": actor,
        "resource": _safe_resource(resource),
        "detail": safe_detail,
    }

    line = json.dumps(payload, ensure_ascii=False, default=str)
    log_fn = getattr(logger, level if level in ("debug", "info", "warning", "error") else "info")
    log_fn(line)
    _persist_event(payload)

    if evaluate_anomaly:
        try:
            from data.security.anomaly import evaluate_audit_event

            evaluate_audit_event(payload)
        except Exception:
            logger.debug("anomaly evaluation skipped", exc_info=True)
    return payload


def add_transaction_audit(
    db,
    event: str,
    *,
    actor_id: int | None,
    workspace_id: int,
    resource_type: str,
    resource_id: int | str,
    detail: dict | None = None,
):
    """Persist a business event in the caller's transaction, including its rollback."""
    from data.database import SecurityAuditEvent

    db.add(
        SecurityAuditEvent(
            action=event,
            actor_id=actor_id,
            workspace_id=workspace_id,
            resource_type=resource_type,
            resource_id=str(resource_id),
            result="success",
            occurred_at=datetime.utcnow(),
            detail_json=_sanitize_detail(detail),
            correlation_id="",
        )
    )
