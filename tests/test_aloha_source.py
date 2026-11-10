import hashlib
import json

import pandas as pd
import pytest

from robodata import aloha
from robodata.dataset import load_metadata
from tools.conversion.aloha_worker import _normalize_numeric_feature


def _git_sha1(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


class _Response:
    def __init__(self, *, payload=None, data=b""):
        self._payload = payload
        self._data = data
        self.links = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload

    def iter_content(self, chunk_size):
        yield self._data


def test_fetch_aloha_records_pinned_manifest_and_verifies_download(monkeypatch, tmp_path):
    source_files = {"README.md": b"MIT source license\n", "meta/info.json": b"{}\n"}
    monkeypatch.setattr(aloha, "ALOHA_FILES", tuple(source_files))
    catalog = [
        {"type": "file", "path": name, "size": len(data), "oid": _git_sha1(data)}
        for name, data in source_files.items()
    ]
    requested = []

    class Session:
        def __init__(self):
            self.headers = {}
            self.trust_env = True
            self.proxies = {}

        def get(self, url, **kwargs):
            requested.append(url)
            if url.startswith(aloha.CATALOG_URL):
                return _Response(payload=catalog)
            relative = url.removeprefix(aloha.BASE_URL + "/")
            return _Response(data=source_files[relative])

    monkeypatch.setattr(aloha.requests, "Session", Session)
    messages = []
    manifest = aloha.fetch_aloha_sample(tmp_path, progress=messages.append)

    assert all(aloha.ALOHA_REVISION in url for url in requested)
    assert manifest["source"]["repo_id"] == aloha.ALOHA_REPO_ID
    assert manifest["source"]["revision"] == aloha.ALOHA_REVISION
    assert manifest["source"]["license"].startswith("MIT")
    assert manifest["source"]["cameras"] == list(aloha.ALOHA_CAMERAS)
    for name, data in source_files.items():
        assert (tmp_path / name).read_bytes() == data
    stored = json.loads((tmp_path / aloha.ALOHA_MANIFEST_NAME).read_text(encoding="utf-8"))
    assert {item["path"] for item in stored["files"]} == set(source_files)


def test_fetch_aloha_rejects_bytes_that_do_not_match_pinned_catalog(monkeypatch, tmp_path):
    source = b"expected"
    monkeypatch.setattr(aloha, "ALOHA_FILES", ("README.md",))
    monkeypatch.setattr(aloha.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(aloha, "_catalog", lambda _session: {
        "README.md": {"path": "README.md", "size": len(source), "hash_algorithm": "git-sha1",
                      "expected_hash": _git_sha1(source)}
    })

    class Session:
        def __init__(self):
            self.headers = {}
            self.trust_env = True
            self.proxies = {}

        def get(self, *_args, **_kwargs):
            return _Response(data=b"changed!")

    monkeypatch.setattr(aloha.requests, "Session", Session)
    with pytest.raises(ValueError, match="哈希不匹配"):
        aloha.fetch_aloha_sample(tmp_path)
    assert not (tmp_path / "README.md").exists()
    assert not (tmp_path / aloha.ALOHA_MANIFEST_NAME).exists()


def test_episode_task_text_supports_task_labels_and_indices():
    metadata = {"tasks": [{"task": "Place a cup on the tray."}]}

    assert aloha.episode_task_text(metadata, {"tasks": ["Place a cup on the tray."]}) == "Place a cup on the tray."
    assert aloha.episode_task_text(metadata, {"tasks": [0]}) == "Place a cup on the tray."


def test_scalar_boolean_feature_is_shaped_as_declared():
    value = _normalize_numeric_feature(False, {"name": "next.done", "dtype": "bool", "shape": [1]})
    assert value.dtype == "bool"
    assert value.shape == (1,)
    assert value.tolist() == [False]


def test_reviewed_aloha_episode_subset_is_accepted_with_exact_manifest(tmp_path):
    cameras = list(aloha.ALOHA_CAMERAS)
    metadata_dir = tmp_path / "meta" / "episodes" / "chunk-000"
    metadata_dir.mkdir(parents=True)
    features = {camera: {"dtype": "video", "shape": [480, 640, 3]} for camera in cameras}
    info = {"codebase_version": "v3.0", "fps": 50, "total_episodes": 5,
            "total_frames": 5500, "features": features,
            "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
            "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4"}
    (tmp_path / "meta" / "info.json").write_text(json.dumps(info), encoding="utf-8")
    pd.DataFrame({"task_index": [0]}, index=["Place a cup on the tray."]).to_parquet(
        tmp_path / "meta" / "tasks.parquet")
    pd.DataFrame([{"episode_index": index, "length": 1100} for index in range(5)]).to_parquet(
        metadata_dir / "file-000.parquet")
    source = {"repo_id": aloha.ALOHA_REPO_ID, "revision": aloha.ALOHA_REVISION,
              "license": "MIT（固定版本数据集页面声明）", "camera": cameras[0], "cameras": cameras}
    manifest = {"profile": "aloha_static_coffee_reviewed_v1", "source": source,
                "episodes": [{"source_episode_index": index, "output_episode_index": index,
                              "row_count": 1100} for index in range(5)]}
    (tmp_path / "source_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    loaded = load_metadata(tmp_path)

    assert loaded["episode_indices"] == list(range(5))


def test_aloha_five_episode_source_cannot_masquerade_as_pinned_full_dataset(tmp_path):
    test_reviewed_aloha_episode_subset_is_accepted_with_exact_manifest(tmp_path)
    manifest_path = tmp_path / "source_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("profile")
    manifest.pop("episodes")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="固定 50 条任务配置"):
        load_metadata(tmp_path)
