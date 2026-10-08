"""Unit coverage for OSS-mirror / 4090 mount feasibility helpers."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_SPEC = importlib.util.spec_from_file_location(
    "verify_oss_mirror_train_feasibility",
    _SCRIPTS / "verify_oss_mirror_train_feasibility.py",
)
assert _SPEC and _SPEC.loader
_mod = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_oss_mirror_train_feasibility"] = _mod
_SPEC.loader.exec_module(_mod)

check_catalog_archive_gate = _mod.check_catalog_archive_gate
check_ecs_4090_mount_plan = _mod.check_ecs_4090_mount_plan
check_oss_mirror_mapping = _mod.check_oss_mirror_mapping
plan_ecs_4090_mounts = _mod.plan_ecs_4090_mounts


@pytest.fixture(autouse=True)
def _mirror_env(monkeypatch):
    monkeypatch.setenv(
        "QUICTRAIN_OSS_CPFS_MIRROR_PREFIX",
        "oss://oss-pai-1183v1b6du4vkucj3h-cn-beijing/quictrain",
    )
    monkeypatch.setenv(
        "QUICTRAIN_CPFS_ROOT_URI",
        "bmcpfs://bmcpfs-03001wmodv0g6pynhun3u.cn-beijing",
    )


def test_catalog_archive_gate_blocks_tar_allows_directory():
    result = check_catalog_archive_gate()
    assert result.ok
    assert (
        result.detail["classified"][
            "oss://quicstudio-uat-export/exports/v1/catalog/1/versions/2/lerobot.tar.gz"
        ]["is_archive"]
        is True
    )


def test_oss_mirror_mapping_for_pusht():
    result = check_oss_mirror_mapping()
    assert result.ok
    assert result.detail["mapped_oss_uri"].endswith(
        "/quictrain/datasets/lerobot/quictrain-pusht-smoke-v1"
    )


def test_ecs_4090_mount_plan_from_oss_and_bmcpfs():
    oss_uri = (
        "oss://oss-pai-1183v1b6du4vkucj3h-cn-beijing/"
        "quictrain/datasets/lerobot/quictrain-pusht-smoke-v1"
    )
    plan = plan_ecs_4090_mounts(oss_uri)
    assert plan["dataset_root"] == "/quictrain/input/dataset"
    assert plan["mounts"][0]["source_uri"] == oss_uri
    assert plan["host_cpfs_required"] is False

    cpfs_uri = (
        "bmcpfs://bmcpfs-03001wmodv0g6pynhun3u.cn-beijing/"
        "quictrain/datasets/lerobot/quictrain-pusht-smoke-v1"
    )
    mapped = plan_ecs_4090_mounts(cpfs_uri)
    assert mapped["mount_uri"] == oss_uri
    assert check_ecs_4090_mount_plan(cpfs_uri).ok


def test_oss_mirror_mapping_requires_prefix(monkeypatch):
    monkeypatch.setenv("QUICTRAIN_OSS_CPFS_MIRROR_PREFIX", "")
    monkeypatch.delenv("ALIYUN_OSS_BUCKET", raising=False)
    os.environ.pop("ALIYUN_OSS_BUCKET", None)
    result = check_oss_mirror_mapping()
    assert result.ok is False
