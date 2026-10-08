"""Backfill reliable legacy source groups in bounded, resumable database pages."""

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
from data.services.capture_provenance import backfill_episode_source_groups


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--after-episode-id", type=int, default=None)
    parser.add_argument("--all", action="store_true", help="process every bounded page")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="report eligible rows without writing")
    mode.add_argument(
        "--apply", action="store_true", help="persist only reliable legacy source groups"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.limit <= 1_000:
        _parser().error("--limit must be between 1 and 1000")
    if args.after_episode_id is not None and args.after_episode_id < 0:
        _parser().error("--after-episode-id must be non-negative")

    assert_schema_current()
    db = SessionLocal()
    try:
        cursor = args.after_episode_id
        scanned = 0
        eligible = 0
        updated = 0
        while True:
            page = backfill_episode_source_groups(
                db,
                limit=args.limit,
                after_episode_id=cursor,
                apply=bool(args.apply),
            )
            scanned += page.scanned_count
            eligible += page.eligible_count
            updated += page.updated_count
            if args.apply:
                db.commit()
            else:
                db.rollback()
            cursor = page.next_after_episode_id
            if not args.all or cursor is None:
                break
        print(
            json.dumps(
                {
                    "mode": "apply" if args.apply else "check",
                    "scanned": scanned,
                    "eligible": eligible,
                    "updated": updated,
                    "next_after_episode_id": cursor,
                    "complete": cursor is None,
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
