"""Operator-facing status: queue, runs, disk, and GC candidates."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .config import APP_VERSION
from .layout import Layout


def _run_summary(runs: Path) -> dict:
    counts: dict[str, int] = {}
    latest = None
    if not runs.is_dir():
        return {"counts": counts, "latest": None}
    for directory in sorted(runs.iterdir()):
        state_path = directory / "state.json"
        if not directory.is_dir() or not state_path.is_file():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        status = str(state.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
        latest = {"run_id": directory.name, "status": status,
                  "message": state.get("message"), "completed_at": state.get("completed_at")}
    return {"counts": counts, "latest": latest}


def status(business_root=None, runs_root=None) -> dict:
    from .gc import scan
    from .jobs import JobStore

    layout = Layout.current(business_root, runs_root).ensure()
    queue = JobStore(layout.business).summary()
    usage = shutil.disk_usage(layout.runs)
    gc_plan = scan(layout)
    return {
        "app": "RoboData Workbench",
        "version": APP_VERSION,
        "layout": {
            "home": str(layout.home), "business": str(layout.business), "runs": str(layout.runs),
            "queue": str(layout.queue), "registry": str(layout.registry),
        },
        "queue": queue,
        "runs": _run_summary(layout.runs),
        "disk": {"free_bytes": usage.free, "total_bytes": usage.total,
                 "used_bytes": usage.used, "free_gib": round(usage.free / 1024 ** 3, 2)},
        "gc": {"candidate_count": len(gc_plan["candidates"]),
               "candidate_bytes": sum(item["bytes"] for item in gc_plan["candidates"]),
               "protected_count": gc_plan["protected_count"]},
    }
