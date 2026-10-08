#!/usr/bin/env python3
"""Explicitly bind and queue bounded historical native LeRobot copies."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data.bootstrap  # noqa: F401
from data.database import SessionLocal
from data.runtime import assert_schema_current
from data.services.native_lerobot_replication import (
    NativeLerobotBackfillError,
    backfill_native_lerobot_page,
)
from data.services.task_dispatcher import (
    JobDispatchUnavailable,
    dispatch_media_job,
    require_celery_worker,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bind one approved OSS scope to historical native LeRobot records."
    )
    parser.add_argument("--scope-id", type=int, required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--after-native-id", type=int, default=None)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check", action="store_true", help="inspect one page without reads or writes"
    )
    mode.add_argument(
        "--apply", action="store_true", help="bind and queue one page after marker revalidation"
    )
    parser.add_argument(
        "--queue-only",
        action="store_true",
        help="retain queued copy jobs without requiring or dispatching an export worker",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 1_000:
        parser.error("--limit must be between 1 and 1000")
    if args.after_native_id is not None and args.after_native_id < 0:
        parser.error("--after-native-id must be non-negative")
    if args.queue_only and not args.apply:
        parser.error("--queue-only requires --apply")
    if args.apply and not args.queue_only:
        try:
            require_celery_worker("export")
        except JobDispatchUnavailable:
            print("export worker is unavailable; no native copies were queued", file=sys.stderr)
            return 2

    assert_schema_current()
    db = SessionLocal()
    try:
        page = backfill_native_lerobot_page(
            db,
            scope_id=args.scope_id,
            limit=args.limit,
            after_native_id=args.after_native_id,
            apply=bool(args.apply),
        )
        if args.apply:
            db.commit()
        else:
            db.rollback()

        dispatched = 0
        if args.apply and not args.queue_only:
            for job in page.queued_jobs:
                dispatch_media_job(job, worker_prechecked=True)
                dispatched += 1
        print(
            json.dumps(
                {
                    "scanned": page.scanned_count,
                    "queued": len(page.queued_jobs),
                    "failed": page.failed_count,
                    "dispatched": dispatched,
                    "next_after_native_id": page.next_after_native_id,
                    "complete": page.next_after_native_id is None,
                },
                sort_keys=True,
            )
        )
        return 0
    except (NativeLerobotBackfillError, JobDispatchUnavailable):
        db.rollback()
        print(
            "native LeRobot backfill could not complete this page; durable queued jobs were retained",
            file=sys.stderr,
        )
        return 2
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
