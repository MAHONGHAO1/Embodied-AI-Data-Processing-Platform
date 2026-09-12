"""Presentation helpers for the fixed HDF5 conversion; no training imports."""

from __future__ import annotations

import os
from pathlib import Path

from .config import PROJECT_ROOT
from .sources import HDF5_PATH
from . import ui_runs


def input_path() -> Path:
    return Path(os.environ.get("ROBODATA_HDF5_PATH", str(HDF5_PATH))).resolve()


def output_root() -> Path:
    return Path(os.environ.get("ROBODATA_EXPORT_ROOT", str(PROJECT_ROOT / "work" / "exports"))).resolve()


def environment_python() -> Path:
    return PROJECT_ROOT / "tools" / "conversion" / ".venv" / "Scripts" / "python.exe"


def registered_exports() -> list[dict]:
    from .conversion import list_exports

    return list_exports(output_root=output_root())


def latest_export() -> dict | None:
    from .conversion import latest_export as latest

    return latest(output_root=output_root())


def export_path(export: dict) -> Path:
    return Path(export.get("path", export.get("output_path", ""))).resolve()


def start_conversion() -> str:
    from .runtime import start_run

    return start_run({"kind": "conversion", "input_path": str(input_path()),
                      "output_root": str(output_root())}, runs_root=ui_runs.runs_root())
