"""Verified loading for the hand-deployed Embodied VL extension module."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import platform
import re
import stat
import sys
import threading
from pathlib import Path
from types import ModuleType

_MODULE_NAME = "embodied_vl_client"
_SUPPORTED_ABIS = frozenset({(3, 9), (3, 10), (3, 11), (3, 12)})
_MANIFEST_ENTRY_RE = re.compile(
    r"^(?P<checksum>[0-9a-f]{64})  embodied-vl/"
    r"(?P<filename>embodied_vl_client\.cpython-(?:39|310|311|312)-x86_64-linux-gnu\.so)$"
)
_LOAD_LOCK = threading.RLock()


class EmbodiedVlSdkError(RuntimeError):
    """A safe, stable failure while validating or loading the external SDK."""


def embodied_vl_sdk_filename() -> str:
    """Return the only extension filename valid for this interpreter."""

    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise EmbodiedVlSdkError("Embodied VL SDK requires Linux x86_64")
    if _implementation_name() != "cpython":
        raise EmbodiedVlSdkError("Embodied VL SDK requires CPython")
    major, minor = _python_version()
    if (major, minor) not in _SUPPORTED_ABIS:
        raise EmbodiedVlSdkError("Embodied VL SDK requires CPython 3.9-3.12")
    return f"embodied_vl_client.cpython-{major}{minor}-x86_64-linux-gnu.so"


def verify_embodied_vl_sdk_asset(
    sdk_dir: str | Path,
    checksums_file: str | Path,
) -> Path:
    """Return the exact verified extension path without following symlinks."""

    filename = embodied_vl_sdk_filename()
    directory = Path(sdk_dir)
    _require_regular_directory(directory)
    binary_path = directory / filename
    _require_regular_file(binary_path, "binary")
    expected_checksum = _manifest_checksum(Path(checksums_file), filename)
    if _sha256_regular_file(binary_path) != expected_checksum:
        raise EmbodiedVlSdkError("Embodied VL SDK checksum mismatch")
    return binary_path


def load_embodied_vl_client(
    sdk_dir: str | Path,
    checksums_file: str | Path,
) -> ModuleType:
    """Load the verified extension without adding deployment paths to ``sys.path``."""

    binary_path = verify_embodied_vl_sdk_asset(sdk_dir, checksums_file)
    with _LOAD_LOCK:
        existing = sys.modules.get(_MODULE_NAME)
        if existing is not None:
            if _is_same_verified_module(existing, binary_path):
                return existing
            raise EmbodiedVlSdkError("Embodied VL SDK module conflicts with verified SDK")

        spec = _extension_spec(binary_path)
        if spec is None or spec.loader is None:
            raise EmbodiedVlSdkError("Embodied VL SDK could not be loaded")

        prior_exists = _MODULE_NAME in sys.modules
        prior_module = sys.modules.get(_MODULE_NAME)
        try:
            module = importlib.util.module_from_spec(spec)
            module.__file__ = str(binary_path)
            sys.modules[_MODULE_NAME] = module
            spec.loader.exec_module(module)
        except Exception as exc:
            if prior_exists:
                sys.modules[_MODULE_NAME] = prior_module
            else:
                sys.modules.pop(_MODULE_NAME, None)
            raise EmbodiedVlSdkError("Embodied VL SDK import failed") from exc

        if not callable(getattr(module, "EmbodiedVLClient", None)):
            if prior_exists:
                sys.modules[_MODULE_NAME] = prior_module
            else:
                sys.modules.pop(_MODULE_NAME, None)
            raise EmbodiedVlSdkError("Embodied VL SDK has no client")
        return module


def _extension_spec(binary_path: Path):
    return importlib.util.spec_from_file_location(_MODULE_NAME, binary_path)


def _implementation_name() -> str:
    return str(getattr(sys.implementation, "name", ""))


def _python_version() -> tuple[int, int]:
    major, minor = sys.version_info[:2]
    return int(major), int(minor)


def _is_same_verified_module(module: object, binary_path: Path) -> bool:
    module_path = getattr(module, "__file__", None)
    if not isinstance(module_path, str):
        return False
    try:
        same_path = Path(module_path).absolute() == binary_path.absolute()
    except (OSError, ValueError):
        return False
    return same_path and callable(getattr(module, "EmbodiedVLClient", None))


def _require_regular_directory(path: Path) -> None:
    try:
        metadata = os.lstat(path)
    except OSError:
        raise EmbodiedVlSdkError("Embodied VL SDK directory is unavailable") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise EmbodiedVlSdkError("Embodied VL SDK directory is unavailable")


def _require_regular_file(path: Path, label: str) -> None:
    try:
        metadata = os.lstat(path)
    except OSError:
        raise EmbodiedVlSdkError(f"Embodied VL SDK {label} is unavailable") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise EmbodiedVlSdkError(f"Embodied VL SDK {label} is unavailable")


def _manifest_checksum(checksums_file: Path, expected_filename: str) -> str:
    _require_regular_file(checksums_file, "checksum manifest")
    try:
        content = checksums_file.read_text(encoding="ascii")
    except (OSError, UnicodeError):
        raise EmbodiedVlSdkError("Embodied VL SDK checksum manifest is invalid") from None

    checksums: dict[str, str] = {}
    for line in content.splitlines():
        if not line:
            continue
        match = _MANIFEST_ENTRY_RE.fullmatch(line)
        if match is None:
            raise EmbodiedVlSdkError("Embodied VL SDK checksum manifest is invalid")
        filename = match.group("filename")
        if filename in checksums:
            raise EmbodiedVlSdkError("Embodied VL SDK checksum manifest is invalid")
        checksums[filename] = match.group("checksum")
    checksum = checksums.get(expected_filename)
    if checksum is None:
        raise EmbodiedVlSdkError("Embodied VL SDK checksum manifest is invalid")
    return checksum


def _sha256_regular_file(path: Path) -> str:
    _require_regular_file(path, "binary")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise EmbodiedVlSdkError("Embodied VL SDK binary is unavailable") from None
    return digest.hexdigest()
