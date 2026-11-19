"""Frozen processing plans: validation, annotation coverage, and dry-run."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from robodata.cli import main
from robodata.process_plan import (EXIT_INVALID, PlanError, build_annotations, load_plan,
                                   normalize_plan, run_process)


def _plan(tmp_path: Path, **annotations) -> dict:
    source = tmp_path / "input"
    source.mkdir(exist_ok=True)
    (source / "68").mkdir(exist_ok=True)
    (source / "68/0.hdf5").write_bytes(b"frame")
    return {
        "schema_version": 1,
        "input": {"path": str(source), "label": "synthetic plan", "source": "auto"},
        "annotations": annotations or {"task_template": "episode {source_episode_id}",
                                       "default_outcome": "success"},
    }


def test_load_plan_accepts_json_and_normalizes(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(_plan(tmp_path)), encoding="utf-8")
    plan = load_plan(path)
    assert plan["input"]["label"] == "synthetic plan"
    assert plan["annotations"]["mode"] == "template"
    assert plan["review"]["approve"] is True
    assert plan["delivery"]["package"] is True


def test_plan_rejects_missing_input_fields(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"schema_version": 1, "input": {"path": str(tmp_path)}}), encoding="utf-8")
    with pytest.raises(PlanError, match="label"):
        load_plan(path)


def test_template_annotations_cover_kept_episodes_and_overrides():
    batch = {"source": {"episode_mapping": {"0": 68, "1": 70}},
             "episodes": [{"episode_index": 0, "row_count": 10},
                          {"episode_index": 1, "row_count": 20}]}
    plan = normalize_plan({
        "schema_version": 1,
        "input": {"path": ".", "label": "x"},
        "annotations": {"task_template": "episode {source_episode_id} / {row_count}",
                        "default_outcome": "success", "tags": ["telecom"],
                        "overrides": [{"episode_index": 1, "outcome": "failure", "notes": "slip"}]},
    })
    annotations = build_annotations(plan, batch, batch["episodes"])
    assert annotations[0] == {"task": "episode 68 / 10", "outcome": "success",
                              "tags": ["telecom"], "notes": "", "episode_index": 0}
    assert annotations[1]["task"] == "episode 70 / 20"
    assert annotations[1]["outcome"] == "failure"
    assert annotations[1]["notes"] == "slip"


def test_list_annotations_apply_in_order():
    batch = {"source": {"episode_mapping": {}},
             "episodes": [{"episode_index": 0, "row_count": 1},
                          {"episode_index": 1, "row_count": 1}]}
    plan = normalize_plan({
        "schema_version": 1, "input": {"path": ".", "label": "x"},
        "annotations": [{"task": "first", "outcome": "success"},
                        {"task": "second", "outcome": "uncertain"}],
    })
    annotations = build_annotations(plan, batch, batch["episodes"])
    assert annotations[0]["task"] == "first"
    assert annotations[1]["outcome"] == "uncertain"


def test_list_annotations_must_cover_every_kept_episode():
    batch = {"source": {"episode_mapping": {}},
             "episodes": [{"episode_index": 0, "row_count": 1},
                          {"episode_index": 1, "row_count": 1}]}
    plan = normalize_plan({
        "schema_version": 1, "input": {"path": ".", "label": "x"},
        "annotations": [{"task": "only one", "outcome": "success"}],
    })
    with pytest.raises(PlanError, match="keep Episode"):
        build_annotations(plan, batch, batch["episodes"])


def test_process_dry_run_validates_matching_cable(tmp_path):
    plan = normalize_plan(_plan(tmp_path))
    result = run_process(plan, dry_run=True)
    assert result["status"] == "validated"
    assert result["exit_code"] == 0
    assert "matching_cable" in result["message"]


def test_process_cli_rejects_invalid_plan(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{}", encoding="utf-8")
    assert main(["process", "--plan", str(path), "--dry-run"]) == EXIT_INVALID
