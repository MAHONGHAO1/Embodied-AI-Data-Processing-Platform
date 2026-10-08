#!/usr/bin/env python3
"""Apply QuicData Alembic revisions without starting an API worker."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data.bootstrap  # noqa: F401
from data.runtime import current_revision, upgrade_database


def main() -> int:
    parser = argparse.ArgumentParser(description="Upgrade QuicData database schema")
    parser.add_argument("--revision", default="head")
    args = parser.parse_args()

    upgrade_database(revision=args.revision)
    print(f"database revision: {current_revision()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
