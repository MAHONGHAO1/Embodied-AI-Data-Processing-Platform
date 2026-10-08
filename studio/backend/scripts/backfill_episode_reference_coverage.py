"""Queue bounded V2 quality checks for source reference-camera coverage."""

from __future__ import annotations

import argparse
import json
import sys

from data.database import SessionLocal
from data.services.import_parser import enqueue_episode_reference_coverage_backfill
from data.services.task_dispatcher import (
    JobDispatchUnavailable,
    dispatch_media_job,
    require_celery_worker,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Queue V2 reference coverage checks for ready source Episodes."
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--after-episode-id", type=int, default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--queue-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 1000:
        parser.error("--limit must be between 1 and 1000")
    if args.after_episode_id is not None and args.after_episode_id < 0:
        parser.error("--after-episode-id must be non-negative")
    if not args.queue_only:
        try:
            require_celery_worker("media")
        except JobDispatchUnavailable:
            print("media worker is unavailable; no quality jobs were queued", file=sys.stderr)
            return 2

    db = SessionLocal()
    try:
        cursor = args.after_episode_id
        queued = 0
        dispatched = 0
        scanned = 0
        while True:
            page = enqueue_episode_reference_coverage_backfill(
                db,
                limit=args.limit,
                after_episode_id=cursor,
            )
            scanned += page.scanned_count
            queued += len(page.jobs)
            if not args.queue_only:
                for job in page.jobs:
                    dispatch_media_job(job, worker_prechecked=True)
                    dispatched += 1
            cursor = page.next_after_episode_id
            if not args.all or cursor is None:
                break
        print(
            json.dumps(
                {
                    "scanned": scanned,
                    "queued": queued,
                    "dispatched": dispatched,
                    "queue_only": bool(args.queue_only),
                    "next_after_episode_id": cursor,
                    "complete": cursor is None,
                },
                sort_keys=True,
            )
        )
        return 0
    except JobDispatchUnavailable:
        print("media dispatch became unavailable; queued jobs were retained", file=sys.stderr)
        return 2
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
