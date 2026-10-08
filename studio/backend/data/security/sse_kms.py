"""OSS Server-Side Encryption (SSE-KMS) extension interface.

Disabled by default; when enabled, upload paths include standard encryption headers to integrate with real bucket policies.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("quicdata.security")


class SseKmsPolicy:
    """Describe the expected OSS server-side encryption policy."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        algorithm: str = "KMS",
        kms_key_id: str = "",
        enforce: bool = False,
    ) -> None:
        self.enabled = enabled
        self.algorithm = algorithm  # KMS | AES256
        self.kms_key_id = kms_key_id
        self.enforce = (
            enforce  # When True, missing encryption headers should trigger rejection/alert
        )

    def build_headers(self) -> dict[str, str]:
        """Generate upload headers; returns empty dict if disabled."""
        if not self.enabled:
            return {}
        headers: dict[str, str] = {
            "x-oss-server-side-encryption": self.algorithm
            if self.algorithm in {"KMS", "AES256"}
            else "KMS",
        }
        if headers["x-oss-server-side-encryption"] == "KMS" and self.kms_key_id:
            headers["x-oss-server-side-encryption-key-id"] = self.kms_key_id
        return headers

    def validate_object_meta(self, headers: dict[str, Any] | None) -> bool:
        """Validate whether object metadata complies with encryption policy (placeholder: fails only on enforce without headers)."""
        if not self.enabled or not self.enforce:
            return True
        if not headers:
            return False
        lower = {str(k).lower(): str(v) for k, v in headers.items()}
        return "x-oss-server-side-encryption" in lower

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "algorithm": self.algorithm,
            "kms_key_id": self.kms_key_id or None,
            "enforce": self.enforce,
            "headers_preview": self.build_headers(),
        }


def get_sse_kms_policy() -> SseKmsPolicy:
    from data.config import settings

    return SseKmsPolicy(
        enabled=bool(getattr(settings, "oss_sse_kms_enabled", False)),
        algorithm=str(getattr(settings, "oss_sse_algorithm", "KMS") or "KMS"),
        kms_key_id=str(getattr(settings, "oss_sse_kms_key_id", "") or ""),
        enforce=bool(getattr(settings, "oss_sse_kms_enforce", False)),
    )
