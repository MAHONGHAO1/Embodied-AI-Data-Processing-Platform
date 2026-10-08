"""Fail AI worker startup when its enabled Embodied VL SDK is unavailable."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from data.config import settings
from data.integrations.embodied_vl.client_loader import load_embodied_vl_client


def verify_when_enabled() -> int:
    """Validate the SDK only when this process is allowed to call the provider."""

    if not settings.behavior_ai_can_call_provider:
        return 0
    if not _has_value(settings.dashscope_api_key) or not _has_value(settings.dashscope_app_id):
        print(
            "Embodied VL SDK preflight failed: provider credentials are unavailable",
            file=sys.stderr,
        )
        return 1
    try:
        load_embodied_vl_client(
            settings.embodied_vl_sdk_dir,
            settings.embodied_vl_checksums_file,
        )
    except Exception:
        print("Embodied VL SDK preflight failed: SDK is unavailable", file=sys.stderr)
        return 1
    return 0


def _has_value(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--when-enabled",
        action="store_true",
        help="only validate when behavior AI outbound calls are enabled",
    )
    arguments = parser.parse_args(argv)
    if arguments.when_enabled:
        return verify_when_enabled()
    parser.error("--when-enabled is required")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
