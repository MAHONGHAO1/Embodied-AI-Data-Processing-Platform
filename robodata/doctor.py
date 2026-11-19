"""One-command environment and adapter readiness report."""

from __future__ import annotations

import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile

from .adapters import descriptors
from .config import APP_VERSION, PROJECT_ROOT
from .conversion import CONVERSION_PYTHON, CONVERSION_TOOL_ROOT


def _check(name: str, status: str, detail: str, hint: str | None = None) -> dict:
    value = {"name": name, "status": status, "detail": detail}
    if hint:
        value["hint"] = hint
    return value


def _pinned_lerobot() -> str | None:
    path = CONVERSION_TOOL_ROOT / "pyproject.toml"
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip().strip(",").strip().strip('"')
        if value.startswith("lerobot=="):
            return value.split("==", 1)[1].strip().strip('"')
    return None


def _project_version() -> str | None:
    path = PROJECT_ROOT / "pyproject.toml"
    if not path.is_file():
        return None
    try:
        import tomllib
        return tomllib.loads(path.read_text(encoding="utf-8")).get("project", {}).get("version")
    except Exception:
        return None


def run_doctor() -> dict:
    checks: list[dict] = []

    python_ok = sys.version_info[:2] == (3, 12)
    checks.append(_check("python", "pass" if python_ok else "fail",
                         f"{sys.version.split()[0]}",
                         None if python_ok else "项目要求 Python 3.12.x"))

    try:
        adapters = descriptors()
        checks.append(_check("adapters", "pass" if adapters else "fail",
                             f"{len(adapters)} 个已注册：" + ", ".join(item["adapter_id"] for item in adapters),
                             None if adapters else "内置适配器未加载"))
    except Exception as exc:
        adapters = []
        checks.append(_check("adapters", "fail", f"注册表加载失败：{exc}"))

    pyproject_version = _project_version()
    version_ok = pyproject_version == APP_VERSION
    checks.append(_check("version_chain", "pass" if version_ok else "fail",
                         f"APP_VERSION={APP_VERSION}，pyproject={pyproject_version}",
                         None if version_ok else "运行 tests/test_version_consistency.py 定位漂移"))

    conversion_ok = CONVERSION_PYTHON.is_file()
    checks.append(_check("conversion_env", "pass" if conversion_ok else "warn",
                         f"{CONVERSION_PYTHON}" if conversion_ok else "未找到独立转换环境",
                         None if conversion_ok else "执行 uv sync --project tools/conversion --locked"))

    pinned = _pinned_lerobot()
    checks.append(_check("conversion_pin", "pass" if pinned else "warn",
                         f"lerobot=={pinned}" if pinned else "无法读取 tools/conversion/pyproject.toml 的 lerobot 固定版本"))

    work = PROJECT_ROOT / "work"
    try:
        work.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=work, prefix=".doctor-", delete=True) as stream:
            stream.write(b"ok")
        checks.append(_check("work_dir", "pass", f"可写：{work}"))
    except OSError as exc:
        checks.append(_check("work_dir", "fail", f"不可写：{exc}", "检查磁盘权限或 ROBODATA 工作目录"))

    try:
        free = shutil.disk_usage(PROJECT_ROOT).free
        status = "pass" if free >= 30 * 1024 ** 3 else "warn"
        checks.append(_check("disk_free", status, f"{free / (1024 ** 3):.1f} GiB 可用",
                             None if status == "pass" else "全量 Matching Cable 建议至少 30–40 GB 空闲"))
    except OSError as exc:
        checks.append(_check("disk_free", "warn", f"无法读取磁盘空间：{exc}"))

    ok = all(item["status"] != "fail" for item in checks)
    return {"ok": ok, "app_version": APP_VERSION, "python": sys.version.split()[0],
            "platform": platform.platform(), "cwd": os.getcwd(),
            "checks": checks, "adapters": adapters}
