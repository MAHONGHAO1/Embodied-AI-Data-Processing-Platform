#!/usr/bin/env python3
"""Migrate explicit legacy child preview jobs to derived preview batches."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data.bootstrap  # noqa: F401
from data.database import SessionLocal
from data.runtime import assert_schema_current
from data.services.legacy_preview_migration import (
    LegacyPreviewMigrationError,
    LegacyPreviewMigrationTarget,
    migrate_legacy_preview_jobs,
)


def parse_source_review(value: str) -> LegacyPreviewMigrationTarget:
    parts = value.split(":")
    if len(parts) != 2 or not all(part.isascii() and part.isdecimal() for part in parts):
        raise argparse.ArgumentTypeError(
            "source/review must be positive integers: SOURCE_ID:REVIEW_ID"
        )
    source_id, review_id = (int(part) for part in parts)
    if source_id <= 0 or review_id <= 0:
        raise argparse.ArgumentTypeError(
            "source/review must be positive integers: SOURCE_ID:REVIEW_ID"
        )
    return LegacyPreviewMigrationTarget(
        source_episode_id=source_id,
        cut_review_work_item_id=review_id,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate explicit legacy derived preview jobs to batch jobs."
    )
    parser.add_argument(
        "--source-review",
        action="append",
        required=True,
        type=parse_source_review,
        metavar="SOURCE_ID:REVIEW_ID",
        help="explicit source Episode and accepted cut review pair; repeat as needed",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="validate and report without writing")
    mode.add_argument("--apply", action="store_true", help="atomically apply all explicit targets")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    targets = tuple(args.source_review)
    if len(set(targets)) != len(targets):
        parser.error("--source-review contains duplicate targets")

    assert_schema_current()
    db = SessionLocal()
    try:
        results = migrate_legacy_preview_jobs(db, targets=targets, apply=bool(args.apply))
    except LegacyPreviewMigrationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        db.close()
    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "check",
                "targets": [asdict(result) for result in results],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
