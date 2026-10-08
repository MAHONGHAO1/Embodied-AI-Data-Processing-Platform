"""Test environment: shared fixtures and QRDF SDK bootstrap."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

os.environ.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
os.environ["TEST_MODE"] = "true"
os.environ["STORAGE_PROVIDER"] = "minio"
os.environ["STORAGE_ENDPOINT"] = os.environ.get("TEST_STORAGE_ENDPOINT", "http://127.0.0.1:19000")
os.environ["STORAGE_ACCESS_KEY_ID"] = "quicstudio-test"
os.environ["STORAGE_SECRET_ACCESS_KEY"] = "quicstudio-test-secret"
os.environ["SCRATCH_ROOT"] = "/tmp/quicstudio-test-scratch"
os.environ["OSS_BUCKET_RAW"] = "quicstudio-test-raw"
os.environ["OSS_BUCKET_PROCESS"] = "quicstudio-test-process"
os.environ["OSS_BUCKET_EXPORT"] = "quicstudio-test-export"
os.environ["DEV_OSS_ENABLED"] = "false"
os.environ["OSS_ACCESS_KEY_ID"] = ""
os.environ["OSS_ACCESS_KEY_SECRET"] = ""
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/15").strip()
_test_redis = urlsplit(TEST_REDIS_URL)
if _test_redis.hostname not in {"127.0.0.1", "localhost", "::1"}:
    raise RuntimeError("TEST_REDIS_URL must point to a loopback Redis instance")
try:
    _test_redis_db = int((_test_redis.path or "").lstrip("/"))
except ValueError as exc:
    raise RuntimeError("TEST_REDIS_URL must include an isolated numeric database") from exc
if not 8 <= _test_redis_db <= 15:
    raise RuntimeError("TEST_REDIS_URL database must be between 8 and 15")
os.environ["REDIS_URL"] = TEST_REDIS_URL
os.environ["CELERY_BROKER_URL"] = _test_redis._replace(path="/13").geturl()
os.environ["CELERY_RESULT_BACKEND"] = _test_redis._replace(path="/14").geturl()
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
if not TEST_DATABASE_URL:
    raise RuntimeError("TEST_DATABASE_URL must point to an isolated PostgreSQL test database")
if not TEST_DATABASE_URL.startswith(("postgresql://", "postgresql+psycopg://")):
    raise RuntimeError("TEST_DATABASE_URL must be a PostgreSQL URL")
if not TEST_DATABASE_URL.rsplit("/", maxsplit=1)[-1].startswith("quicdata_test"):
    raise RuntimeError("TEST_DATABASE_URL database name must start with quicdata_test")
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

import hashlib
from dataclasses import dataclass

import pytest
import redis
from fastapi.testclient import TestClient
from scripts.seed_dev import seed_development_data
from sqlalchemy import create_engine, text

import data.bootstrap  # noqa: F401
from data.config import settings
from data.database import RealtimeEvent, SessionLocal, engine
from data.main import app
from data.runtime import upgrade_database


@dataclass
class InMemoryObjectStore:
    """Provider transport double for tests that exercise OSS-backed paths."""

    objects: dict[tuple[str, str], bytes]

    def object_info(self, bucket: str, key: str):
        from data.infra.oss_client import OSSObjectInfo

        payload = self.objects.get((bucket, key))
        if payload is None:
            return None
        digest = hashlib.sha256(payload).hexdigest()
        return OSSObjectInfo(len(payload), digest, None, digest[:16], {})

    def object_exists(self, bucket: str, key: str) -> bool:
        return (bucket, key) in self.objects

    def upload_file(self, local_path, bucket: str, key: str, **_kwargs):
        from pathlib import Path

        source = Path(local_path)
        if source.is_dir():
            for item in source.rglob("*"):
                if item.is_file() and not item.is_symlink():
                    self.objects[
                        (bucket, f"{key.rstrip('/')}/{item.relative_to(source).as_posix()}")
                    ] = item.read_bytes()
        else:
            self.objects[(bucket, key)] = source.read_bytes()
        return f"oss://{bucket}/{key}"

    def download_to(self, local_dest, bucket: str, key: str):
        from pathlib import Path

        destination = Path(local_dest)
        destination.mkdir(parents=True, exist_ok=True)
        matching = [
            (object_key, payload)
            for (object_bucket, object_key), payload in self.objects.items()
            if object_bucket == bucket
            and (object_key == key or object_key.startswith(f"{key.rstrip('/')}/"))
        ]
        if not matching:
            raise FileNotFoundError(f"object does not exist: oss://{bucket}/{key}")
        for object_key, payload in matching:
            relative = (
                object_key[len(key.rstrip("/")).lstrip("/")]
                if object_key != key
                else Path(object_key).name
            )
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        return destination

    def download_object_to_file(self, local_file, bucket: str, key: str):
        from pathlib import Path

        payload = self.objects.get((bucket, key))
        if payload is None:
            raise FileNotFoundError(f"object does not exist: oss://{bucket}/{key}")
        target = Path(local_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return target

    def delete_object(self, bucket: str, key: str):
        self.objects.pop((bucket, key), None)

    def copy_object(
        self, source_bucket: str, source_key: str, target_bucket: str, target_key: str, **_kwargs
    ):
        payload = self.objects.get((source_bucket, source_key))
        if payload is None:
            raise FileNotFoundError(f"object does not exist: oss://{source_bucket}/{source_key}")
        if (target_bucket, target_key) in self.objects:
            raise FileExistsError(f"object exists: oss://{target_bucket}/{target_key}")
        self.objects[(target_bucket, target_key)] = payload
        return f"oss://{target_bucket}/{target_key}"


@pytest.fixture
def memory_object_store(monkeypatch):
    from data.infra import oss_client

    store = InMemoryObjectStore({})
    for name in (
        "object_info",
        "object_exists",
        "upload_file",
        "download_to",
        "download_object_to_file",
        "delete_object",
        "copy_object",
    ):
        monkeypatch.setattr(oss_client, name, getattr(store, name))
    return store


@pytest.fixture(scope="session", autouse=True)
def migrated_test_database():
    """Reset only the explicitly named PostgreSQL test database."""
    redis_urls = {
        TEST_REDIS_URL,
        os.environ["CELERY_BROKER_URL"],
        os.environ["CELERY_RESULT_BACKEND"],
    }
    for redis_url in redis_urls:
        client = redis.from_url(redis_url, decode_responses=True)
        client.ping()
        client.flushdb()
    reset_engine = create_engine(
        TEST_DATABASE_URL, isolation_level="AUTOCOMMIT", pool_pre_ping=True
    )
    try:
        with reset_engine.connect() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
            connection.execute(text("GRANT ALL ON SCHEMA public TO CURRENT_USER"))
    finally:
        reset_engine.dispose()
    upgrade_database(settings.database_url)
    seed_development_data()
    yield
    engine.dispose()
    for redis_url in redis_urls:
        redis.from_url(redis_url, decode_responses=True).flushdb()


@pytest.fixture(autouse=True)
def isolated_realtime_outbox():
    """Keep committed realtime events from leaking between test cases."""
    db = SessionLocal()
    try:
        db.query(RealtimeEvent).delete(synchronize_session=False)
        db.commit()
        yield
    finally:
        db.rollback()
        db.query(RealtimeEvent).delete(synchronize_session=False)
        db.commit()
        db.close()


@pytest.fixture
def client():
    """Return a client backed by the Alembic-migrated isolated test database."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db_session():
    """A PostgreSQL-backed ORM session for the reset-domain tests."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def admin_headers(client):
    res = client.post(
        "/api/v1/auth/login", json={"email": "admin@quicdata.com", "password": "admin123"}
    )
    assert res.status_code == 200
    token = res.json()["data"]["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def annotator_headers(client):
    res = client.post(
        "/api/v1/auth/login", json={"email": "annotator@quicdata.com", "password": "annotator123"}
    )
    if res.json().get("code") != 200:
        pytest.skip("annotator 种子用户未初始化")
    token = res.json()["data"]["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def auditor_headers(client):
    res = client.post(
        "/api/v1/auth/login", json={"email": "auditor@quicdata.com", "password": "auditor123"}
    )
    if res.json().get("code") != 200:
        pytest.skip("auditor 种子用户未初始化")
    token = res.json()["data"]["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def operator_headers(annotator_headers):
    # The operator role is retired (see refactor(rbac)); this fixture is a
    # legacy alias kept so existing tests keep a non-admin principal. It does
    # NOT test an operator identity; new RBAC tests must use the actual role.
    return annotator_headers


@pytest.fixture
def viewer_headers(auditor_headers):
    # The viewer role is not part of the RBAC defaults either; keep the audit
    # role as the non-admin read principal for legacy tests only. This does
    # NOT provide coverage of unconfigured viewer identities.
    return auditor_headers


@pytest.fixture
def auth_headers(admin_headers):
    return admin_headers


@pytest.fixture
def tmp_storage(monkeypatch, tmp_path: Path):
    hot = tmp_path / "hot"
    chunks = tmp_path / "chunks"
    exports = tmp_path / "exports"
    for d in (hot, chunks, exports):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    return tmp_path
