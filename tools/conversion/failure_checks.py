"""Real subprocess failure checks on labelled copies; never edits the source."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import h5py
import numpy as np

from worker import SOURCE_SHA256, sha256, write_json


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source, output = args.input.resolve(), args.output.resolve()
    assert sha256(source) == SOURCE_SHA256, "The source must be the fixed original"
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "TEST_COPY_NOTICE.json", {"purpose": "故障注入测试副本；不是真实源数据异常，禁止替代原始样本", "source_sha256": SOURCE_SHA256})
    env = os.environ.copy()
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1"})
    results = []

    def run_case(name, stage, input_path, expected_code, expected_demo=None, expected_field=None, expected_row=None, *, test_mode=True, target=None):
        artifact_dir = output / name / "artifacts"
        target = target or output / name / "dataset"
        command = [sys.executable, str(Path(__file__).with_name("worker.py")), "--stage", stage, "--input", str(input_path), "--output", str(target), "--artifacts", str(artifact_dir)]
        if test_mode:
            command += ["--test-mode", name]
        result = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "stdout.log").write_text(result.stdout, encoding="utf-8")
        (artifact_dir / "stderr.log").write_text(result.stderr, encoding="utf-8")
        assert result.returncode != 0, (name, "Unexpected success")
        diagnostic = json.loads((artifact_dir / "diagnostic.json").read_text(encoding="utf-8"))
        stage_result = json.loads((artifact_dir / "steps" / f"{stage}.json").read_text(encoding="utf-8"))
        assert stage_result["status"] == "failed", name
        assert diagnostic["code"] == expected_code, (name, diagnostic)
        assert diagnostic["stage"] == stage, name
        assert diagnostic["demo"] == expected_demo, (name, diagnostic)
        assert diagnostic["field"] == expected_field, (name, diagnostic)
        assert diagnostic["row"] == expected_row, (name, diagnostic)
        assert diagnostic["traceback"] and diagnostic["message_zh"], name
        assert not (target / "source_manifest.json").exists(), name
        results.append({"case": name, "status": "passed", "failed_stage": stage, "code": expected_code, "demo": expected_demo, "field": expected_field, "row": expected_row, "diagnostic_file": str(artifact_dir / "diagnostic.json")})

    missing = output / "missing_field.hdf5"
    shutil.copyfile(source, missing)
    with h5py.File(missing, "r+") as handle:
        del handle["data/demo_1/obs/robot0_eef_pos"]
    run_case("missing_field", "hdf5_validation", missing, "hdf5_missing_field", "demo_1", "/data/demo_1/obs/robot0_eef_pos")
    run_case("unlabelled_modified_copy", "file_validation", missing, "source_hash_mismatch", test_mode=False)

    nan_copy = output / "nan.hdf5"
    shutil.copyfile(source, nan_copy)
    with h5py.File(nan_copy, "r+") as handle:
        handle["data/demo_0/actions"][12, 3] = np.nan
    run_case("nan", "hdf5_validation", nan_copy, "hdf5_nonfinite", "demo_0", "/data/demo_0/actions", 12)

    length_copy = output / "length.hdf5"
    shutil.copyfile(source, length_copy)
    with h5py.File(length_copy, "r+") as handle:
        values = handle["data/demo_2/actions"][:-1]
        del handle["data/demo_2/actions"]
        handle["data/demo_2"].create_dataset("actions", data=values)
    run_case("length", "hdf5_validation", length_copy, "hdf5_length_mismatch", "demo_2", "/data/demo_2@num_samples")

    shape_copy = output / "dimension.hdf5"
    shutil.copyfile(source, shape_copy)
    with h5py.File(shape_copy, "r+") as handle:
        values = handle["data/demo_0/actions"][:, :6]
        del handle["data/demo_0/actions"]
        handle["data/demo_0"].create_dataset("actions", data=values)
    run_case("dimension", "field_mapping", shape_copy, "mapping_dimension_mismatch", "demo_0", "/data/demo_0/actions")

    truncated = output / "truncated.hdf5"
    with source.open("rb") as original:
        truncated.write_bytes(original.read(2048))
    run_case("truncated", "hdf5_validation", truncated, "hdf5_unreadable")

    blocked_parent = output / "blocked_parent"
    blocked_parent.write_text("This is intentionally a file, not an output directory.", encoding="utf-8")
    run_case("write_failure", "format_write", source, "stage_execution_failed", test_mode=False, target=blocked_parent / "dataset")

    assert sha256(source) == SOURCE_SHA256, "Original source was modified"
    summary = {"status": "passed", "cases": results, "case_count": len(results), "source_unchanged": True, "source_sha256": SOURCE_SHA256}
    write_json(output / "failure_checks.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
