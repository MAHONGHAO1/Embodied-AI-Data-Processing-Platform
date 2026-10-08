"""Studio-side registration rules. These do not touch the train database."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from auth_bridge import project_role_for_permissions
from catalog_bridge import (
    CatalogRegistrationRequest,
    export_dataset_metadata,
    register_catalog_export,
    register_stable_export,
    registration_block_reason,
    stable_oss_uri,
)


def verified_export_metadata(info=None):
    if info is None:
        info = {
            "codebase_version": "v3.0",
            "total_episodes": 2,
            "total_frames": 194,
            "total_tasks": 2,
            "fps": 10.0,
            "features": {"observation.images.head": {"dtype": "video", "shape": [3, 720, 960]}},
            "export_mode": "ego_rgb",
            "qrdf_lerobot_artifacts": {"export_kind": "ego", "policy_io": False},
        }
    raw = json.dumps(info)
    return {
        "lerobot_metadata": {
            "schema": "quicstudio.lerobot-export-metadata.v1",
            "archive_sha256": "a" * 64,
            "info_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "info_json": raw,
        }
    }


def verified_export():
    return {
        "format": "lerobot_3_0",
        "status": "succeeded",
        "checksum": "a" * 64,
        "detail_json": {
            "oss_uri": "oss://bucket/train/dataset.tar.gz",
            **verified_export_metadata(),
        },
    }


def test_placeholder_export_cannot_register():
    export = {
        "checksum": "abc",
        "detail_json": {"asset_count": 1},
        "format": "lerobot_3_0",
        "status": "succeeded",
    }
    assert stable_oss_uri(export) is None
    assert "oss://" in (registration_block_reason(export) or "")


def test_stable_oss_uri_without_verified_metadata_cannot_register():
    export = {
        "format": "lerobot_3_0",
        "status": "succeeded",
        "detail_json": {"oss_uri": "oss://bucket/train/v1"},
    }
    assert stable_oss_uri(export) == "oss://bucket/train/v1"
    assert "元数据" in registration_block_reason(export)


def test_verified_export_can_register_without_inventing_policy_features():
    export = verified_export()
    assert registration_block_reason(export) is None
    metadata = export_dataset_metadata(export)
    assert metadata["episodes"] == 2
    assert metadata["frames"] == 194
    assert metadata["fps"] == 10.0
    assert metadata["camera_keys"] == ["observation.images.head"]
    assert metadata["robot_type"] is None
    assert metadata["action_dim"] == metadata["state_dim"] == 0
    assert metadata["duration_hours"] == pytest.approx(19.4 / 3600)


@pytest.mark.parametrize("changed", ["archive_sha256", "info_json"])
def test_registration_rejects_metadata_from_different_bytes(changed):
    export = verified_export()
    export["detail_json"]["lerobot_metadata"][changed] = "changed"
    assert "不可变身份" in registration_block_reason(export)


def test_actual_export_is_registered_and_remains_incompatible_with_policy_models(
    tmp_path, monkeypatch
):
    from quictrain_model_specs import MODEL_REGISTRY, compatibility_issues
    from tests.db_helpers import rebind_database

    import quictrain_api.db as db
    from quictrain_api.service import dataset_from_record

    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/catalog-bridge.db")
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    rebind_database()
    db.init_database()
    result = register_stable_export(
        export=verified_export(),
        actor_id="usr_demo",
        display_name="Actual ego samples",
        dataset_id="catalog-2",
        version="v1-export-5",
    )
    assert result["status"] == "REGISTERED"
    assert result["checksum"] == "sha256:" + "a" * 64
    with db.SessionLocal() as session:
        record = session.get(db.DatasetVersionRecord, result["dataset_version_id"])
        dataset = dataset_from_record(record)
        assert dataset.episodes == 2 and dataset.frames == 194 and dataset.fps == 10
        assert dataset.camera_keys == ["observation.images.head"]
        assert dataset.action_dim == dataset.state_dim == 0
        assert session.query(db.MaterializationAttemptRecord).count() == 0
        assert session.query(db.JobRecord).count() == 0
        for model in MODEL_REGISTRY.values():
            issues = compatibility_issues(dataset, model)
            assert any(issue["code"] == "DATASET_NOT_READY" for issue in issues)
            ready_issues = compatibility_issues(
                dataset.model_copy(update={"status": "READY"}), model
            )
            assert {
                issue["field"]
                for issue in ready_issues
                if issue["code"] == "POLICY_FEATURE_MISSING"
            } == {"action_dim", "state_dim"}


def test_registration_rejects_unstable_status_and_non_lerobot_exports():
    stable_uri = {"detail_json": {"oss_uri": "oss://bucket/train/v1"}}
    assert "LeRobot" in (
        registration_block_reason({**stable_uri, "status": "succeeded", "format": "qrdf_0_2"}) or ""
    )
    assert "完成" in (
        registration_block_reason(
            {**stable_uri, "status": "running", "format": "lerobot_3_0"},
        )
        or ""
    )
    assert "oss://" in (
        registration_block_reason(
            {"status": "succeeded", "format": "lerobot_3_0", "detail_json": {}},
        )
        or ""
    )


def test_registration_ignores_newer_qrdf_and_failed_exports(monkeypatch):
    """The route chooses a stable LeRobot artifact instead of the newest attempt."""
    from data.models.catalog_dataset import (
        CatalogDataset,
        CatalogDatasetExport,
        CatalogDatasetVersion,
    )

    try:
        import train.mount as train_mount
    except ModuleNotFoundError:
        import mount as train_mount

    version = SimpleNamespace(id=42, dataset_id=7, version=3)
    exports = [
        SimpleNamespace(
            id=99,
            version_id=42,
            format="qrdf_0_2",
            status="succeeded",
            checksum="qrdf",
            detail_json={"oss_uri": "oss://bucket/qrdf"},
            oss_uri=None,
        ),
        SimpleNamespace(
            id=98,
            version_id=42,
            format="lerobot_3_0",
            status="failed",
            checksum="failed",
            detail_json={"oss_uri": "oss://bucket/failed"},
            oss_uri=None,
        ),
        SimpleNamespace(
            id=97,
            version_id=42,
            format="lerobot_3_0",
            status="succeeded",
            checksum="a" * 64,
            detail_json={"oss_uri": "oss://bucket/stable", **verified_export_metadata()},
            oss_uri=None,
        ),
    ]

    class FakeQuery:
        def __init__(self, result=None, rows=()):
            self.result = result
            self.rows = list(rows)

        def filter(self, *_conditions):
            return self

        def one_or_none(self):
            return self.result

        def order_by(self, *_ordering):
            return self

        def __iter__(self):
            return iter(self.rows)

    class FakeDB:
        def query(self, model):
            if model is CatalogDatasetVersion:
                return FakeQuery(result=version)
            assert model is CatalogDatasetExport
            return FakeQuery(rows=exports)

        def get(self, model, _key):
            assert model is CatalogDataset
            return SimpleNamespace(name="Robot trajectories")

    captured = {}
    monkeypatch.setattr(train_mount, "startup_train", lambda: None)
    monkeypatch.setattr(
        "catalog_bridge.register_stable_export",
        lambda **kwargs: captured.update(kwargs) or {"dataset_version_id": "dsv-1"},
    )

    result = register_catalog_export(
        CatalogRegistrationRequest(version_id=42),
        db=FakeDB(),
        user={"role": "admin", "sub": "admin-1"},
    )

    assert result["data"]["dataset_version_id"] == "dsv-1"
    assert captured["export"]["id"] == 97
    assert captured["export"]["format"] == "lerobot_3_0"
    assert captured["export"]["status"] == "succeeded"


def test_registration_requires_admin_and_rejects_cross_version_export(monkeypatch):
    """Permission is checked before catalog disclosure and explicit ids stay version-scoped."""
    from fastapi import HTTPException

    from data.models.catalog_dataset import CatalogDatasetExport, CatalogDatasetVersion

    class FakeQuery:
        def __init__(self, result):
            self.result = result

        def filter(self, *_conditions):
            return self

        def one_or_none(self):
            return self.result

    class FakeDB:
        def __init__(self, export):
            self.export = export
            self.queries = 0

        def query(self, model):
            self.queries += 1
            if model is CatalogDatasetVersion:
                return FakeQuery(SimpleNamespace(id=42, dataset_id=7, version=3))
            assert model is CatalogDatasetExport
            return FakeQuery(self.export)

    denied_db = FakeDB(None)
    with pytest.raises(HTTPException) as denied:
        register_catalog_export(
            CatalogRegistrationRequest(version_id=42),
            db=denied_db,
            user={"role": "operator", "sub": "operator-1"},
        )
    assert denied.value.status_code == 403
    assert denied_db.queries == 0

    cross_version = SimpleNamespace(
        id=123,
        version_id=999,
        format="lerobot_3_0",
        status="succeeded",
        checksum="stable",
        detail_json={"oss_uri": "oss://bucket/other-version"},
        oss_uri=None,
    )
    with pytest.raises(HTTPException) as mismatch:
        register_catalog_export(
            CatalogRegistrationRequest(version_id=42, export_id=123),
            db=FakeDB(cross_version),
            user={"role": "admin", "sub": "admin-1"},
        )
    assert mismatch.value.status_code == 404
    assert "does not belong" in str(mismatch.value.detail)


def test_mounted_health_stays_public_and_other_routes_require_studio_login():
    from auth_bridge import StudioAuthBridge
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from quictrain_api.main import app as train_app

    outer = FastAPI()
    outer.mount("/api/train", StudioAuthBridge(train_app))
    client = TestClient(outer)
    health = client.get("/api/train/health")
    assert health.status_code == 200
    denied = client.get("/api/train/api/v1/jobs")
    assert denied.status_code == 401
    assert "未登录" in denied.text


def test_studio_permissions_map_to_train_roles():
    assert project_role_for_permissions(["*"]) == "admin"
    assert project_role_for_permissions(["train:write"]) == "operator"
    assert project_role_for_permissions(["train:*"]) == "operator"
    assert project_role_for_permissions(["train:read"]) is None
    assert project_role_for_permissions(["dataset:write"]) is None
    assert project_role_for_permissions(["dataset:read"]) is None
    assert project_role_for_permissions(["episode:read"]) is None
