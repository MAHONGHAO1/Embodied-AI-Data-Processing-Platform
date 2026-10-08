"""Deployment entry points must use the migrated data package."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_scripts_have_no_legacy_app_imports():
    for path in (ROOT / "scripts").iterdir():
        if path.suffix not in {".sh", ".ps1", ".py"}:
            continue
        assert not re.search(r"\b(?:from|import) app\.", path.read_text()), path.name


def test_make_frontend_is_independent_of_backend():
    result = subprocess.run(
        ["make", "-n", "dev-frontend"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "frontend/serve_preview.py" in result.stdout
    assert "--port" in result.stdout and "8090" in result.stdout
    assert "docker compose" not in result.stdout
    assert "deployment build inputs" not in result.stderr


def test_managed_frontend_commands_use_the_screen_lifecycle():
    preview = subprocess.run(
        ["make", "-n", "dev-frontend-up"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    mock = subprocess.run(
        ["make", "-n", "dev-mock-up"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert preview.returncode == 0, preview.stderr
    assert "FRONTEND_MOCK=false" in preview.stdout
    assert "scripts/dev-screen.sh restart frontend" in preview.stdout
    assert mock.returncode == 0, mock.stderr
    assert "FRONTEND_MOCK=true" in mock.stdout
    assert "scripts/dev-screen.sh restart frontend" in mock.stdout

    screen_script = (ROOT / "scripts" / "dev-screen.sh").read_text(encoding="utf-8")
    assert "SERVICES=(api frontend" in screen_script
    assert "quicstudio-dev-frontend" in screen_script
