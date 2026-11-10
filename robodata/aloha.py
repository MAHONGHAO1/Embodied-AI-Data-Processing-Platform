"""Pinned ALOHA Coffee v3.0 sample acquisition with read-only source files."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

ALOHA_REPO_ID = "lerobot/aloha_static_coffee"
ALOHA_REVISION = "b144896feb1f37398a862927b22cd3abdf005a6b"
ALOHA_CAMERAS = (
    "observation.images.cam_high",
    "observation.images.cam_left_wrist",
    "observation.images.cam_low",
    "observation.images.cam_right_wrist",
)
ALOHA_EPISODES = (0, 1, 2, 3, 4)


def episode_task_text(metadata: dict, episode: dict) -> str:
    """Read task labels from either task-index or task-text LeRobot metadata."""
    tasks = episode.get("tasks")
    if isinstance(tasks, str):
        value = tasks
    elif tasks:
        value = tasks[0]
    else:
        raise ValueError("ALOHA episode 缺少任务描述")
    if isinstance(value, str):
        return value
    try:
        return str(metadata["tasks"][int(value)]["task"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError("ALOHA episode 的任务索引无法解析") from exc
ALOHA_MANIFEST_NAME = "source_manifest.json"
ALOHA_FILES = (
    "README.md",
    "meta/info.json",
    "meta/stats.json",
    "meta/tasks.parquet",
    "meta/episodes/chunk-000/file-000.parquet",
    "data/chunk-000/file-000.parquet",
    *(f"videos/{camera}/chunk-000/file-000.mp4" for camera in ALOHA_CAMERAS),
)
ALOHA_URL = f"https://huggingface.co/datasets/{ALOHA_REPO_ID}/tree/{ALOHA_REVISION}"
BASE_URL = f"https://huggingface.co/datasets/{ALOHA_REPO_ID}/resolve/{ALOHA_REVISION}"
CATALOG_URL = f"https://huggingface.co/api/datasets/{ALOHA_REPO_ID}/tree/{ALOHA_REVISION}"


def _digest(path: Path, algorithm: str, size: int) -> str:
    digest = hashlib.sha256() if algorithm == "sha256" else hashlib.sha1()
    if algorithm == "git-sha1":
        digest.update(f"blob {size}\0".encode("ascii"))
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified(path: Path, expected: dict) -> bool:
    return (path.is_file() and path.stat().st_size == expected["size"]
            and _digest(path, expected["hash_algorithm"], expected["size"]) == expected["expected_hash"])


def _catalog(session: requests.Session) -> dict[str, dict]:
    entries = {}
    url = CATALOG_URL
    params = {"recursive": "true", "limit": 1000}
    while url:
        with session.get(url, params=params, timeout=(20, 90)) as response:
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("ALOHA 固定版本的文件清单不是预期列表")
        for item in payload:
            if item.get("type") != "file" or item.get("path") not in ALOHA_FILES:
                continue
            lfs = item.get("lfs") or {}
            algorithm = "sha256" if lfs else "git-sha1"
            oid = lfs.get("oid") if lfs else item.get("oid")
            size = item.get("size")
            if not isinstance(oid, str) or len(oid) != (64 if algorithm == "sha256" else 40):
                raise ValueError(f"ALOHA 源文件缺少受支持的校验值：{item.get('path')}")
            try:
                int(oid, 16)
            except ValueError as exc:
                raise ValueError(f"ALOHA 源文件校验值格式无效：{item.get('path')}") from exc
            if not isinstance(size, int) or size < 0:
                raise ValueError(f"ALOHA 源文件大小无效：{item.get('path')}")
            entries[item["path"]] = {"path": item["path"], "size": size,
                                      "hash_algorithm": algorithm, "expected_hash": oid}
        next_url = response.links.get("next", {}).get("url")
        if next_url and not next_url.startswith(CATALOG_URL + "?"):
            raise ValueError("ALOHA 文件清单分页地址超出固定版本")
        url, params = next_url, None
    missing = sorted(set(ALOHA_FILES) - entries.keys())
    if missing:
        raise ValueError(f"ALOHA 固定版本缺少所需文件：{', '.join(missing)}")
    return entries


def fetch_aloha_sample(root: Path, proxy: str | None = None,
                       progress: Callable[[str], None] | None = None) -> dict:
    """Download source metadata and shared v3.0 media needed for Episodes 0–4.

    Inputs are never edited. Every cache hit and completed download is checked
    against the pinned revision's upstream object id and byte size.
    """
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    notify = progress or (lambda message: None)
    session = requests.Session()
    try:
        session.headers["User-Agent"] = "robodata-workbench/0.6"
        if proxy:
            session.trust_env = False
            session.proxies.update({"http": proxy, "https": proxy})
        expected_files = None
        for attempt in range(3):
            try:
                expected_files = _catalog(session)
                break
            except (requests.RequestException, ValueError):
                if attempt == 2:
                    raise
                notify(f"ALOHA 固定版本清单读取失败，重试 {attempt + 1}/2")
                time.sleep(attempt + 1)
        verified = []
        for relative in ALOHA_FILES:
            expected = expected_files[relative]
            target = root / relative
            if not target.resolve().is_relative_to(root):
                raise ValueError("ALOHA 下载路径超出指定目录")
            target.parent.mkdir(parents=True, exist_ok=True)
            if not _verified(target, expected):
                partial = target.with_suffix(target.suffix + ".part")
                for attempt in range(3):
                    try:
                        notify(f"下载 ALOHA：{relative}")
                        with session.get(f"{BASE_URL}/{relative}", stream=True,
                                         timeout=(20, 120)) as response:
                            response.raise_for_status()
                            written = 0
                            with partial.open("wb") as stream:
                                for chunk in response.iter_content(chunk_size=1024 * 1024):
                                    if not chunk:
                                        continue
                                    written += len(chunk)
                                    if written > expected["size"]:
                                        raise ValueError(f"ALOHA 文件超出固定版本大小：{relative}")
                                    stream.write(chunk)
                                stream.flush()
                                os.fsync(stream.fileno())
                        if not _verified(partial, expected):
                            raise ValueError(f"ALOHA 文件大小或固定版本哈希不匹配：{relative}")
                        partial.replace(target)
                        break
                    except (requests.RequestException, OSError, ValueError):
                        partial.unlink(missing_ok=True)
                        if attempt == 2:
                            raise
                        notify(f"ALOHA 下载或校验失败，重试 {attempt + 1}/2：{relative}")
                        time.sleep(attempt + 1)
            else:
                notify(f"复用通过固定版本校验的文件：{relative}")
            verified.append({**expected, "sha256": _sha256(target)})

    finally:
        close = getattr(session, "close", None)
        if callable(close):
            close()

    manifest = {
        "manifest_version": 1,
        "source": {
            "repo_id": ALOHA_REPO_ID,
            "revision": ALOHA_REVISION,
            "url": ALOHA_URL,
            "license": "MIT（固定版本数据集页面声明）",
            "robot": "ALOHA 双臂操作平台",
            "format_version": "v3.0",
            "camera": ALOHA_CAMERAS[0],
            "cameras": list(ALOHA_CAMERAS),
            "timestamp_semantics": "沿用 LeRobot v3.0 episode 记录时间；逐相机检查文件内映射，不声明硬件同步",
        },
        "episodes": list(ALOHA_EPISODES),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "files": verified,
    }
    manifest_path = root / ALOHA_MANIFEST_NAME
    partial = manifest_path.with_suffix(".json.part")
    partial.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    partial.replace(manifest_path)
    return manifest
