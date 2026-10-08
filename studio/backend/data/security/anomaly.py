"""Security anomaly rules: brute force, unauthorized access spikes, bulk downloads, abnormal exports.

Rules are based on audit event sliding window counts; when triggered, alerts.dispatch_alert is invoked and written back to audit events.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from data.security.alerts import dispatch_alert
from data.security.rate_limit import record_hit

logger = logging.getLogger("quicdata.security.anomaly")

# Cooldown for the same actor+rule to prevent alert storms
_cooldown_lock = threading.Lock()
_last_alert_ts: dict[str, int] = {}
_DEFAULT_COOLDOWN = 300

ANOMALY_EVENTS = frozenset(
    {
        "security.anomaly.brute_force",
        "security.anomaly.authz_burst",
        "security.anomaly.bulk_download",
        "security.anomaly.export_abuse",
        "security.rate_limit.block",
    }
)


def clear_anomaly_cooldown_for_tests() -> None:
    with _cooldown_lock:
        _last_alert_ts.clear()


def _settings_int(name: str, default: int) -> int:
    from data.config import settings

    try:
        return int(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        return default


def _cooldown_ok(key: str, *, cooldown_seconds: int = _DEFAULT_COOLDOWN) -> bool:
    import time

    now = int(time.time())
    with _cooldown_lock:
        last = _last_alert_ts.get(key, 0)
        if now - last < cooldown_seconds:
            return False
        _last_alert_ts[key] = now
        return True


def _fire(
    event: str,
    *,
    rule: str,
    summary: str,
    actor: str | None,
    resource: str | None,
    detail: dict[str, Any],
    severity: str = "high",
) -> dict[str, Any] | None:
    cooldown_key = f"{rule}:{actor or 'unknown'}"
    if not _cooldown_ok(cooldown_key):
        return None

    alert = dispatch_alert(
        rule,
        severity=severity,
        summary=summary,
        detail=detail,
        actor=actor,
        resource=resource,
    )
    # Deferred import to prevent circular dependency with audit.emit
    from data.security.audit import emit_audit_event

    emit_audit_event(
        event,
        actor=actor,
        resource=resource,
        detail={**detail, "alert_rule": rule, "alert_ts": alert.get("ts")},
        level="error" if severity in ("high", "critical") else "warning",
        evaluate_anomaly=False,
    )
    return alert


def evaluate_audit_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Evaluate anomaly rules against a newly emitted audit event. Returns the triggered alert or None."""
    event = str(payload.get("event") or "")
    if event in ANOMALY_EVENTS or event.startswith("security.alert"):
        return None

    actor = payload.get("actor") or "unknown"
    resource = payload.get("resource")

    if event == "auth.login.fail":
        threshold = _settings_int("security_login_fail_threshold", 5)
        window = _settings_int("security_login_fail_window_seconds", 300)
        count = record_hit(f"login_fail:{actor}", window_seconds=window)
        if count >= threshold:
            return _fire(
                "security.anomaly.brute_force",
                rule="brute_force",
                summary=f"登录失败次数超阈值: {actor} ({count}/{threshold})",
                actor=str(actor),
                resource=resource,
                detail={"count": count, "threshold": threshold, "window_seconds": window},
                severity="high",
            )

    if event == "security.authz.denied":
        threshold = _settings_int("security_authz_denied_threshold", 20)
        window = _settings_int("security_authz_denied_window_seconds", 300)
        count = record_hit(f"authz_denied:{actor}", window_seconds=window)
        if count >= threshold:
            return _fire(
                "security.anomaly.authz_burst",
                rule="authz_burst",
                summary=f"越权拒绝突发: {actor} ({count}/{threshold})",
                actor=str(actor),
                resource=resource,
                detail={"count": count, "threshold": threshold, "window_seconds": window},
                severity="high",
            )

    if event in ("file.download", "export.download"):
        threshold = _settings_int("security_bulk_download_threshold", 30)
        window = _settings_int("security_bulk_download_window_seconds", 300)
        count = record_hit(f"download:{actor}", window_seconds=window)
        if count >= threshold:
            return _fire(
                "security.anomaly.bulk_download",
                rule="bulk_download",
                summary=f"批量下载异常: {actor} ({count}/{threshold})",
                actor=str(actor),
                resource=resource,
                detail={
                    "count": count,
                    "threshold": threshold,
                    "window_seconds": window,
                    "trigger_event": event,
                },
                severity="high",
            )

    if event == "export.create":
        threshold = _settings_int("security_export_create_threshold", 10)
        window = _settings_int("security_export_create_window_seconds", 600)
        count = record_hit(f"export_create:{actor}", window_seconds=window)
        if count >= threshold:
            return _fire(
                "security.anomaly.export_abuse",
                rule="export_abuse",
                summary=f"异常导出创建频率: {actor} ({count}/{threshold})",
                actor=str(actor),
                resource=resource,
                detail={"count": count, "threshold": threshold, "window_seconds": window},
                severity="high",
            )

    return None
