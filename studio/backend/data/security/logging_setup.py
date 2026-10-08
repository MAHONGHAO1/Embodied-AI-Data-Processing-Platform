"""Application log security filter mounting."""

from __future__ import annotations

import logging

from data.security.redact import SecretFilter

_FILTER_ATTACHED = False


def attach_secret_log_filter() -> None:
    """Attach SecretFilter to root and quicdata.* loggers to prevent secrets from leaking into logs."""
    global _FILTER_ATTACHED
    if _FILTER_ATTACHED:
        return
    filt = SecretFilter()
    root = logging.getLogger()
    root.addFilter(filt)
    for name in (
        "quicdata",
        "quicdata.audit",
        "quicdata.security",
        "quicdata.config",
        "quicdata.http",
        "uvicorn",
        "uvicorn.error",
    ):
        logging.getLogger(name).addFilter(filt)
    # Gunicorn can leave the root logger at WARNING. Request boundaries are
    # operationally useful at INFO, while SecretFilter still sanitizes output.
    logging.getLogger("quicdata.http").setLevel(logging.INFO)
    # Ensure at least one handler exists so filter stays on logger even when pytest/certain entrypoints produce no output
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.addFilter(filt)
        root.addHandler(handler)
    else:
        for handler in root.handlers:
            handler.addFilter(filt)
    _FILTER_ATTACHED = True
