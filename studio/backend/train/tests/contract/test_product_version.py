"""Product version surfaces must stay aligned at 1.0.0 (V1 release tag)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import tomllib
from quictrain_core import __version__

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = "1.0.0"


def _package_json_version(path: Path) -> str:
    return json.loads(path.read_text(encoding="utf-8"))["version"]


def test_canonical_python_version() -> None:
    assert __version__ == EXPECTED


def test_root_version_file() -> None:
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == EXPECTED


def test_pyproject_project_version() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == EXPECTED
    assert "dynamic" not in data["project"] or "version" not in data["project"].get("dynamic", [])


def test_npm_package_versions() -> None:
    assert _package_json_version(ROOT / "package.json") == EXPECTED
    assert _package_json_version(ROOT / "apps/web/package.json") == EXPECTED


def test_fastapi_app_version_uses_canonical_constant() -> None:
    source = (ROOT / "src/quictrain_api/main.py").read_text(encoding="utf-8")
    assert "from quictrain_core.version import __version__ as QUICTRAIN_VERSION" in source
    assert "version=QUICTRAIN_VERSION" in source
    assert re.search(r'version\s*=\s*["\']0\.1\.0["\']', source) is None
