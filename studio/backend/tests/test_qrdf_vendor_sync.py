from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _create_local_qrdf_source(tmp_path: Path) -> Path:
    source = tmp_path / "local-qrdf"
    _write(source / ".gitignore", "data/\n!examples/data/\n")
    _write(source / "pyproject.toml", "[project]\nname = 'qrdf'\n")
    _write(source / "qrdf" / "__init__.py", "QRDF_VERSION = 'test'\n")
    _write(source / "qrdf" / "version.py", 'QRDF_VERSION = "test"\n')
    _write(
        source / "qrdf" / "mcap" / "reader.py",
        "canonical_schema_name = 'test'\ndef get_canonical_schema_name(): return 'test'\n"
        "descriptor_format = 'test'\nis_legacy_schema = False\n",
    )
    _write(
        source / "qrdf" / "models" / "episode.py",
        "def resolve_data_file(): return None\n",
    )
    _write(
        source / "qrdf" / "registry" / "topic_layout.py",
        "EGO_EPISODE_TYPE_ALIASES = {}\n",
    )
    _write(source / "docs" / "published.md", "published\n")
    _write(source / "examples" / "data" / "sample.json", '{"sample": true}\n')

    for command in (
        ["git", "init", "--quiet"],
        ["git", "config", "user.email", "tests@example.invalid"],
        ["git", "config", "user.name", "QRDF Sync Tests"],
        ["git", "add", "."],
        ["git", "commit", "--quiet", "-m", "tracked QRDF source"],
    ):
        subprocess.run(command, cwd=source, check=True, capture_output=True, text=True)

    _write(source / ".git" / "info" / "exclude", "/docs/local-only.md\n")
    _write(source / "docs" / "local-only.md", "local engineering note\n")
    _write(source / "data" / "generated.txt", "ignored build output\n")
    return source


def test_local_qrdf_sync_excludes_git_ignored_files(tmp_path: Path) -> None:
    """Vendor sync must not leak local notes into the deployable SDK tree."""
    repo = tmp_path / "studio"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "fetch-qrdf.sh", repo / "scripts" / "fetch-qrdf.sh")
    vendor = repo / "backend" / "vendor" / "qrdf"
    vendor.mkdir(parents=True)
    (vendor / ".gitkeep").touch()
    _write(vendor / "docs" / "local-only.md", "stale local note\n")
    source = _create_local_qrdf_source(tmp_path)

    result = subprocess.run(
        ["bash", "scripts/fetch-qrdf.sh"],
        cwd=repo,
        env={**os.environ, "QRDF_LOCAL_PATH": str(source)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (vendor / ".gitkeep").exists()
    assert (vendor / "docs" / "published.md").exists()
    assert not (vendor / "docs" / "local-only.md").exists()
    assert (vendor / "examples" / "data" / "sample.json").exists()
    assert not (vendor / "data" / "generated.txt").exists()
