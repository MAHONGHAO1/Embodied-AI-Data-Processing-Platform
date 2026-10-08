#!/usr/bin/env python3
"""Verify PAI-DSW SDK access, then optionally stop instances to free DLC quota.

Example (verify 4090 pair, then stop):
  python scripts/dsw_verify_then_stop.py --family 4090 --stop

H20 family is protected: --stop --family h20 is refused unless
--force-h20 is also passed (and QUICTRAIN_ALLOW_H20_DSW_STOP=true).

Never pass dsw-* as DLC CreateJob ResourceId — use quota* IDs instead.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--family", choices=("4090", "h20", "all"), default="4090")
    parser.add_argument("--stop", action="store_true", help="Stop verified instances after probe")
    parser.add_argument(
        "--force-h20",
        action="store_true",
        help="Required together with QUICTRAIN_ALLOW_H20_DSW_STOP=true to stop H20 DSW",
    )
    parser.add_argument(
        "--ids",
        default="",
        help="Comma-separated instance IDs (overrides family catalog)",
    )
    args = parser.parse_args()

    sys.path[:0] = [
        os.path.join(os.path.dirname(__file__), "..", "apps", "api", "src"),
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "packages",
            "provider-aliyun-dlc",
            "src",
        ),
    ]

    from quictrain_api.dsw_ops import (  # noqa: E402
        get_dsw_instance,
        parse_known_dsw_instances,
        stop_dsw_instance,
        verify_dsw_instances,
    )
    from quictrain_api.errors import ServiceError  # noqa: E402

    if args.ids.strip():
        ids = [item.strip() for item in args.ids.split(",") if item.strip()]
    elif args.family == "all":
        ids = [item["instance_id"] for item in parse_known_dsw_instances()]
    else:
        ids = [
            item["instance_id"]
            for item in parse_known_dsw_instances()
            if item["family"] == args.family
        ]

    if args.stop and args.family in {"h20", "all"} and not args.force_h20:
        print(
            json.dumps(
                {
                    "error": "H20_STOP_BLOCKED",
                    "message": (
                        "Refusing to stop H20 DSW / H20-5 resource group. "
                        "Use --family 4090 for Quota4090 only, or pass --force-h20 "
                        "with QUICTRAIN_ALLOW_H20_DSW_STOP=true."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 3

    report = verify_dsw_instances(ids)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report.get("verified"):
        return 2
    if not args.stop:
        return 0

    stopped = []
    for instance_id in ids:
        before = get_dsw_instance(instance_id)
        try:
            after = stop_dsw_instance(instance_id, force=args.force_h20)
        except ServiceError as exc:
            print(json.dumps({"error": exc.code, "message": str(exc)}, ensure_ascii=False))
            return 3
        stopped.append(
            {
                "instance_id": instance_id,
                "before": before.get("status"),
                "after": after.get("status"),
            }
        )
    print(json.dumps({"stopped": stopped}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
