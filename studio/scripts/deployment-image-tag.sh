#!/usr/bin/env bash
# Print the revision-derived image tag for the checked-out deployment revision.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# These are the repository inputs copied by deploy/Dockerfile. Refuse to label
# an image with a committed revision when any of its copied inputs differ.
build_inputs=(
  backend
  frontend
  alembic.ini
  deploy/Dockerfile
  deploy/run-with-secrets.sh
  deploy/external-deps/checksums.sha256
)

if ! git -C "$ROOT" diff --quiet --no-ext-diff -- "${build_inputs[@]}" \
  || ! git -C "$ROOT" diff --cached --quiet --no-ext-diff -- "${build_inputs[@]}"; then
  echo "deployment build inputs are dirty; commit or revert them before deploying" >&2
  exit 2
fi

untracked_build_inputs="$(git -C "$ROOT" ls-files --others --exclude-standard -- "${build_inputs[@]}")" || {
  echo "could not inspect deployment build inputs" >&2
  exit 2
}
if [[ -n "$untracked_build_inputs" ]]; then
  echo "deployment build inputs are dirty; commit or remove them before deploying" >&2
  exit 2
fi

vendor_dir="$ROOT/backend/vendor/qrdf"
[[ -d "$vendor_dir" ]] || {
  echo "QRDF vendor directory is missing: $vendor_dir" >&2
  exit 2
}
command -v python3 >/dev/null 2>&1 || {
  echo "python3 is required to fingerprint the QRDF vendor" >&2
  exit 2
}

# backend/vendor/qrdf is currently a gitlink without a usable submodule
# mapping. Docker copies its files into the image, so include a deterministic
# content fingerprint rather than assuming the top-level Git revision covers it.
vendor_fingerprint="$(python3 - "$vendor_dir" <<'PY'
import hashlib
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
if not (root / "pyproject.toml").is_file() or not (root / "qrdf" / "__init__.py").is_file():
    raise SystemExit("QRDF vendor is incomplete; run scripts/fetch-qrdf.sh before deploying")

digest = hashlib.sha256()
ignored_directories = {
    ".git",
    ".worktrees",
    ".superpowers",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
ignored_suffixes = (".pyc", ".pyo", ".pyd", ".db", ".log")


def write_record(kind: bytes, relative: bytes, mode: int, value: bytes = b"") -> None:
    digest.update(kind)
    digest.update(b"\0")
    digest.update(relative)
    digest.update(b"\0")
    digest.update(f"{mode:o}".encode("ascii"))
    digest.update(b"\0")
    digest.update(value)
    digest.update(b"\0")


def is_docker_ignored(entry: os.DirEntry[str]) -> bool:
    # Keep this in sync with the generic rules in the repository .dockerignore.
    return (
        entry.name in ignored_directories
        or entry.name == ".env"
        or entry.name.endswith(ignored_suffixes)
    )


def walk(directory: Path, relative: bytes) -> None:
    entries = sorted(os.scandir(directory), key=lambda entry: os.fsencode(entry.name))
    for entry in entries:
        if is_docker_ignored(entry):
            continue
        entry_relative = relative + (b"/" if relative else b"") + os.fsencode(entry.name)
        metadata = entry.stat(follow_symlinks=False)
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISLNK(metadata.st_mode):
            write_record(b"link", entry_relative, mode, os.fsencode(os.readlink(entry.path)))
        elif stat.S_ISDIR(metadata.st_mode):
            write_record(b"directory", entry_relative, mode)
            walk(Path(entry.path), entry_relative)
        elif stat.S_ISREG(metadata.st_mode):
            write_record(b"file", entry_relative, mode)
            with open(entry.path, "rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
        else:
            raise SystemExit(f"unsupported QRDF vendor entry: {entry_relative.decode(errors='replace')}")


walk(root, b"")
print(digest.hexdigest())
PY
)" || {
  echo "could not fingerprint the QRDF vendor" >&2
  exit 2
}
[[ "$vendor_fingerprint" =~ ^[0-9a-f]{64}$ ]] || {
  echo "unexpected QRDF vendor fingerprint" >&2
  exit 2
}

revision="$(git -C "$ROOT" rev-parse --verify --short=12 HEAD 2>/dev/null)" || {
  echo "could not resolve the checked-out Git revision for the deployment image tag" >&2
  exit 2
}
[[ "$revision" =~ ^[0-9a-f]{12}$ ]] || {
  echo "unexpected Git revision for the deployment image tag" >&2
  exit 2
}

printf 'git-%s-qrdf-%.12s\n' "$revision" "$vendor_fingerprint"
