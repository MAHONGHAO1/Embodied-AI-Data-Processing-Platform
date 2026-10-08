"""Platform security: redaction, secrets, auditing, signed downloads, and anomaly alerts."""

from data.security.alerts import dispatch_alert, list_recent_alerts
from data.security.audit import emit_audit_event, list_recent_audit_events
from data.security.redact import redact_secrets
from data.security.secrets import KeyManager, SecretProvider, get_key_manager
from data.security.sse_kms import get_sse_kms_policy

__all__ = [
    "KeyManager",
    "SecretProvider",
    "dispatch_alert",
    "emit_audit_event",
    "get_key_manager",
    "get_sse_kms_policy",
    "list_recent_alerts",
    "list_recent_audit_events",
    "redact_secrets",
]
