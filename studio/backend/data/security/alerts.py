"""Security alert channels: logs (required) + PostgreSQL + Webhook/email (optional)."""

from __future__ import annotations

import json
import logging
import smtplib
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from typing import Any

logger = logging.getLogger("quicdata.security.alert")


def _recent_persisted_alerts(*, limit: int, rule: str | None) -> list[dict[str, Any]]:
    """Read the durable alert history written through the security audit table."""
    import data.database as database

    db = None
    try:
        db = database.SessionLocal()
        query = (
            db.query(database.SecurityAuditEvent, database.User.email)
            .outerjoin(database.User, database.User.id == database.SecurityAuditEvent.actor_id)
            .filter(database.SecurityAuditEvent.action == "security.alert.dispatch")
            .order_by(
                database.SecurityAuditEvent.occurred_at.desc(),
                database.SecurityAuditEvent.id.desc(),
            )
        )
        rows = query.limit(limit * 4).all()
        items: list[dict[str, Any]] = []
        for row, actor in rows:
            detail = dict(row.detail_json or {})
            item_rule = str(detail.get("rule") or "")
            if rule and item_rule != rule:
                continue
            resource = None
            if row.resource_type and row.resource_id:
                resource = f"{row.resource_type}:{row.resource_id}"
            elif row.resource_id:
                resource = str(row.resource_id)
            items.append(
                {
                    "rule": item_rule,
                    "severity": str(detail.get("severity") or "high"),
                    "summary": str(detail.get("summary") or ""),
                    "ts": int(row.occurred_at.timestamp()),
                    "actor": actor,
                    "resource": resource,
                    "detail": dict(detail.get("alert_detail") or {}),
                    "channels": dict(detail.get("channels") or {}),
                }
            )
            if len(items) >= limit:
                break
        return items
    except Exception as exc:
        logger.warning("security alert audit query failed: %s", type(exc).__name__)
        return []
    finally:
        if db is not None:
            try:
                db.close()
            except Exception as exc:
                logger.warning("security alert audit session close failed: %s", type(exc).__name__)


def list_recent_alerts(*, limit: int = 50, rule: str | None = None) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    return _recent_persisted_alerts(limit=limit, rule=rule)


def _send_webhook(url: str, payload: dict[str, Any], timeout: float = 5.0) -> bool:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            return 200 <= getattr(resp, "status", 200) < 300
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("security alert webhook failed: %s", exc)
        return False


def _send_email(
    *,
    to_addr: str,
    subject: str,
    body: str,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    smtp_from: str,
    use_tls: bool = True,
) -> bool:
    if not to_addr or not smtp_host:
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_from or smtp_user or "quicdata-security@localhost"
    msg["To"] = to_addr
    msg.set_content(body)
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
            if use_tls:
                smtp.starttls()
            if smtp_user:
                smtp.login(smtp_user, smtp_password)
            smtp.send_message(msg)
        return True
    except Exception as exc:
        logger.warning("security alert email failed: %s", exc)
        return False


def dispatch_alert(
    rule: str,
    *,
    severity: str = "high",
    summary: str,
    detail: dict[str, Any] | None = None,
    actor: str | None = None,
    resource: str | None = None,
) -> dict[str, Any]:
    """Dispatch a security alert: structured logging + audit record + optional webhook/email."""
    from data.config import settings

    payload: dict[str, Any] = {
        "rule": rule,
        "severity": severity,
        "summary": summary,
        "ts": int(time.time()),
        "actor": actor,
        "resource": resource,
        "detail": detail or {},
        "channels": {"log": True, "webhook": False, "email": False},
    }

    line = json.dumps(payload, ensure_ascii=False, default=str)
    log_fn = logger.error if severity in ("critical", "high") else logger.warning
    log_fn("SECURITY_ALERT %s", line)

    webhook = (getattr(settings, "security_alert_webhook_url", "") or "").strip()
    if webhook:
        payload["channels"]["webhook"] = _send_webhook(webhook, payload)

    email_to = (getattr(settings, "security_alert_email", "") or "").strip()
    if email_to:
        payload["channels"]["email"] = _send_email(
            to_addr=email_to,
            subject=f"[QuicData Security][{severity}] {rule}",
            body=json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            smtp_host=(getattr(settings, "security_alert_smtp_host", "") or "").strip(),
            smtp_port=int(getattr(settings, "security_alert_smtp_port", 587) or 587),
            smtp_user=(getattr(settings, "security_alert_smtp_user", "") or "").strip(),
            smtp_password=(getattr(settings, "security_alert_smtp_password", "") or "").strip(),
            smtp_from=(getattr(settings, "security_alert_smtp_from", "") or "").strip(),
            use_tls=bool(getattr(settings, "security_alert_smtp_tls", True)),
        )

    # Durable audit rows make the admin view consistent across API workers.
    from data.security.audit import emit_audit_event

    emit_audit_event(
        "security.alert.dispatch",
        actor=actor,
        resource=resource,
        detail={
            "rule": rule,
            "severity": severity,
            "summary": summary,
            "alert_detail": detail or {},
            "channels": payload["channels"],
        },
        level="error" if severity in ("critical", "high") else "warning",
        evaluate_anomaly=False,
    )
    return payload
