"""Backend bootstrap: ensure vendored SDK packages are importable."""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_VENDOR_ROOT = _BACKEND_ROOT / "vendor" / "qrdf"
_QRDF_PACKAGE = _VENDOR_ROOT / "qrdf" / "__init__.py"


def _ensure_vendor_path() -> None:
    if _QRDF_PACKAGE.is_file():
        vendor_path = str(_VENDOR_ROOT)
        if vendor_path not in sys.path:
            sys.path.insert(0, vendor_path)


_ensure_vendor_path()


def qrdf_available() -> bool:
    try:
        import qrdf  # noqa: F401

        return True
    except ImportError:
        return False


def qrdf_vendor_hint() -> str:
    if qrdf_available():
        import qrdf

        return f"qrdf {qrdf.__version__} @ {_VENDOR_ROOT}"
    return f"QRDF not ready — run: bash scripts/fetch-qrdf.sh (expected {_QRDF_PACKAGE})"
