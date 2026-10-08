#!/usr/bin/env python3
"""Runtime secret migration utility.

Local envelope:
  python -m scripts.migrate_secrets --kind local [--apply]

Cloud KMS (placeholder for future extension):
  python -m scripts.migrate_secrets --kind cloud_kms
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data.bootstrap  # noqa: F401
from data.security.logging_setup import attach_secret_log_filter
from data.security.migration import get_migration_tool


def main() -> int:
    attach_secret_log_filter()
    parser = argparse.ArgumentParser(description="Migrate QuicData runtime secrets")
    parser.add_argument("--kind", default="local", choices=["local", "cloud_kms"])
    parser.add_argument(
        "--apply", action="store_true", help="actually write changes (default dry-run)"
    )
    args = parser.parse_args()

    tool = get_migration_tool(args.kind)
    result = tool.migrate(dry_run=not args.apply)
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    return 0 if result.ok or args.kind == "cloud_kms" else 1


if __name__ == "__main__":
    raise SystemExit(main())
