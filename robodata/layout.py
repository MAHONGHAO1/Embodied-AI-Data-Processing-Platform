"""One place for local storage roots.

Existing environment variables keep working. New code (queue, status, GC) uses
this layout so a future team edition can swap the local implementation without
changing the contracts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .config import PROJECT_ROOT


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser().resolve() if value else default.resolve()


@dataclass(frozen=True)
class Layout:
    home: Path
    business: Path
    runs: Path
    queue: Path
    registry: Path
    exports: Path
    samples: Path
    reports: Path
    cache: Path

    @classmethod
    def current(cls, business_root=None, runs_root=None) -> "Layout":
        home = _env_path("ROBODATA_HOME", PROJECT_ROOT / "work")
        business = (Path(business_root).expanduser().resolve() if business_root
                    else _env_path("ROBODATA_BUSINESS_ROOT", home / "business"))
        runs = (Path(runs_root).expanduser().resolve() if runs_root
                else _env_path("ROBODATA_RUNS_ROOT", home / "runs"))
        return cls(home=home, business=business, runs=runs, queue=business / "queue",
                   registry=business / "registry.sqlite3", exports=home / "exports",
                   samples=home / "samples", reports=PROJECT_ROOT / "reports",
                   cache=home / "cache")

    def ensure(self) -> "Layout":
        for path in (self.home, self.business, self.runs, self.queue, self.exports,
                     self.samples, self.cache):
            path.mkdir(parents=True, exist_ok=True)
        return self
