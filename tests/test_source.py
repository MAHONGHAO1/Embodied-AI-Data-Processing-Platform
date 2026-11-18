import hashlib
import json

import pytest

from robodata import source


class Response:
    def __init__(self, content=b"", payload=None, links=None):
        self.content = content
        self.payload = payload
        self.links = links or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        yield self.content


class Session:
    def __init__(self, content):
        self.content = content
        self.calls = []
        self.headers = {}
        self.proxies = {}
        self.trust_env = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, **kwargs):
        self.calls.append(url)
        return Response(self.content)


def configure(monkeypatch, content=b"pinned content"):
    relative = "meta/example.json"
    session = Session(content)
    entry = {"path": relative, "size": len(content), "hash_algorithm": "sha256",
             "expected_hash": hashlib.sha256(content).hexdigest()}
    monkeypatch.setattr(source, "SAMPLE_FILES", (relative,))
    monkeypatch.setattr(source, "_catalog",
                        lambda session, files=None, endpoint=None: {relative: entry})
    monkeypatch.setattr(source.requests, "Session", lambda: session)
    monkeypatch.setattr(source.time, "sleep", lambda seconds: None)
    return relative, session


def test_download_cache_verified_against_pinned_hash_and_manifest_stable(tmp_path, monkeypatch):
    relative, session = configure(monkeypatch)
    first = source.fetch_sample(tmp_path)
    second = source.fetch_sample(tmp_path)
    assert first == second
    assert len(session.calls) == 1
    # Same-size corruption must be caught; local manifest edits cannot bless it.
    target = tmp_path / relative
    target.write_bytes(b"broken content")
    first["files"][0]["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    (tmp_path / source.MANIFEST_NAME).write_text(json.dumps(first), encoding="utf-8")
    source.fetch_sample(tmp_path)
    assert target.read_bytes() == b"pinned content"
    assert len(session.calls) == 2
    assert not target.with_suffix(".json.part").exists()


def test_download_failure_never_replaces_existing_file(tmp_path, monkeypatch):
    relative, session = configure(monkeypatch)
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing local")
    session.content = b"wrong content!"
    with pytest.raises(ValueError, match="校验失败"):
        source.fetch_sample(tmp_path)
    assert target.read_bytes() == b"existing local"
    assert len(session.calls) == 3
    assert not target.with_suffix(".json.part").exists()
    assert not (tmp_path / source.MANIFEST_NAME).exists()


def test_explicit_proxy_is_session_only_and_not_persisted(tmp_path, monkeypatch):
    _, session = configure(monkeypatch)
    manifest = source.fetch_sample(tmp_path, proxy="http://example.invalid:1234")
    assert session.trust_env is False
    assert session.proxies["https"] == "http://example.invalid:1234"
    assert "example.invalid" not in json.dumps(manifest)


def test_git_blob_hash_verification(tmp_path):
    path = tmp_path / "README.md"
    path.write_bytes(b"hello\n")
    expected = {"size": 6, "hash_algorithm": "git-sha1",
                "expected_hash": hashlib.sha1(b"blob 6\0hello\n").hexdigest()}
    assert source._verified(path, expected)
    path.write_bytes(b"HELLO\n")
    assert not source._verified(path, expected)


def test_catalog_requires_every_pinned_file(monkeypatch):
    monkeypatch.setattr(source, "SAMPLE_FILES", ("README.md", "missing",))
    class CatalogSession:
        def get(self, *args, **kwargs):
            return Response(payload=[{"type": "file", "path": "README.md", "size": 6,
                                      "oid": "f" * 40}])
    with pytest.raises(ValueError, match="missing"):
        source._catalog(CatalogSession())


def test_sample_files_selects_whole_episodes_only():
    files = source.sample_files((0, 1))
    assert "data/chunk-000/episode_000000.parquet" in files
    assert "data/chunk-000/episode_000001.parquet" in files
    assert "data/chunk-000/episode_000002.parquet" not in files
    assert any("episode_000001.mp4" in item for item in files)
    with pytest.raises(ValueError):
        source.sample_files(())


def test_fetch_sample_subset_writes_explicit_manifest_range(tmp_path, monkeypatch):
    relative = "meta/info.json"
    content = b"subset"
    entry = {"path": relative, "size": len(content), "hash_algorithm": "sha256",
             "expected_hash": hashlib.sha256(content).hexdigest()}
    session = Session(content)
    monkeypatch.setattr(source, "_catalog",
                        lambda session, files=None, endpoint=None: {relative: entry})
    monkeypatch.setattr(source.requests, "Session", lambda: session)
    manifest = source.fetch_sample(tmp_path, episodes=(0, 1), files=(relative,))
    assert manifest["episodes"] == [0, 1]
    assert manifest["file_count"] == 1
    assert manifest["selection"].startswith("complete upstream episodes")
    assert (tmp_path / relative).read_bytes() == content


def test_verify_manifest_detects_same_size_corruption(tmp_path, monkeypatch):
    relative = "meta/info.json"
    content = b"subset"
    entry = {"path": relative, "size": len(content), "hash_algorithm": "sha256",
             "expected_hash": hashlib.sha256(content).hexdigest()}
    session = Session(content)
    monkeypatch.setattr(source, "_catalog",
                        lambda session, files=None, endpoint=None: {relative: entry})
    monkeypatch.setattr(source.requests, "Session", lambda: session)
    source.fetch_sample(tmp_path, episodes=(0, 1), files=(relative,))
    assert source.verify_manifest(tmp_path)["ok"] is True
    (tmp_path / relative).write_bytes(b"SUBSEt")
    result = source.verify_manifest(tmp_path)
    assert result["ok"] is False
    assert result["mismatched"] == [relative]
    with pytest.raises(ValueError, match="不包含"):
        source.verify_manifest(tmp_path, episodes=(2,))


def test_verify_manifest_reports_missing_file(tmp_path, monkeypatch):
    relative = "meta/info.json"
    content = b"subset"
    entry = {"path": relative, "size": len(content), "hash_algorithm": "sha256",
             "expected_hash": hashlib.sha256(content).hexdigest()}
    session = Session(content)
    monkeypatch.setattr(source, "_catalog",
                        lambda session, files=None, endpoint=None: {relative: entry})
    monkeypatch.setattr(source.requests, "Session", lambda: session)
    source.fetch_sample(tmp_path, episodes=(0, 1), files=(relative,))
    (tmp_path / relative).unlink()
    result = source.verify_manifest(tmp_path)
    assert result["ok"] is False
    assert result["missing"] == [relative]


def test_catalog_extracts_git_and_lfs_hashes(monkeypatch):
    monkeypatch.setattr(source, "SAMPLE_FILES", ("README.md", "video.mp4"))
    class CatalogSession:
        def get(self, *args, **kwargs):
            return Response(payload=[
                {"type": "file", "path": "README.md", "size": 6, "oid": "f" * 40},
                {"type": "file", "path": "video.mp4", "size": 10, "oid": "a" * 40,
                 "lfs": {"oid": "b" * 64, "size": 10}},
            ])
    catalog = source._catalog(CatalogSession())
    assert catalog["README.md"]["hash_algorithm"] == "git-sha1"
    assert catalog["video.mp4"]["expected_hash"] == "b" * 64
