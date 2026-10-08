from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest

from data.config import settings
from data.utils.checksums import legacy_batch_tree_sha256, tree_sha256


def _source(root: Path, *, payload: bytes = b"mcap", data_file: str = "data.mcap") -> Path:
    root.mkdir(parents=True)
    (root / "metadata.json").write_text(
        json.dumps({"qrdf_version": "0.2.0", "episode_id": "source", "data_file": data_file}),
        encoding="utf-8",
    )
    if data_file == "data.mcap":
        (root / data_file).write_bytes(payload)
    return root


def _install_downloader(monkeypatch, module, source: Path, calls: list[str]) -> None:
    def download_to(destination: Path, bucket: str, key: str) -> Path:
        calls.append(f"{bucket}/{key}")
        shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)
        return destination

    monkeypatch.setattr(module.oss_client, "download_to", download_to)


def _facts(module, source: Path, *, artifact_id: int = 1, checksum: str | None = None):
    return module.PublicationSourceFacts(
        artifact_id=artifact_id,
        storage_uri="oss://raw-bucket/source/",
        checksum_sha256=checksum or tree_sha256(source),
    )


def test_publication_source_cache_downloads_one_artifact_once(tmp_path, monkeypatch):
    from data.services import publication_source_cache

    monkeypatch.setattr(settings, "storage_root", str(tmp_path / "storage"))
    source = _source(tmp_path / "provider")
    calls: list[str] = []
    _install_downloader(monkeypatch, publication_source_cache, source, calls)
    facts = _facts(publication_source_cache, source)

    with publication_source_cache.acquire_publication_source(facts) as first:
        assert (first / "data.mcap").is_file()
    with publication_source_cache.acquire_publication_source(facts) as second:
        assert second == first

    assert calls == ["raw-bucket/source/"]


def test_publication_source_cache_rejects_checksum_mismatch(tmp_path, monkeypatch):
    from data.services import publication_source_cache

    monkeypatch.setattr(settings, "storage_root", str(tmp_path / "storage"))
    source = _source(tmp_path / "provider")
    _install_downloader(monkeypatch, publication_source_cache, source, [])
    facts = _facts(publication_source_cache, source, checksum="f" * 64)

    with pytest.raises(ValueError, match="checksum"):
        with publication_source_cache.acquire_publication_source(facts):
            pass


def test_publication_source_cache_accepts_legacy_import_checksum(tmp_path, monkeypatch):
    from data.services import publication_source_cache

    monkeypatch.setattr(settings, "storage_root", str(tmp_path / "storage"))
    source = _source(tmp_path / "provider")
    _install_downloader(monkeypatch, publication_source_cache, source, [])
    facts = _facts(
        publication_source_cache,
        source,
        checksum=legacy_batch_tree_sha256(source),
    )

    with publication_source_cache.acquire_publication_source(facts) as cached:
        assert (cached / "data.mcap").is_file()


def test_publication_source_cache_rejects_symlinked_entry(tmp_path, monkeypatch):
    from data.services import publication_source_cache

    monkeypatch.setattr(settings, "storage_root", str(tmp_path / "storage"))
    source = _source(tmp_path / "provider")
    outside = tmp_path / "outside.mcap"
    outside.write_bytes(b"outside")
    (source / "linked.mcap").symlink_to(outside)
    _install_downloader(monkeypatch, publication_source_cache, source, [])
    facts = publication_source_cache.PublicationSourceFacts(
        artifact_id=1,
        storage_uri="oss://raw-bucket/source/",
        checksum_sha256="a" * 64,
    )

    with pytest.raises(ValueError, match="symlink"):
        with publication_source_cache.acquire_publication_source(facts):
            pass
    assert outside.read_bytes() == b"outside"


def test_publication_source_cache_rejects_data_file_escape(tmp_path, monkeypatch):
    from data.services import publication_source_cache

    monkeypatch.setattr(settings, "storage_root", str(tmp_path / "storage"))
    source = _source(tmp_path / "provider", data_file="../outside.mcap")
    outside = tmp_path / "outside.mcap"
    outside.write_bytes(b"outside")
    _install_downloader(monkeypatch, publication_source_cache, source, [])
    facts = _facts(publication_source_cache, source)

    with pytest.raises(ValueError, match="data_file|inside|relative"):
        with publication_source_cache.acquire_publication_source(facts):
            pass
    assert outside.read_bytes() == b"outside"


def test_publication_source_cache_rejects_mismatched_marker(tmp_path, monkeypatch):
    from data.services import publication_source_cache

    monkeypatch.setattr(settings, "storage_root", str(tmp_path / "storage"))
    source = _source(tmp_path / "provider")
    _install_downloader(monkeypatch, publication_source_cache, source, [])
    facts = _facts(publication_source_cache, source)

    with publication_source_cache.acquire_publication_source(facts) as cached:
        marker = cached.parent / "complete.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["storage_uri"] = "oss://other/source/"
    marker.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="marker"):
        with publication_source_cache.acquire_publication_source(facts):
            pass


def test_publication_source_cache_cleanup_skips_locked_entry(tmp_path, monkeypatch):
    from data.services import publication_source_cache

    monkeypatch.setattr(settings, "storage_root", str(tmp_path / "storage"))
    source = _source(tmp_path / "provider")
    _install_downloader(monkeypatch, publication_source_cache, source, [])
    facts = _facts(publication_source_cache, source)

    with publication_source_cache.acquire_publication_source(facts) as cached:
        old = time.time() - 3600
        os.utime(cached.parent, (old, old))
        publication_source_cache.cleanup_publication_source_cache(ttl_seconds=0, max_bytes=0)
        assert cached.is_dir()

    publication_source_cache.cleanup_publication_source_cache(ttl_seconds=0, max_bytes=0)
    assert not cached.parent.exists()


def test_publication_source_cache_cleanup_skips_active_staging(tmp_path, monkeypatch):
    from data.services import publication_source_cache

    monkeypatch.setattr(settings, "storage_root", str(tmp_path / "storage"))
    source = _source(tmp_path / "provider")
    facts = _facts(publication_source_cache, source)

    def download_to(destination: Path, _bucket: str, _key: str) -> Path:
        shutil.copytree(source, destination, dirs_exist_ok=True)
        old = time.time() - 3600
        os.utime(destination.parent, (old, old))
        publication_source_cache.cleanup_publication_source_cache(ttl_seconds=0, max_bytes=10**9)
        return destination

    monkeypatch.setattr(publication_source_cache.oss_client, "download_to", download_to)

    with publication_source_cache.acquire_publication_source(facts) as cached:
        assert (cached / "data.mcap").is_file()


def test_publication_source_cache_cleanup_stays_below_root(tmp_path, monkeypatch):
    from data.services import publication_source_cache

    monkeypatch.setattr(settings, "storage_root", str(tmp_path / "storage"))
    source = _source(tmp_path / "provider", payload=b"x" * 64)
    _install_downloader(monkeypatch, publication_source_cache, source, [])
    first = _facts(publication_source_cache, source, artifact_id=1)
    second = _facts(publication_source_cache, source, artifact_id=2)
    with publication_source_cache.acquire_publication_source(first):
        pass
    with publication_source_cache.acquire_publication_source(second):
        pass
    canary = tmp_path / "canary"
    canary.write_text("keep", encoding="utf-8")

    publication_source_cache.cleanup_publication_source_cache(ttl_seconds=10**9, max_bytes=1)

    assert canary.read_text(encoding="utf-8") == "keep"
    cache_root = Path(settings.storage_root) / "hot" / "publication-source-cache" / "entries"
    assert not [entry for entry in cache_root.iterdir() if entry.is_dir()]
