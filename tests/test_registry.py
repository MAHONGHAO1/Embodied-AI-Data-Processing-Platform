"""Dataset handoff cards and the local registry must be re-checkable."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from robodata.cli import main
from robodata.dataset_card import build_card, card_digest, verify_card
from robodata.registry import (RegistryConflict, list_datasets, register_card, show,
                               verify_card_path)


def _fixture(tmp_path: Path) -> tuple[dict, dict, dict, Path]:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "source_manifest.json").write_text(json.dumps({
        "conversion_strategy": {"name": "preview_video_reuse", "schema_version": 1,
                                "source_numeric_consistency_rechecked": True,
                                "preview_video_hashes_verified": True,
                                "preview_data_hashes_verified": True,
                                "video_files_reused": 4, "fallback_reason": None},
        "limitations": ["不声明相机硬件同步"],
    }), encoding="utf-8")
    (dataset / "annotations.json").write_text("{}", encoding="utf-8")
    run = tmp_path / "run"
    (run / "steps").mkdir(parents=True)
    (run / "reports").mkdir()
    (run / "steps/official_verify.json").write_text(json.dumps({
        "stage": "official_verify", "status": "passed",
        "details": {"output_loader_consistency_checked": True, "sample_count": 6,
                    "row_count": 10, "source_numeric_consistency_rechecked": False},
    }), encoding="utf-8")
    (run / "reports/final_quality.json").write_text(json.dumps({
        "outcome": "passed", "summary": {"issue_count": 0}, "rule_version": "final-0.4.0",
    }), encoding="utf-8")
    (run / "reports/quality_report.json").write_text("{}", encoding="utf-8")
    batch = {"batch_id": "batch_x", "label": "demo", "content_version": 3,
             "source": {"kind": "matching_cable", "repo_id": "local", "revision": "local",
                        "license": "local", "robot": "dual arm", "fps": 20,
                        "cameras": ["observation.images.camera_0"],
                        "missing_cameras": [], "depth_source_cameras": [],
                        "missing_depth_source_cameras": [], "optional_fields": {},
                        "tactile": {"status": "not_provided"},
                        "state_field": "puppet/joint_position",
                        "action_field": "master/joint_position",
                        "timestamp_semantics": "ms", "source_root": r"C:\private\data"}}
    output = {"output_id": "output_x", "artifact_path": str(dataset),
              "validation": {"official_loader_passed": True, "source_numeric": True,
                             "source_numeric_scope": "format_write"}}
    spec = {"artifact_path": str(dataset), "snapshot_id": "snap", "content_version": 3,
            "episodes": [{"source_episode_index": 0, "output_episode_index": 0, "row_count": 10,
                          "annotation": {"task": "pick"}}]}
    return batch, output, spec, run


def test_card_is_self_verifying_and_portable(tmp_path):
    batch, output, spec, run = _fixture(tmp_path)
    card = build_card(batch, output, spec, run, dataset_id="dataset_demo", version="1")
    assert card["dataset_id"] == "dataset_demo"
    assert card["content"]["frame_count"] == 10
    assert card["content"]["tasks"] == ["pick"]
    assert card["quality"]["official_verify"]["output_loader_consistency_checked"] is True
    assert "source_root" not in card["source"]
    assert r"C:\private" not in json.dumps(card, ensure_ascii=False)
    assert verify_card(card) == (True, "ok")
    same = {**card, "created_at": "2000-01-01T00:00:00+00:00"}
    assert card_digest(same) == card["card_digest"]
    assert verify_card(same)[0] is True
    tampered = {**card, "label": "changed"}
    assert verify_card(tampered)[0] is False


def test_registry_register_list_show_and_conflict(tmp_path):
    batch, output, spec, run = _fixture(tmp_path)
    card = build_card(batch, output, spec, run, dataset_id="dataset_demo", version="1")
    card_path = tmp_path / "dataset_card.json"
    card_path.write_text(json.dumps(card), encoding="utf-8")

    first = register_card(card, root=tmp_path, card_path=card_path)
    assert first["status"] == "registered"
    assert register_card(card, root=tmp_path, card_path=card_path)["status"] == "unchanged"
    listing = list_datasets(tmp_path)
    assert listing["count"] == 1
    assert listing["datasets"][0]["versions"][0]["dataset_id"] == "dataset_demo"
    assert show("dataset_demo", "1", tmp_path)["versions"][0]["card"]["label"] == "demo"

    changed = {**card, "label": "other"}
    changed["card_digest"] = card_digest(changed)
    with pytest.raises(RegistryConflict):
        register_card(changed, root=tmp_path)


def test_registry_cli_verify_and_tamper(tmp_path):
    batch, output, spec, run = _fixture(tmp_path)
    card = build_card(batch, output, spec, run)
    path = tmp_path / "card.json"
    path.write_text(json.dumps(card), encoding="utf-8")
    assert main(["registry", "verify", "--card", str(path)]) == 0
    tampered = {**card, "label": "tampered"}
    path.write_text(json.dumps(tampered), encoding="utf-8")
    assert main(["registry", "verify", "--card", str(path)]) == 3
    assert verify_card_path(path)["valid"] is False
