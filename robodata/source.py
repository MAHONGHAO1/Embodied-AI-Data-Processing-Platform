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


def sample_files(episodes) -> tuple[str, ...]:
    """Return the exact upstream files required for one complete episode subset.

    Episodes are selected whole. We never trim frames or rewrite videos here:
    a derived trim would no longer be byte-identical to the pinned upstream
    revision and would need its own provenance chain.
    """
    selected = tuple(int(index) for index in episodes)
    if not selected:
        raise ValueError("至少选择一个 episode")
    return (
        "README.md", "meta/info.json", "meta/episodes.jsonl", "meta/tasks.jsonl", "meta/stats.json",
        *(f"data/chunk-000/episode_{i:06d}.parquet" for i in selected),
        *(f"videos/chunk-000/{CAMERA}/episode_{i:06d}.mp4" for i in selected),
    )


SAMPLE_FILES = sample_files(EPISODES)
DEFAULT_ENDPOINT = "https://huggingface.co"
BASE_URL = f"{DEFAULT_ENDPOINT}/datasets/{REPO_ID}/resolve/{REVISION}"
CATALOG_URL = f"{DEFAULT_ENDPOINT}/api/datasets/{REPO_ID}/tree/{REVISION}"


def _endpoint(value: str | None) -> str:
    """Validate a transport endpoint; the pinned repository revision never changes."""
    endpoint = (value or DEFAULT_ENDPOINT).strip().rstrip("/")
    if not endpoint.startswith(("https://", "http://")):
        raise ValueError("数据源地址必须以 http:// 或 https:// 开头")
    return endpoint


def _base_url(endpoint: str) -> str:
    return f"{endpoint}/datasets/{REPO_ID}/resolve/{REVISION}"


def _catalog_url(endpoint: str) -> str:
    return f"{endpoint}/api/datasets/{REPO_ID}/tree/{REVISION}"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _catalog(session: requests.Session, files=None, endpoint: str | None = None) -> dict[str, dict]:
    """Read trusted content identifiers from the API at the pinned commit."""
    required = tuple(files if files is not None else SAMPLE_FILES)
    endpoint = _endpoint(endpoint)
    catalog_url = _catalog_url(endpoint)
    entries = {}
    url = catalog_url
    params = {"recursive": "true", "limit": 1000}
    while url:
        with session.get(url, params=params, timeout=(20, 90)) as response:
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("数据源文件清单不是预期的列表")
            for item in payload:
                if item.get("type") != "file" or item.get("path") not in required:
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
            if next_url and not next_url.startswith(catalog_url + "?"):
                raise ValueError("数据源分页地址与固定版本不符")
            url = next_url
            params = None
    missing = sorted(set(required) - entries.keys())
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
                 progress: Callable[[str], None] | None = None,
                 episodes=None, files=None, endpoint: str | None = None) -> dict:
    """Fetch complete episodes; verify every cache hit against pinned upstream hashes.

    ``episodes`` selects whole upstream episodes. Omitting it keeps the configured
    default and preserves the historical five-episode manifest. ``files`` is an
    internal/testing override; production callers should use ``episodes``.

    Standard requests proxy environment variables are inherited. An explicit proxy
    applies only to this session. No credentials or proxy addresses enter the manifest.
    """
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    notify = progress or (lambda message: None)
    selected = list(episodes) if episodes is not None else list(EPISODES)
    required = tuple(files if files is not None else (sample_files(selected) if episodes is not None else SAMPLE_FILES))
    endpoint = _endpoint(endpoint)
    base_url = _base_url(endpoint)
    with requests.Session() as session:
        session.headers["User-Agent"] = "robodata-workbench/0.1"
        if proxy:
            session.trust_env = False
            session.proxies.update({"http": proxy, "https": proxy})
        for attempt in range(3):
            try:
                expected_files = _catalog(session, required, endpoint=endpoint)
                break
            except (requests.RequestException, ValueError):
                if attempt == 2:
                    raise
                notify(f"数据源清单读取失败，重试 {attempt + 1}/2")
                time.sleep(attempt + 1)
        verified_files = []
        for relative in required:
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
                        with session.get(f"{base_url}/{relative}", stream=True,
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
        "source_url": f"{endpoint}/datasets/{REPO_ID}/tree/{REVISION}",
        "transport_endpoint": endpoint,
        "license": "Apache-2.0 (upstream README declaration)",
        "episodes": selected,
        "camera": CAMERA,
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "file_count": len(verified_files),
        "total_bytes": sum(item["size"] for item in verified_files),
        "selection": "complete upstream episodes; bytes are not trimmed or rewritten",
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


def _target(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError("来源清单包含空路径")
    path = (root / relative).resolve()
    if path == root or root not in path.parents:
        raise ValueError(f"来源清单路径超出样本目录：{relative}")
    return path


def verify_manifest(root: Path, episodes=None) -> dict:
    """Verify local bytes against the manifest written at download time.

    This is the offline half of source verification: it proves the files are the
    bytes recorded in ``source_manifest.json``. Use :func:`verify_sample` when a
    caller also needs to re-check those identifiers against the pinned upstream
    revision. The manifest is not a signature; it is a local provenance record.
    """
    root = Path(root).resolve()
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise FileNotFoundError(f"缺少来源清单：{manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("repo_id") != REPO_ID or manifest.get("revision") != REVISION:
        raise ValueError("来源清单的仓库或 revision 与固定配置不一致")
    manifest_episodes = manifest.get("episodes")
    if not isinstance(manifest_episodes, list) or not manifest_episodes:
        raise ValueError("来源清单没有记录 episode 范围")
    selected = list(episodes) if episodes is not None else list(manifest_episodes)
    if episodes is not None and not set(selected).issubset(set(manifest_episodes)):
        raise ValueError(f"来源清单不包含请求的 episode：{sorted(set(selected) - set(manifest_episodes))}")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise ValueError("来源清单没有文件记录")
    if episodes is not None:
        required = set(sample_files(selected))
        entries = [entry for entry in entries if entry.get("path") in required]
        missing_entries = sorted(required - {entry.get("path") for entry in entries})
        if missing_entries:
            raise ValueError(f"来源清单未记录请求文件：{', '.join(missing_entries)}")
    checked, missing, mismatched, size = [], [], [], 0
    for entry in entries:
        relative = entry.get("path")
        target = _target(root, relative)
        if not target.is_file():
            missing.append(relative)
            continue
        size += target.stat().st_size
        if not _verified(target, entry):
            mismatched.append(relative)
            continue
        checked.append({"path": relative, "size": target.stat().st_size, "sha256": file_sha256(target)})
    return {
        "ok": not missing and not mismatched,
        "repo_id": REPO_ID,
        "revision": REVISION,
        "episodes": selected,
        "file_count": len(entries),
        "total_bytes": size,
        "checked": checked,
        "missing": missing,
        "mismatched": mismatched,
        "manifest_verified_at_utc": manifest.get("verified_at_utc"),
        "scope": "local bytes checked against source_manifest.json; not a remote re-download",
    }


def verify_sample(root: Path, episodes=None, proxy: str | None = None, endpoint: str | None = None) -> dict:
    """Re-check local bytes against the pinned upstream file identifiers.

    Requires network access to the Hugging Face API. It does not download file
    bodies; it fetches the catalog at the pinned revision and hashes local files.
    """
    root = Path(root).resolve()
    endpoint = _endpoint(endpoint)
    required = sample_files(episodes) if episodes is not None else SAMPLE_FILES
    with requests.Session() as session:
        session.headers["User-Agent"] = "robodata-workbench/0.1"
        if proxy:
            session.trust_env = False
            session.proxies.update({"http": proxy, "https": proxy})
        expected = _catalog(session, required, endpoint=endpoint)
    checked, missing, mismatched, size = [], [], [], 0
    for relative in required:
        target = _target(root, relative)
        if not target.is_file():
            missing.append(relative)
            continue
        size += target.stat().st_size
        if not _verified(target, expected[relative]):
            mismatched.append(relative)
            continue
        checked.append({"path": relative, "size": target.stat().st_size})
    return {
        "ok": not missing and not mismatched,
        "repo_id": REPO_ID,
        "revision": REVISION,
        "episodes": list(episodes) if episodes is not None else list(EPISODES),
        "file_count": len(required),
        "total_bytes": size,
        "checked": checked,
        "missing": missing,
        "mismatched": mismatched,
        "scope": "local bytes checked against the pinned upstream catalog at the recorded revision",
    }
