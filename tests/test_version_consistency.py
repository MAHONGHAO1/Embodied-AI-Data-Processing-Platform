"""The version chain must agree everywhere it is published.

The workbench writes ``APP_VERSION`` into every quality report. If that value
drifts from the README, the release tag or the frontend build, a report can
claim to come from a version that was never released. This test keeps the
chain honest; run it with ``CHECK_RELEASE_TAG=1`` on a tagged commit to also
check the Git tag.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _search(pattern: str, text: str, label: str) -> str:
    match = re.search(pattern, text, re.MULTILINE)
    assert match is not None, f"未在 {label} 中找到版本号"
    return match.group(1)


def collect_versions() -> dict[str, str]:
    readme = _read("README.md")
    pyproject = _read("pyproject.toml")
    config = _read("robodata/config.py")
    web = _read("robodata/web.py")
    frontend = json.loads(_read("frontend/package.json"))
    app = _read("frontend/src/App.tsx")
    evidence = _read("docs/EVIDENCE.md")

    return {
        "README 版本行": _search(r"^v(\d+\.\d+\.\d+) · Windows / Linux CI", readme, "README.md"),
        "README release 徽章": _search(
            r"release-v(\d+\.\d+\.\d+)-blue\.svg", readme, "README.md release 徽章"
        ),
        "README release 徽章链接": _search(
            r"releases/tag/v(\d+\.\d+\.\d+)\)", readme, "README.md release 徽章链接"
        ),
        "pyproject.toml": _search(
            r'^version\s*=\s*"(\d+\.\d+\.\d+)"', pyproject, "pyproject.toml"
        ),
        "robodata/config.py": _search(
            r'^APP_VERSION\s*=\s*"(\d+\.\d+\.\d+)"', config, "robodata/config.py"
        ),
        "robodata/web.py FastAPI": _search(
            r'FastAPI\([^)]*version="(\d+\.\d+\.\d+)"', web, "robodata/web.py"
        ),
        "robodata/web.py health": _search(
            r'"version": "(\d+\.\d+\.\d+)"', web, "robodata/web.py health"
        ),
        "frontend/package.json": frontend["version"],
        "frontend/src/App.tsx": _search(r"v(\d+\.\d+\.\d+) · React 工作台", app, "App.tsx"),
        "docs/EVIDENCE.md": _search(r"^# 证据总表 · v(\d+\.\d+\.\d+)", evidence, "docs/EVIDENCE.md"),
    }


def test_version_chain_agrees() -> None:
    versions = collect_versions()
    unique = set(versions.values())
    assert len(unique) == 1, "版本号不一致：" + "；".join(
        f"{label}={version}" for label, version in versions.items()
    )

    version = unique.pop()
    release_note = ROOT / "docs" / "releases" / f"RELEASE_NOTES_v{version}.md"
    assert release_note.exists(), (
        f"缺少 docs/releases/RELEASE_NOTES_v{version}.md；发布前先写发布说明"
    )


def test_latest_git_tag_matches_release_version() -> None:
    if os.environ.get("CHECK_RELEASE_TAG") != "1":
        pytest.skip("仅在发布校验时设置 CHECK_RELEASE_TAG=1")

    version = collect_versions()["robodata/config.py"]
    try:
        tag = subprocess.check_output(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.fail(f"无法读取 Git tag：{exc}")

    assert tag == f"v{version}", f"最新 tag 是 {tag}，代码版本是 v{version}"
