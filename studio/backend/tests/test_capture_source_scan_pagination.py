"""V2 capture source paths and bounded OSS listing pages."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from data.config import settings
from data.database import (
    ExternalOssImportScope,
    ImportCandidate,
    ImportSession,
    TaskLabel,
    TaskSet,
    User,
    Workspace,
)
from data.infra import oss_client
from data.infra.oss_client import OSSDirectoryPage, OSSObjectInfo
from data.services.batches import create_batch
from data.services.capture_batch_import import _capture_source_path
from data.services.import_intake import run_import_candidate_scan, start_import_candidate_scan
from data.services.import_sessions import create_import_session


def test_capture_source_path_accepts_flat_and_utc_date_hour_layouts():
    flat = _capture_source_path("raw/v2/sources/episode-a/data.mcap", require_known_file=True)
    partitioned = _capture_source_path(
        "raw/v2/sources/date=2026-08-25/hour=03/episode-a/complete.json",
        require_known_file=True,
    )

    assert flat is not None
    assert flat.episode_id == "episode-a"
    assert flat.capture_date is None
    assert flat.capture_hour is None
    assert partitioned is not None
    assert partitioned.episode_id == "episode-a"
    assert partitioned.capture_date == date(2026, 8, 25)
    assert partitioned.capture_hour == 3


@pytest.mark.parametrize(
    "key",
    (
        "raw/v2/sources/date=2026-02-30/hour=03/episode-a/data.mcap",
        "raw/v2/sources/date=20260825/hour=03/episode-a/data.mcap",
        "raw/v2/sources/date=2026-08-25/hour=24/episode-a/data.mcap",
        "raw/v2/sources/date=2026-08-25/hour=3/episode-a/data.mcap",
        "raw/v2/sources/date=2026-08-25/hour=03/episode-a/nested/data.mcap",
        "raw/v2/sources/date=2026-08-25/hour=03/../data.mcap",
    ),
)
def test_capture_source_path_rejects_invalid_partitioned_layouts(key):
    assert _capture_source_path(key, require_known_file=True) is None


def test_local_mirror_listing_pages_are_stable_and_do_not_rescan_previous_keys(
    tmp_path, monkeypatch
):
    root = tmp_path / "cloud"
    prefix = root / "bucket-a" / "raw/v2/sources/date=2026-08-25/hour=03"
    for relative_key in (
        "episode-b/complete.json",
        "episode-a/data.mcap",
        "episode-a/metadata.json",
    ):
        path = prefix / relative_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative_key, encoding="utf-8")
    monkeypatch.setattr(oss_client, "_local_mirror_root", lambda: root)

    first = oss_client.list_prefix_page(
        "bucket-a",
        "raw/v2/sources/date=2026-08-25/hour=03",
        continuation_token=None,
        max_keys=2,
    )
    second = oss_client.list_prefix_page(
        "bucket-a",
        "raw/v2/sources/date=2026-08-25/hour=03",
        continuation_token=first.next_token,
        max_keys=2,
    )

    assert [item["key"] for item in first.objects] == [
        "raw/v2/sources/date=2026-08-25/hour=03/episode-a/data.mcap",
        "raw/v2/sources/date=2026-08-25/hour=03/episode-a/metadata.json",
    ]
    assert first.next_token is not None
    assert [item["key"] for item in second.objects] == [
        "raw/v2/sources/date=2026-08-25/hour=03/episode-b/complete.json",
    ]
    assert second.next_token is None


def test_local_mirror_listing_pages_do_not_materialize_the_full_prefix(tmp_path, monkeypatch):
    """Local fallback must keep the same bounded-page contract as cloud OSS."""
    root = tmp_path / "cloud"
    prefix = root / "bucket-a" / "raw/v2/sources/date=2026-08-25/hour=03"
    for index in range(8):
        path = prefix / f"episode-{index:02d}" / "complete.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(index), encoding="utf-8")
    monkeypatch.setattr(oss_client, "_local_mirror_root", lambda: root)

    def forbidden_rglob(*_args, **_kwargs):
        raise AssertionError("a bounded listing must not materialize mirror.rglob()")

    monkeypatch.setattr(Path, "rglob", forbidden_rglob)

    page = oss_client.list_prefix_page(
        "bucket-a",
        "raw/v2/sources/date=2026-08-25/hour=03",
        continuation_token=None,
        max_keys=2,
    )

    assert [item["key"] for item in page.objects] == [
        "raw/v2/sources/date=2026-08-25/hour=03/episode-00/complete.json",
        "raw/v2/sources/date=2026-08-25/hour=03/episode-01/complete.json",
    ]
    assert page.next_token == page.objects[-1]["key"]


def test_local_mirror_listing_rejects_an_invalid_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(oss_client, "_local_mirror_root", lambda: tmp_path / "cloud")

    with pytest.raises(ValueError, match="continuation"):
        oss_client.list_prefix_page(
            "bucket-a",
            "raw/v2/sources",
            continuation_token="../outside",
            max_keys=2,
        )


def test_cloud_listing_object_validation_uses_the_actual_bucket(monkeypatch):
    checked: list[tuple[str, str]] = []

    def record_bucket(bucket: str, key: str) -> bool:
        checked.append((bucket, key))
        return bucket == "authorized-source-bucket"

    monkeypatch.setattr(oss_client, "_provider_path_is_safe", record_bucket)

    result = oss_client._listing_object_view(
        SimpleNamespace(
            key="incoming/raw/v2/sources/episode-a/complete.json", size=1, last_modified=None
        ),
        "authorized-source-bucket",
        "incoming/raw/v2/sources/",
    )

    assert result is not None
    assert checked == [
        ("authorized-source-bucket", "incoming/raw/v2/sources/episode-a/complete.json")
    ]


def test_local_mirror_listing_rejects_a_dangling_symlink_prefix(tmp_path, monkeypatch):
    """A dangling local mirror symlink is unsafe, not an empty OSS prefix."""
    root = tmp_path / "cloud"
    bucket_root = root / "bucket-a"
    bucket_root.mkdir(parents=True)
    (bucket_root / "raw").symlink_to(tmp_path / "missing-target")
    monkeypatch.setattr(oss_client, "_local_mirror_root", lambda: root)

    with pytest.raises(ValueError, match="symlink"):
        oss_client.list_prefix_page(
            "bucket-a",
            "raw/v2/sources",
            continuation_token=None,
            max_keys=2,
        )


def test_legacy_oss_scan_can_exceed_the_browser_candidate_page_limit(db_session, monkeypatch):
    """Compatibility scans use the worker budget, not the 500-row API cap."""
    import data.services.import_intake as intake

    actor = db_session.query(User).order_by(User.id).first()
    assert actor is not None
    suffix = uuid4().hex
    workspace = Workspace(name=f"legacy scan workspace {suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    task_set = TaskSet(workspace_id=workspace.id, name=f"legacy scan task set {suffix}")
    task_label = TaskLabel(key=f"legacy-scan-{suffix}", name="record")
    db_session.add_all((task_set, task_label))
    db_session.flush()
    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name=f"legacy scan batch {suffix}",
        batch_type="teleop",
        actor_id=actor.id,
    )
    db_session.add(
        ExternalOssImportScope(
            workspace_id=workspace.id,
            task_set_id=task_set.id,
            bucket="legacy-source-bucket",
            prefixes_json=["legacy"],
        )
    )
    import_session = create_import_session(
        db_session,
        batch_id=batch.id,
        import_type="oss_scan",
        actor_id=actor.id,
        task_label_id=task_label.id,
    )
    db_session.commit()

    objects = [{"key": f"legacy/candidate-{index:04d}.zip", "size": 1} for index in range(501)]
    monkeypatch.setattr(intake, "allow_oss_import", lambda: True)
    monkeypatch.setattr(settings, "import_scan_candidate_limit", 600, raising=False)
    monkeypatch.setattr(settings, "import_scan_legacy_object_limit", 600, raising=False)
    monkeypatch.setattr(oss_client, "list_prefix", lambda *_args, **_kwargs: objects)

    candidates = intake._scan_oss_candidates(
        db_session,
        import_session=import_session,
        batch=batch,
    )

    assert len(candidates) == 501


def test_partitioned_capture_scan_resumes_from_committed_page_and_exceeds_browser_page_limit(
    db_session,
    monkeypatch,
):
    """V2 capture discovery commits each page and never inherits the 500-row API cap."""
    import data.services.import_intake as intake

    actor = db_session.query(User).order_by(User.id).first()
    assert actor is not None
    suffix = uuid4().hex
    workspace = Workspace(name=f"v2 scan workspace {suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    task_set = TaskSet(workspace_id=workspace.id, name=f"v2 scan task set {suffix}")
    task_label = TaskLabel(key=f"v2-scan-{suffix}", name="record")
    db_session.add_all((task_set, task_label))
    db_session.flush()
    db_session.add(
        ExternalOssImportScope(
            workspace_id=workspace.id,
            task_set_id=task_set.id,
            bucket="v2-source-bucket",
            prefixes_json=["incoming/raw/v2/sources"],
        )
    )
    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name=f"v2 scan batch {suffix}",
        batch_type="teleop",
        actor_id=actor.id,
    )
    import_session = create_import_session(
        db_session,
        batch_id=batch.id,
        import_type="oss_scan",
        actor_id=actor.id,
        task_label_id=task_label.id,
    )
    db_session.commit()

    capture_day = datetime.now(timezone.utc).date()
    objects: dict[str, bytes] = {}
    for number in range(3):
        episode_id = f"episode-v2-{number}"
        parent = f"incoming/raw/v2/sources/date={capture_day.isoformat()}/hour=00/{episode_id}"
        data = f"mcap:{episode_id}".encode()
        metadata = json.dumps(
            {
                "qrdf_version": "0.2.0",
                "episode_id": episode_id,
                "data_file": "data.mcap",
                "capture": {"mode": "iphone_ego", "episode_type": "human_demonstration"},
            },
            sort_keys=True,
        ).encode("utf-8")

        def _etag(payload: bytes) -> str:
            return f"etag-{hashlib.sha256(payload).hexdigest()[:16]}"

        marker = json.dumps(
            {
                "schema": "quicdata.capture.upload-complete.v1",
                "schema_version": 1,
                "episode_id": episode_id,
                "ingest_mode": "trusted_offline",
                "objects": {
                    "data.mcap": {"size": len(data), "etag": _etag(data)},
                    "metadata.json": {"size": len(metadata), "etag": _etag(metadata)},
                },
                "completed_at": "2026-08-25T00:00:00Z",
            },
            sort_keys=True,
        ).encode("utf-8")
        objects.update(
            {
                f"{parent}/data.mcap": data,
                f"{parent}/metadata.json": metadata,
                f"{parent}/complete.json": marker,
            }
        )

    monkeypatch.setattr(intake, "allow_oss_import", lambda: True)
    monkeypatch.setattr(settings, "import_scan_default_days", 1, raising=False)
    monkeypatch.setattr(settings, "import_scan_page_size", 2, raising=False)
    monkeypatch.setattr(settings, "import_scan_candidate_limit", 20_000, raising=False)

    def object_info(_bucket: str, key: str):
        payload = objects.get(key)
        if payload is None:
            return None
        return OSSObjectInfo(
            size=len(payload),
            etag=f"etag-{hashlib.sha256(payload).hexdigest()[:16]}",
            crc64=None,
            version_id="version-1",
            metadata={},
        )

    def read_json_object(_bucket: str, key: str, *, if_match: str | None = None, **_kwargs):
        info = object_info(_bucket, key)
        assert info is not None
        assert if_match == info.etag
        return json.loads(objects[key].decode("utf-8"))

    failed_once = {"value": False}

    def list_prefix_directory_page(
        _bucket: str, prefix: str, *, continuation_token: str | None, max_keys: int
    ):
        if prefix == "incoming/raw/v2/sources":
            return OSSDirectoryPage(prefixes=(), next_token=None)
        if prefix.endswith("/hour=01") and not failed_once["value"]:
            failed_once["value"] = True
            raise OSError("injected continuation interruption")
        keys = sorted(
            f"{key.rsplit('/', 1)[0]}/"
            for key in objects
            if key.startswith(prefix) and key.endswith("/complete.json")
        )
        if continuation_token is not None:
            keys = [key for key in keys if key > continuation_token]
        page_keys = keys[:max_keys]
        next_token = page_keys[-1] if len(keys) > len(page_keys) else None
        return OSSDirectoryPage(
            prefixes=tuple(page_keys),
            next_token=next_token,
        )

    monkeypatch.setattr(oss_client, "object_info", object_info)
    monkeypatch.setattr(oss_client, "read_json_object", read_json_object)
    monkeypatch.setattr(oss_client, "list_prefix_directory_page", list_prefix_directory_page)

    job, created = start_import_candidate_scan(db_session, import_session_id=import_session.id)
    assert created is True
    db_session.commit()
    with pytest.raises(OSError, match="continuation interruption"):
        run_import_candidate_scan(db_session, job)

    db_session.expire_all()
    interrupted = db_session.get(ImportSession, import_session.id)
    assert interrupted is not None
    assert interrupted.scan_cursor_json["hour"] == 1
    assert (
        db_session.query(ImportCandidate)
        .filter(ImportCandidate.import_session_id == import_session.id)
        .count()
        == 3
    )

    job.retry_count = 1
    db_session.commit()
    result = run_import_candidate_scan(db_session, job)
    assert result["candidate_count"] == 3
    db_session.expire_all()
    completed = db_session.get(ImportSession, import_session.id)
    assert completed is not None
    assert completed.result_json["scan_status"] == "succeeded"
    assert completed.scan_cursor_json == {"version": 1, "phase": "complete"}


def test_v2_scan_discovers_historical_flat_packages_before_date_partitioned_packages(
    db_session,
    monkeypatch,
):
    """A V2 root must dual-read flat history and the new date/hour layout."""
    import data.services.capture_batch_import as capture_import
    import data.services.import_intake as intake

    actor = db_session.query(User).order_by(User.id).first()
    assert actor is not None
    suffix = uuid4().hex
    workspace = Workspace(name=f"flat v2 scan workspace {suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    task_set = TaskSet(workspace_id=workspace.id, name=f"flat v2 scan task set {suffix}")
    task_label = TaskLabel(key=f"flat-v2-scan-{suffix}", name="record")
    db_session.add_all((task_set, task_label))
    db_session.flush()
    root = "incoming/raw/v2/sources"
    db_session.add(
        ExternalOssImportScope(
            workspace_id=workspace.id,
            task_set_id=task_set.id,
            bucket="flat-v2-source-bucket",
            prefixes_json=[root],
        )
    )
    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name=f"flat v2 scan batch {suffix}",
        batch_type="teleop",
        actor_id=actor.id,
    )
    import_session = create_import_session(
        db_session,
        batch_id=batch.id,
        import_type="oss_scan",
        actor_id=actor.id,
        task_label_id=task_label.id,
    )
    db_session.commit()

    capture_day = datetime.now(timezone.utc).date()
    flat_complete = f"{root}/episode-flat-history/complete.json"
    partitioned_complete = (
        f"{root}/date={capture_day.isoformat()}/hour=00/episode-partitioned/complete.json"
    )
    monkeypatch.setattr(intake, "allow_oss_import", lambda: True)
    monkeypatch.setattr(settings, "import_scan_default_days", 1, raising=False)

    def list_prefix_directory_page(
        _bucket: str, prefix: str, *, continuation_token: str | None, max_keys: int
    ):
        assert continuation_token is None
        assert max_keys >= 1
        if prefix == root:
            return OSSDirectoryPage(prefixes=(f"{root}/episode-flat-history/",), next_token=None)
        if prefix.endswith(f"date={capture_day.isoformat()}/hour=00"):
            return OSSDirectoryPage(
                prefixes=(f"{partitioned_complete.rsplit('/', 1)[0]}/",),
                next_token=None,
            )
        return OSSDirectoryPage(prefixes=(), next_token=None)

    def scan_candidate(_db, *, object_key: object, **_kwargs):
        assert object_key in {flat_complete, partitioned_complete}
        episode_id = str(object_key).rsplit("/", maxsplit=2)[-2]
        return ImportCandidate(
            id=str(uuid4()),
            import_session_id=import_session.id,
            candidate_type="capture_episode_oss",
            status="discovered",
            original_name=episode_id,
            size_bytes=1,
            source_fingerprint=hashlib.sha256(str(object_key).encode("utf-8")).hexdigest(),
            locator_json={"fixture": "v2-flat-and-partitioned"},
        )

    monkeypatch.setattr(oss_client, "list_prefix_directory_page", list_prefix_directory_page)
    monkeypatch.setattr(capture_import, "scan_capture_episode_candidate", scan_candidate)

    job, created = start_import_candidate_scan(db_session, import_session_id=import_session.id)
    assert created is True
    db_session.commit()

    result = run_import_candidate_scan(db_session, job)

    assert result["candidate_count"] == 2
    assert {
        candidate.original_name
        for candidate in db_session.query(ImportCandidate)
        .filter(ImportCandidate.import_session_id == import_session.id)
        .all()
    } == {"episode-flat-history", "episode-partitioned"}


def test_partitioned_v2_scan_also_discovers_a_separate_legacy_ego_scope(
    db_session,
    monkeypatch,
):
    """A V2 scope must not make separately authorized legacy scopes invisible."""
    import data.services.capture_batch_import as capture_import
    import data.services.historical_ego_import as historical_ego_import
    import data.services.import_intake as intake

    actor = db_session.query(User).order_by(User.id).first()
    assert actor is not None
    suffix = uuid4().hex
    workspace = Workspace(name=f"mixed scan workspace {suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    task_set = TaskSet(workspace_id=workspace.id, name=f"mixed scan task set {suffix}")
    task_label = TaskLabel(key=f"mixed-scan-{suffix}", name="record")
    db_session.add_all((task_set, task_label))
    db_session.flush()
    v2_root = "incoming/raw/v2/sources"
    legacy_root = "incoming/raw/v1/tasks"
    db_session.add_all(
        (
            ExternalOssImportScope(
                workspace_id=workspace.id,
                task_set_id=task_set.id,
                bucket="mixed-v2-bucket",
                prefixes_json=[v2_root],
            ),
            ExternalOssImportScope(
                workspace_id=workspace.id,
                task_set_id=task_set.id,
                bucket="mixed-legacy-bucket",
                prefixes_json=[legacy_root],
            ),
        )
    )
    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name=f"mixed scan batch {suffix}",
        batch_type="teleop",
        actor_id=actor.id,
    )
    import_session = create_import_session(
        db_session,
        batch_id=batch.id,
        import_type="oss_scan",
        actor_id=actor.id,
        task_label_id=task_label.id,
    )
    db_session.commit()

    capture_day = datetime.now(timezone.utc).date()
    v2_complete = f"{v2_root}/date={capture_day.isoformat()}/hour=00/episode-v2/complete.json"
    legacy_marker = (
        f"{legacy_root}/legacy-group/devices/device-a/episodes/episode-legacy/complete.json"
    )
    legacy_listed_prefixes: list[tuple[str, str]] = []
    monkeypatch.setattr(intake, "allow_oss_import", lambda: True)
    monkeypatch.setattr(settings, "import_scan_default_days", 1, raising=False)

    def list_prefix_directory_page(
        _bucket: str, prefix: str, *, continuation_token: str | None, max_keys: int
    ):
        assert continuation_token is None
        assert max_keys >= 1
        if prefix.endswith(f"date={capture_day.isoformat()}/hour=00"):
            return OSSDirectoryPage(
                prefixes=(f"{v2_complete.rsplit('/', 1)[0]}/",),
                next_token=None,
            )
        return OSSDirectoryPage(prefixes=(), next_token=None)

    def list_prefix(bucket: str, prefix: str, *, limit: int | None = None, **_kwargs):
        legacy_listed_prefixes.append((bucket, prefix))
        assert bucket == "mixed-legacy-bucket"
        assert prefix == legacy_root
        assert limit is not None
        return [{"key": legacy_marker, "size": 1}]

    def scan_capture_candidate(_db, *, object_key: object, **_kwargs):
        assert object_key == v2_complete
        return ImportCandidate(
            id=str(uuid4()),
            import_session_id=import_session.id,
            candidate_type="capture_episode_oss",
            status="discovered",
            original_name="episode-v2",
            size_bytes=1,
            source_fingerprint="v2-fingerprint",
            locator_json={"fixture": "mixed-v2"},
        )

    def scan_legacy_candidates(_db, *, bucket: str, objects, **_kwargs):
        assert bucket == "mixed-legacy-bucket"
        assert list(objects) == [{"key": legacy_marker, "size": 1}]
        return [
            ImportCandidate(
                id=str(uuid4()),
                import_session_id=import_session.id,
                candidate_type="ego_episode_oss",
                status="discovered",
                original_name="episode-legacy",
                size_bytes=1,
                source_fingerprint="legacy-fingerprint",
                locator_json={"fixture": "mixed-legacy"},
            )
        ]

    monkeypatch.setattr(oss_client, "list_prefix_directory_page", list_prefix_directory_page)
    monkeypatch.setattr(oss_client, "list_prefix", list_prefix)
    monkeypatch.setattr(capture_import, "scan_capture_episode_candidate", scan_capture_candidate)
    monkeypatch.setattr(
        historical_ego_import, "scan_ego_episode_candidates", scan_legacy_candidates
    )

    job, created = start_import_candidate_scan(db_session, import_session_id=import_session.id)
    assert created is True
    db_session.commit()

    result = run_import_candidate_scan(db_session, job)

    assert result["candidate_count"] == 2
    assert legacy_listed_prefixes == [("mixed-legacy-bucket", legacy_root)]
    assert {
        candidate.original_name
        for candidate in db_session.query(ImportCandidate)
        .filter(ImportCandidate.import_session_id == import_session.id)
        .all()
    } == {"episode-v2", "episode-legacy"}


def test_local_mirror_listing_stops_after_a_page_continuation_sentinel(tmp_path, monkeypatch):
    """A first local page must not recurse into lexically later payload trees."""
    root = tmp_path / "cloud"
    prefix = root / "bucket-a" / "raw/v2/sources/date=2026-08-25/hour=03"
    for name in ("episode-a", "episode-b", "episode-c"):
        path = prefix / name / "complete.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name, encoding="utf-8")
    # This is lexically after the requested page and its sentinel. A full-tree
    # walk would inspect it and reject it as a symlink; a bounded page must
    # stop before entering it.
    later = prefix / "episode-z-after-page"
    later.symlink_to(tmp_path / "outside")
    monkeypatch.setattr(oss_client, "_local_mirror_root", lambda: root)

    page = oss_client.list_prefix_page(
        "bucket-a",
        "raw/v2/sources/date=2026-08-25/hour=03",
        continuation_token=None,
        max_keys=2,
    )

    assert [item["key"] for item in page.objects] == [
        "raw/v2/sources/date=2026-08-25/hour=03/episode-a/complete.json",
        "raw/v2/sources/date=2026-08-25/hour=03/episode-b/complete.json",
    ]
    assert page.next_token == "raw/v2/sources/date=2026-08-25/hour=03/episode-b/complete.json"


def test_local_mirror_listing_continuation_skips_finished_sibling_subtrees(tmp_path, monkeypatch):
    """A continuation resumes after its key instead of walking prior siblings again."""
    root = tmp_path / "cloud"
    prefix = root / "bucket-a" / "raw/v2/sources/date=2026-08-25/hour=03"
    for name in ("episode-0-before", "episode-a", "episode-b"):
        path = prefix / name / "complete.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name, encoding="utf-8")
    monkeypatch.setattr(oss_client, "_local_mirror_root", lambda: root)

    first = oss_client.list_prefix_page(
        "bucket-a",
        "raw/v2/sources/date=2026-08-25/hour=03",
        continuation_token=None,
        max_keys=2,
    )
    assert first.next_token is not None

    prior_subtree = prefix / "episode-0-before"
    (prior_subtree / "complete.json").unlink()
    prior_subtree.rmdir()
    prior_subtree.symlink_to(tmp_path / "outside")

    second = oss_client.list_prefix_page(
        "bucket-a",
        "raw/v2/sources/date=2026-08-25/hour=03",
        continuation_token=first.next_token,
        max_keys=2,
    )

    assert [item["key"] for item in second.objects] == [
        "raw/v2/sources/date=2026-08-25/hour=03/episode-b/complete.json",
    ]
    assert second.next_token is None


def test_local_mirror_directory_listing_reuses_its_bounded_snapshot(tmp_path, monkeypatch):
    """Flat V2 directory continuation pages do not enumerate the root again."""
    root = tmp_path / "cloud"
    prefix = root / "bucket-a" / "raw/v2/sources"
    for name in ("episode-a", "episode-b", "episode-c"):
        (prefix / name).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(oss_client, "_local_mirror_root", lambda: root)

    first = oss_client.list_prefix_directory_page(
        "bucket-a",
        "raw/v2/sources",
        continuation_token=None,
        max_keys=2,
    )
    assert first.next_token is not None

    original_scandir = oss_client.os.scandir

    def forbid_second_root_scan(path):
        if Path(path) == prefix:
            raise AssertionError("continuation must use the existing directory snapshot")
        return original_scandir(path)

    monkeypatch.setattr(oss_client.os, "scandir", forbid_second_root_scan)
    second = oss_client.list_prefix_directory_page(
        "bucket-a",
        "raw/v2/sources",
        continuation_token=first.next_token,
        max_keys=2,
    )

    assert second.prefixes == ("raw/v2/sources/episode-c/",)
    assert second.next_token is None


def test_partitioned_capture_scan_persists_more_than_the_browser_candidate_page_limit(
    db_session,
    monkeypatch,
):
    """The worker scan budget is independent from the browser's 500-row page."""
    import data.services.capture_batch_import as capture_import
    import data.services.import_intake as intake

    actor = db_session.query(User).order_by(User.id).first()
    assert actor is not None
    suffix = uuid4().hex
    workspace = Workspace(name=f"large v2 scan workspace {suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    task_set = TaskSet(workspace_id=workspace.id, name=f"large v2 scan task set {suffix}")
    task_label = TaskLabel(key=f"large-v2-scan-{suffix}", name="record")
    db_session.add_all((task_set, task_label))
    db_session.flush()
    db_session.add(
        ExternalOssImportScope(
            workspace_id=workspace.id,
            task_set_id=task_set.id,
            bucket="large-v2-source-bucket",
            prefixes_json=["incoming/raw/v2/sources"],
        )
    )
    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name=f"large v2 scan batch {suffix}",
        batch_type="teleop",
        actor_id=actor.id,
    )
    import_session = create_import_session(
        db_session,
        batch_id=batch.id,
        import_type="oss_scan",
        actor_id=actor.id,
        task_label_id=task_label.id,
    )
    db_session.commit()

    capture_day = datetime.now(timezone.utc).date()
    root = f"incoming/raw/v2/sources/date={capture_day.isoformat()}/hour=00"
    episode_prefixes = tuple(f"{root}/episode-{index:04d}/" for index in range(501))

    monkeypatch.setattr(intake, "allow_oss_import", lambda: True)
    monkeypatch.setattr(settings, "import_scan_default_days", 1, raising=False)
    monkeypatch.setattr(settings, "import_scan_page_size", 500, raising=False)
    monkeypatch.setattr(settings, "import_scan_candidate_limit", 20_000, raising=False)

    def list_prefix_directory_page(
        _bucket: str, prefix: str, *, continuation_token: str | None, max_keys: int
    ):
        if prefix != root:
            return OSSDirectoryPage(prefixes=(), next_token=None)
        start = int(continuation_token or "0")
        page_prefixes = episode_prefixes[start : start + max_keys]
        next_token = (
            str(start + len(page_prefixes))
            if start + len(page_prefixes) < len(episode_prefixes)
            else None
        )
        return OSSDirectoryPage(
            prefixes=page_prefixes,
            next_token=next_token,
        )

    def scan_candidate(_db, *, object_key: object, **_kwargs):
        assert isinstance(object_key, str)
        episode_id = object_key.rsplit("/", maxsplit=2)[-2]
        return ImportCandidate(
            id=str(uuid4()),
            import_session_id=import_session.id,
            candidate_type="capture_episode_oss",
            status="discovered",
            original_name=episode_id,
            size_bytes=1,
            source_fingerprint=hashlib.sha256(object_key.encode("utf-8")).hexdigest(),
            locator_json={"fixture": "large-v2-scan"},
        )

    monkeypatch.setattr(oss_client, "list_prefix_directory_page", list_prefix_directory_page)

    def forbidden_file_listing(*_args, **_kwargs):
        raise AssertionError("partitioned V2 scan must list Episode directories")

    monkeypatch.setattr(oss_client, "list_prefix_page", forbidden_file_listing)
    monkeypatch.setattr(capture_import, "scan_capture_episode_candidate", scan_candidate)

    job, created = start_import_candidate_scan(db_session, import_session_id=import_session.id)
    assert created is True
    db_session.commit()

    result = run_import_candidate_scan(db_session, job)

    assert result["candidate_count"] == 501
    assert (
        db_session.query(ImportCandidate)
        .filter(ImportCandidate.import_session_id == import_session.id)
        .count()
        == 501
    )
