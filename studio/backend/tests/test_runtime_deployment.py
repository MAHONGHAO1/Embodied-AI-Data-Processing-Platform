from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _create_clean_tag_repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "tag-repository"
    (repo / "scripts").mkdir(parents=True)
    (repo / "backend").mkdir()
    (repo / "frontend").mkdir()
    qrdf_root = repo / "backend" / "vendor" / "qrdf"
    (qrdf_root / "qrdf").mkdir(parents=True)
    shutil.copy2(
        ROOT / "scripts" / "deployment-image-tag.sh",
        repo / "scripts" / "deployment-image-tag.sh",
    )
    shutil.copy2(ROOT / "Makefile", repo / "Makefile")
    shutil.copy2(ROOT / ".gitignore", repo / ".gitignore")
    (repo / "backend" / "app.py").write_text("APP_VERSION = 1\n", encoding="utf-8")
    (repo / "frontend" / "app.js").write_text("export {};\n", encoding="utf-8")
    (repo / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
    (qrdf_root / "pyproject.toml").write_text("[project]\nname = 'qrdf'\n", encoding="utf-8")
    (qrdf_root / "qrdf" / "__init__.py").write_text("QRDF_VERSION = 'test'\n", encoding="utf-8")

    for command in (
        ["git", "init", "--quiet"],
        ["git", "config", "user.email", "tests@example.invalid"],
        ["git", "config", "user.name", "Deployment Tests"],
    ):
        subprocess.run(command, cwd=repo, check=True, capture_output=True, text=True)

    for command in (
        ["git", "init", "--quiet"],
        ["git", "config", "user.email", "tests@example.invalid"],
        ["git", "config", "user.name", "QRDF Fixture"],
        ["git", "add", "."],
        ["git", "commit", "--quiet", "-m", "test QRDF revision"],
    ):
        subprocess.run(command, cwd=qrdf_root, check=True, capture_output=True, text=True)

    qrdf_revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=qrdf_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    shutil.rmtree(qrdf_root / ".git")

    for command in (
        ["git", "add", "."],
        [
            "git",
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{qrdf_revision},backend/vendor/qrdf",
        ],
        ["git", "commit", "--quiet", "-m", "test deployment revision"],
    ):
        subprocess.run(command, cwd=repo, check=True, capture_output=True, text=True)

    revision = subprocess.run(
        ["git", "rev-parse", "--verify", "--short=12", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repo, revision


def _read_deployment_image_tag(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/deployment-image-tag.sh"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def test_production_compose_isolates_state_broker_and_realtime_dispatcher():
    compose = yaml.safe_load((ROOT / "deploy/docker-compose.prod.yml").read_text())
    services = compose["services"]

    assert {"redis-state", "redis-broker", "realtime-dispatcher"} <= set(services)
    assert "redis" not in services
    for name in ("redis-state", "redis-broker"):
        command = "\n".join(services[name]["command"])
        assert "maxmemory-policy noeviction" in command
        assert "auto-aof-rewrite-percentage" in command
        assert services[name]["healthcheck"]

    app_environment = services["migrate"]["environment"]
    assert app_environment["REALTIME_DISPATCHER_IN_API"].endswith(":-false}")
    assert services["realtime-dispatcher"]["command"] == [
        "python",
        "-m",
        "scripts.run_realtime_dispatcher",
    ]


def test_backup_script_dry_run_is_parameterized_and_rejects_unsafe_paths():
    script = ROOT / "scripts/backup-deployment.sh"
    command = [
        "bash",
        str(script),
        "--ssh-target",
        "ecs-uat",
        "--deploy-dir",
        "/opt/quicdata/uat/quicdata",
        "--compose-project",
        "quicstudio-uat",
        "--retention-days",
        "7",
        "--dry-run",
    ]
    dry_run = subprocess.run(command, check=True, capture_output=True, text=True)

    assert "ecs-uat" in dry_run.stdout
    assert "/opt/quicdata/uat/quicdata" in dry_run.stdout
    assert "pg_dump" in dry_run.stdout
    assert "password" not in dry_run.stdout.lower()

    unsafe = subprocess.run(
        [
            "bash",
            str(script),
            "--ssh-target",
            "ecs-uat",
            "--deploy-dir",
            "/opt/quicdata/../production",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )
    assert unsafe.returncode != 0


def test_backup_script_finalizes_remote_backup_when_pg_dump_is_non_interactive(tmp_path):
    deploy_dir = tmp_path / "deployment"
    deploy_inputs = deploy_dir / "deploy"
    deploy_inputs.mkdir(parents=True)
    (deploy_inputs / ".env").write_text("", encoding="utf-8")
    (deploy_inputs / "docker-compose.prod.yml").write_text("services: {}\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
[[ "$1" == "--" ]]
shift
ssh_target="$1"
shift
exec "$@"
""",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
arguments=" $* "
[[ "$arguments" == *" exec "* && "$arguments" == *" pg_dump "* ]] || {
  echo "unexpected docker invocation: $*" >&2
  exit 2
}
if [[ "$arguments" != *" --interactive=false "* ]]; then
  cat >/dev/null
fi
printf 'fake-pg-dump\\n'
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "backup-deployment.sh"),
            "--ssh-target",
            "ecs-test",
            "--deploy-dir",
            str(deploy_dir),
            "--compose-project",
            "quicdata-test",
            "--retention-days",
            "7",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "backup_created file=" in result.stdout
    backups = list((deploy_inputs / "runtime" / "backups").glob("*.dump"))
    assert len(backups) == 1
    backup = backups[0]
    assert backup.read_bytes() == b"fake-pg-dump\n"
    checksum = backup.with_suffix(".dump.sha256")
    assert checksum.exists()
    digest = hashlib.sha256(backup.read_bytes()).hexdigest()
    assert checksum.read_text(encoding="utf-8").startswith(f"{digest}  ")
    assert backup.name in checksum.read_text(encoding="utf-8")


def test_deployment_image_tag_tracks_the_checked_out_git_revision(tmp_path):
    repo, revision = _create_clean_tag_repository(tmp_path)
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    tag = _read_deployment_image_tag(repo)
    assert tag.returncode == 0, tag.stderr
    assert re.fullmatch(rf"git-{revision}-qrdf-[0-9a-f]{{12}}", tag.stdout.strip())

    make_tag = subprocess.run(
        ["make", "--silent", "prod-image-tag"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert make_tag.returncode == 0, make_tag.stderr
    assert make_tag.stdout.strip() == tag.stdout.strip()
    assert (
        "override PROD_IMAGE_TAG := $(if $(filter prod-% oss-browser-cors-%,$(MAKECMDGOALS)),$(shell bash scripts/deployment-image-tag.sh),)"
        in makefile
    )


def test_deployment_image_tag_includes_qrdf_vendor_content_identity(tmp_path):
    repo, revision = _create_clean_tag_repository(tmp_path)
    initial_tag = _read_deployment_image_tag(repo)

    assert initial_tag.returncode == 0, initial_tag.stderr
    assert re.fullmatch(rf"git-{revision}-qrdf-[0-9a-f]{{12}}", initial_tag.stdout.strip())

    cache_file = (
        repo / "backend" / "vendor" / "qrdf" / "qrdf" / "__pycache__" / "__init__.cpython-311.pyc"
    )
    cache_file.parent.mkdir()
    cache_file.write_bytes(b"not part of the Docker build context")
    cached_tag = _read_deployment_image_tag(repo)

    assert cached_tag.returncode == 0, cached_tag.stderr
    assert cached_tag.stdout.strip() == initial_tag.stdout.strip()

    qrdf_file = repo / "backend" / "vendor" / "qrdf" / "qrdf" / "__init__.py"
    qrdf_file.write_text("QRDF_VERSION = 'changed'\n", encoding="utf-8")
    changed_tag = _read_deployment_image_tag(repo)

    assert changed_tag.returncode == 0, changed_tag.stderr
    assert changed_tag.stdout.strip() != initial_tag.stdout.strip()


def test_deployment_image_tag_rejects_dirty_tracked_build_inputs(tmp_path):
    repo, _ = _create_clean_tag_repository(tmp_path)
    (repo / "backend" / "app.py").write_text("APP_VERSION = 2\n", encoding="utf-8")

    tag = _read_deployment_image_tag(repo)

    assert tag.returncode == 2
    assert "deployment build inputs are dirty" in tag.stderr


def test_deployment_image_tag_rejects_untracked_build_inputs_but_allows_unrelated_files(
    tmp_path,
):
    repo, revision = _create_clean_tag_repository(tmp_path)
    untracked_build_input = repo / "frontend" / "scratch.js"
    untracked_build_input.write_text("export const scratch = true;\n", encoding="utf-8")

    dirty_tag = _read_deployment_image_tag(repo)

    assert dirty_tag.returncode == 2
    assert "deployment build inputs are dirty" in dirty_tag.stderr

    untracked_build_input.unlink()
    (repo / "docs").mkdir()
    (repo / "docs" / "local-note.md").write_text("local only\n", encoding="utf-8")

    clean_tag = _read_deployment_image_tag(repo)

    assert clean_tag.returncode == 0, clean_tag.stderr
    assert re.fullmatch(rf"git-{revision}-qrdf-[0-9a-f]{{12}}", clean_tag.stdout.strip())


def test_uat_deployment_passes_the_git_tag_to_docker_compose():
    script = (ROOT / "scripts" / "uat-up.sh").read_text(encoding="utf-8")

    assert 'image_tag="$(bash "$ROOT/scripts/deployment-image-tag.sh")"' in script
    assert 'export QUICSTUDIO_IMAGE_TAG="$image_tag"' in script
    assert "compose=(docker compose" in script


def test_production_cors_preflight_inherits_the_revision_image_tag(tmp_path):
    repo, _ = _create_clean_tag_repository(tmp_path)
    tag = _read_deployment_image_tag(repo)
    assert tag.returncode == 0, tag.stderr
    result = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "-n",
            "oss-browser-cors-check",
            "PROD_ENV_FILE=deploy/.env.example",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (
        f'QUICSTUDIO_IMAGE_TAG="{tag.stdout.strip()}" bash scripts/oss-browser-cors.sh'
        in result.stdout
    )

    cors_script = (ROOT / "scripts" / "oss-browser-cors.sh").read_text(encoding="utf-8")
    assert 'image_tag="$(bash "$SCRIPT_DIR/deployment-image-tag.sh")"' in cors_script
    assert 'export QUICSTUDIO_IMAGE_TAG="$image_tag"' in cors_script
