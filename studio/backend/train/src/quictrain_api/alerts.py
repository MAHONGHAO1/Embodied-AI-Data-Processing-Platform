"""Alert emission + SLS retention reservation (V1 ops floor)."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

from quictrain_api.settings import get_settings

LOGGER = logging.getLogger(__name__)


def emit_alert(
    *,
    severity: str,
    title: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fire webhook when configured; otherwise record a structured no-op for tests."""

    settings = get_settings()
    payload = {
        "severity": severity,
        "title": title,
        "details": details or {},
        "emitted_at": datetime.now(UTC).isoformat(),
        "retention_days": settings.log_retention_days,
        "sls_project": settings.sls_project,
        "sls_logstore": settings.sls_logstore,
    }
    webhook = settings.alerts_webhook_url
    if not webhook:
        LOGGER.info("alert_suppressed_no_webhook %s", json.dumps(payload, ensure_ascii=False))
        return {**payload, "delivered": False, "reason": "ALERTS_WEBHOOK_UNCONFIGURED"}

    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        webhook,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310  # nosec B310 - configured webhook
            status = getattr(response, "status", 200)
        return {**payload, "delivered": True, "http_status": status}
    except (urllib.error.URLError, TimeoutError) as exc:
        LOGGER.warning("alert_delivery_failed: %s", exc)
        return {**payload, "delivered": False, "reason": str(exc)}


def sls_reservation_status() -> dict[str, Any]:
    """Documented reservation until SLS project/credentials are provisioned."""

    settings = get_settings()
    ready = bool(settings.sls_project and settings.sls_logstore)
    return {
        "configured": ready,
        "project": settings.sls_project,
        "logstore": settings.sls_logstore,
        "retention_days": settings.log_retention_days,
        "status": "ready" if ready else "RESERVED_PENDING_SLS",
        "message": (
            "SLS sink configured; ship control-plane/audit logs via agent."
            if ready
            else (
                "Set QUICTRAIN_SLS_PROJECT and QUICTRAIN_SLS_LOGSTORE; "
                "evidence under docs/evidence/ops/."
            ),
        ),
    }
