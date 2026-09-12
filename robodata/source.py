"""Download a small, pinned public sample without altering upstream data."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

from .config import CAMERA, EPISODES, REPO_ID, REVISION

MANIFEST_NAME = "source_manifest.json"
DEMO_MANIFEST_NAME = "demo_manifest.json"
SAMPLE_FILES = (
    "README.md", "meta/info.json", "meta/episodes.jsonl", "meta/tasks.jsonl", "meta/stats.json",
    *(f"data/chunk-000/episode_{i:06d}.parquet" for i in EPISODES),
    *(f"videos/chunk-000/{CAMERA}/episode_{i:06d}.mp4" for i in EPISODES),
)
BASE_URL = f"https://huggingface.co/datasets/{REPO_ID}/resolve/{REVISION}"
CATALOG_URL = f"https://huggingface.co/api/datasets/{REPO_ID}/tree/{REVISION}"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _catalog(session: requests.Session) -> dict[str, dict]:
    """Read trusted content identifiers from the official API at the pinned commit."""
    entries = {}
    url = CATALOG_URL
    params = {"recursive": "true", "limit": 1000}
    while url:
        with session.get(url, params=params, timeout=(20, 90)) as response:
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("数据源文件清单不是预期的列表")
            for item in payload:
                if item.get("type") != "file" or item.get("path") not in SAMPLE_FILES:
                    continue
                lfs = item.get("lfs") or {}
                algorithm = "sha256" if lfs else "git-sha1"
                oid = lfs.get("oid") if lfs else item.get("oid")
                if not isinstance(oid, str) or len(oid) != (64 if lfs else 40):
                    raise ValueError(f"源文件没有可校验的内容标识：{item.get('path')}")
                int(oid, 16)
                size = item.get("size")
                if not isinstance(size, int) or size < 0:
                    raise ValueError(f"源文件大小无效：{item.get('path')}")
                entries[item["path"]] = {"path": item["path"], "size": size,
                                         "hash_algorithm": algorithm, "expected_hash": oid}
            next_url = response.links.get("next", {}).get("url")
            if next_url and not next_url.startswith(CATALOG_URL + "?"):
                raise ValueError("数据源分页地址与固定版本不符")
            url = next_url
            params = None
    missing = sorted(set(SAMPLE_FILES) - entries.keys())
    if missing:
        raise ValueError(f"固定版本缺少所需文件：{', '.join(missing)}")
    return entries


def _verified(path: Path, expected: dict) -> bool:
    if not path.is_file() or path.stat().st_size != expected["size"]:
        return False
    if expected["hash_algorithm"] == "sha256":
        digest = hashlib.sha256()
    elif expected["hash_algorithm"] == "git-sha1":
        digest = hashlib.sha1()
        digest.update(f"blob {expected['size']}\0".encode("ascii"))
    else:
        raise ValueError("不支持的源文件校验算法")
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == expected["expected_hash"]


def fetch_sample(root: Path, proxy: str | None = None,
                 progress: Callable[[str], None] | None = None) -> dict:
    """Fetch 5 complete episodes; verify every cache hit against pinned upstream hashes.

    Standard requests proxy environment variables are inherited. An explicit proxy
    applies only to this session. No credentials or proxy addresses enter the manifest.
    """
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    notify = progress or (lambda message: None)
    with requests.Session() as session:
        session.headers["User-Agent"] = "robodata-workbench/0.1"
        if proxy:
            session.trust_env = False
            session.proxies.update({"http": proxy, "https": proxy})
        for attempt in range(3):
            try:
                expected_files = _catalog(session)
                break
            except (requests.RequestException, ValueError):
                if attempt == 2:
                    raise
                notify(f"数据源清单读取失败，重试 {attempt + 1}/2")
                time.sleep(attempt + 1)
        verified_files = []
        for relative in SAMPLE_FILES:
            expected = expected_files[relative]
            target = root / relative
            if root not in target.resolve().parents:
                raise ValueError("下载目标超出指定目录")
            target.parent.mkdir(parents=True, exist_ok=True)
            if _verified(target, expected):
                notify(f"校验通过，复用：{relative}")
            else:
                partial = target.with_suffix(target.suffix + ".part")
                for attempt in range(3):
                    try:
                        notify(f"下载：{relative}")
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
                                        raise ValueError(f"下载内容超出固定版本大小：{relative}")
                                    stream.write(chunk)
                                stream.flush()
                                os.fsync(stream.fileno())
                        if not _verified(partial, expected):
                            raise ValueError(f"源文件大小或内容校验失败：{relative}")
                        partial.replace(target)
                        break
                    except (requests.RequestException, OSError, ValueError):
                        partial.unlink(missing_ok=True)
                        if attempt == 2:
                            raise
                        notify(f"下载或校验失败，重试 {attempt + 1}/2：{relative}")
                        time.sleep(attempt + 1)
            verified_files.append({**expected, "sha256": file_sha256(target)})
    manifest = {
        "manifest_version": 1,
        "repo_id": REPO_ID,
        "revision": REVISION,
        "source_url": f"https://huggingface.co/datasets/{REPO_ID}/tree/{REVISION}",
        "license": "Apache-2.0 (upstream README declaration)",
        "episodes": list(EPISODES),
        "camera": CAMERA,
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "files": verified_files,
    }
    # Preserve manifest bytes when content is unchanged so repeat fetches do not
    # spuriously invalidate content-based dataset caches.
    manifest_path = root / MANIFEST_NAME
    if manifest_path.is_file():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            comparable = {k: v for k, v in previous.items() if k != "verified_at_utc"}
            if comparable == {k: v for k, v in manifest.items() if k != "verified_at_utc"}:
                return previous
        except (ValueError, OSError):
            pass
    partial = manifest_path.with_suffix(".json.part")
    partial.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    partial.replace(manifest_path)
    return manifest
