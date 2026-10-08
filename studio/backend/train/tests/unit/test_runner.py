import json
import runpy
from pathlib import Path

from quictrain_runner import JobSpec, Runner
from quictrain_runner.runner import load_job_spec
from quictrain_runtime_lerobot import ActRuntime, Pi05Runtime
from quictrain_runtime_lerobot.adapters import parse_lerobot_metric_line


def test_runner_writes_reproducibility_bundle(tmp_path: Path):
    job = JobSpec(
        job_id="job_runner",
        attempt_id="att_runner",
        model_id="act",
        model_version_id="mv_act_test",
        recipe_id="fine_tune",
        adapter_version="0.1.0",
        dataset={
            "uri": "oss://fixture/data",
            "checksum": "sha256:data",
            "camera_keys": ["observation.images.left"],
            "action_dim": 7,
            "episodes": 4,
        },
        resolved_config={
            "training": {"steps": 100},
            "optimizer": {"learning_rate": 0.00005},
        },
        source={"upstream_ref": "abc123", "image_digest": "sha256:image"},
        output_dir=str(tmp_path),
    )
    summary = Runner(ActRuntime()).run(job)
    assert summary["metrics"]["train/loss"] == 0.421
    for required in [
        "final_model.safetensors",
        "resolved_config.json",
        "source.json",
        "environment.json",
        "summary.json",
        "artifact_manifest.json",
        "_SUCCESS",
    ]:
        assert (tmp_path / required).exists()


def test_runtime_commands_match_pinned_lerobot_cli(tmp_path: Path):
    job = JobSpec(
        job_id="job_command",
        attempt_id="att_command",
        model_id="pi05",
        model_version_id="mv_pi05_test",
        recipe_id="fine_tune",
        adapter_version="0.1.0",
        dataset={
            "uri": "oss://fixture/data",
            "repo_id": "quicrobot/kitchen-v17",
            "root": "/quictrain/data",
            "checksum": "sha256:data",
            "camera_keys": ["observation.images.left"],
            "action_dim": 7,
        },
        resolved_config={
            "training": {"steps": 100, "batch_size": 1, "seed": 42, "precision": "bf16"},
            "optimizer": {"learning_rate": 0.00005, "weight_decay": 0.01},
            "pretrained_path": "lerobot/pi05_base",
            "action_expert_only": True,
            "max_action_dim": 32,
        },
        source={"upstream_ref": "abc123", "image_digest": "sha256:image"},
        output_dir=str(tmp_path),
    )
    command = Pi05Runtime().command(job)
    assert command[0] == "lerobot-train"
    assert "--policy.type=pi05" in command
    assert "--policy.pretrained_path=lerobot/pi05_base" in command
    assert "--dataset.root=/quictrain/data" in command
    assert "--num_workers=0" in command
    assert "--log_freq=1" in command
    assert f"--output_dir=/tmp/quictrain/{job.job_id}/{job.attempt_id}/lerobot" in command


def test_lerobot_metric_line_is_converted_to_platform_metrics():
    parsed = parse_lerobot_metric_line(
        "INFO step:20 smpl:20 ep:1 epch:0.16 loss:0.421 grdn:1.250 lr:5.0e-05"
    )
    assert parsed == (
        20,
        {
            "train/epoch": 0.16,
            "train/loss": 0.421,
            "train/grad_norm": 1.25,
            "train/lr": 5.0e-05,
        },
    )


def test_act_runtime_is_offline_by_default_and_archives_regular_files(tmp_path: Path):
    job = JobSpec(
        job_id="job_act_cloud",
        attempt_id="att_act_cloud",
        model_id="act",
        model_version_id="mv_act_test",
        recipe_id="fine_tune",
        adapter_version="0.2.0",
        dataset={
            "uri": "oss://fixture/data",
            "repo_id": "quicrobot/pusht-smoke",
            "root": "/quictrain/input",
            "checksum": "sha256:data",
            "camera_keys": ["observation.image"],
            "action_dim": 2,
        },
        resolved_config={
            "training": {"steps": 5, "batch_size": 1, "num_workers": 0},
            "optimizer": {"learning_rate": 0.00005, "weight_decay": 0.01},
            "chunk_size": 100,
            "pretrained_backbone_weights": None,
        },
        source={"upstream_ref": "abc123", "image_digest": "sha256:image"},
        output_dir=str(tmp_path / "output"),
    )
    runtime = ActRuntime()
    command = runtime.command(job)
    assert "--policy.pretrained_backbone_weights=null" in command

    work_dir = tmp_path / "work"
    checkpoint = work_dir / "lerobot" / "checkpoints" / "000005"
    model_dir = checkpoint / "pretrained_model"
    state_dir = checkpoint / "training_state"
    model_dir.mkdir(parents=True)
    state_dir.mkdir()
    (model_dir / "model.safetensors").write_bytes(b"model")
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (state_dir / "optimizer.pt").write_bytes(b"optimizer")
    log_path = work_dir / "lerobot.log"
    log_path.write_text("step:5 loss:0.5\n", encoding="utf-8")

    result = {
        "mode": "lerobot",
        "lerobot_output": str(work_dir / "lerobot"),
        "log": str(log_path),
    }
    Path(job.output_dir).mkdir(parents=True)
    artifacts = runtime.collect_artifacts(job, result)

    archived_model = Path(job.output_dir) / "checkpoint" / "pretrained_model" / "model.safetensors"
    assert archived_model in artifacts
    assert archived_model.read_bytes() == b"model"
    assert not any(path.is_symlink() for path in Path(job.output_dir).rglob("*"))
    assert result["checkpoint"] == str(Path(job.output_dir) / "checkpoint")


def test_job_spec_can_be_loaded_from_dlc_environment(monkeypatch, tmp_path: Path):
    payload = {
        "job_id": "job_env",
        "attempt_id": "att_env",
        "model_id": "act",
        "model_version_id": "mv_act_test",
        "recipe_id": "fine_tune",
        "adapter_version": "0.1.0",
        "dataset": {
            "uri": "hf://datasets/lerobot/pusht",
            "checksum": "sha256:data",
            "camera_keys": ["observation.images.top"],
            "action_dim": 2,
        },
        "resolved_config": {
            "training": {"steps": 2},
            "optimizer": {"learning_rate": 0.00005},
        },
        "source": {"image_digest": "sha256:image"},
        "output_dir": str(tmp_path),
        "output_uri": "oss://bucket/runs/job_env/att_env",
    }
    monkeypatch.setenv("QUICTRAIN_JOB_SPEC_JSON", json.dumps(payload))
    loaded = load_job_spec("env://QUICTRAIN_JOB_SPEC_JSON")
    Runner(ActRuntime()).run(loaded)
    manifest = json.loads((tmp_path / "artifact_manifest.json").read_text())
    assert all(
        item["uri"].startswith("oss://bucket/runs/job_env/att_env/")
        for item in manifest["artifacts"]
    )


def test_runner_mirrors_only_small_control_artifacts(tmp_path: Path):
    output = tmp_path / "cpfs" / "run"
    control = tmp_path / "oss-control" / "run"
    job = JobSpec(
        job_id="job_cpfs",
        attempt_id="att_cpfs",
        model_id="act",
        model_version_id="mv_act_test",
        recipe_id="fine_tune",
        adapter_version="0.1.0",
        dataset={
            "uri": "bmcpfs://fixture/data",
            "checksum": "sha256:data",
            "camera_keys": ["observation.image"],
            "action_dim": 2,
        },
        resolved_config={
            "training": {"steps": 1},
            "optimizer": {"learning_rate": 0.00005},
        },
        source={"upstream_ref": "abc123"},
        output_dir=str(output),
        output_uri="bmcpfs://fixture/quictrain/runs/job_cpfs/att_cpfs",
        control_output_dir=str(control),
        control_output_uri="oss://fixture/control/job_cpfs/att_cpfs",
    )

    Runner(ActRuntime()).run(job)

    assert (output / "final_model.safetensors").exists()
    assert not (control / "final_model.safetensors").exists()
    for mirrored in [
        "resolved_config.json",
        "source.json",
        "environment.json",
        "summary.json",
        "artifact_manifest.json",
        "_SUCCESS",
    ]:
        assert (control / mirrored).exists()


def test_scheduler_bootstrap_supports_the_published_runtime():
    from quictrain_scheduler.engine import RUNTIME_BOOTSTRAP_COMMAND

    assert "/tmp/quictrain-job-spec.json" in RUNTIME_BOOTSTRAP_COMMAND
    assert "env://" not in RUNTIME_BOOTSTRAP_COMMAND
    assert '"$QUICTRAIN_RUNTIME_PLUGIN"' in RUNTIME_BOOTSTRAP_COMMAND
    assert "QUICTRAIN_CONTROL_OUTPUT_DIR" in RUNTIME_BOOTSTRAP_COMMAND
    assert "2097152" in RUNTIME_BOOTSTRAP_COMMAND


def test_canary_omits_empty_output_uri_for_published_runtime(monkeypatch):
    monkeypatch.delenv("QUICTRAIN_ARTIFACT_ROOT", raising=False)
    script = runpy.run_path(str(Path(__file__).parents[2] / "scripts" / "submit_dlc_canary.py"))
    build_launch_spec = script["build_launch_spec"]
    spec = build_launch_spec("act", steps=1, max_minutes=5)
    payload = json.loads(spec.environment["QUICTRAIN_JOB_SPEC_JSON"])

    assert "output_uri" not in payload


def test_canary_mounts_offline_dataset_and_artifact_roots(monkeypatch):
    monkeypatch.setenv(
        "QUICTRAIN_CANARY_DATASET_URI",
        "oss://bucket/quictrain/datasets/lerobot",
    )
    monkeypatch.setenv(
        "QUICTRAIN_CANARY_DATASET_ROOT",
        "/quictrain/input/quictrain-pusht-smoke-v1",
    )
    monkeypatch.setenv("QUICTRAIN_ARTIFACT_ROOT", "oss://bucket/quictrain/runs")
    script = runpy.run_path(str(Path(__file__).parents[2] / "scripts" / "submit_dlc_canary.py"))
    spec = script["build_launch_spec"]("act", steps=5, max_minutes=15)
    payload = json.loads(spec.environment["QUICTRAIN_JOB_SPEC_JSON"])

    assert payload["dataset"]["root"] == "/quictrain/input/quictrain-pusht-smoke-v1"
    assert "revision" not in payload["dataset"]
    assert payload["output_dir"].startswith("/quictrain/output/canary_act_")
    assert spec.environment["HF_HUB_OFFLINE"] == "1"
    assert [mount["target"] for mount in spec.mounts] == [
        "/quictrain/input",
        "/quictrain/output",
    ]


def test_cpfs_bootstrap_is_pinned_and_mounts_only_source_and_workspace(monkeypatch):
    monkeypatch.setenv("QUICTRAIN_CPFS_ROOT_URI", "bmcpfs://filesystem")
    monkeypatch.setenv("QUICTRAIN_CPFS_BOOTSTRAP_PUSHT_URI", "oss://fixture/pusht")
    script = runpy.run_path(
        str(Path(__file__).parents[2] / "scripts" / "bootstrap_cpfs_workspace.py")
    )
    spec = script["build_launch_spec"](90)

    assert spec.idempotency_key == "admin_cpfs_bootstrap_v11b:attempt:1"
    assert [mount["target"] for mount in spec.mounts] == [
        "/mnt/cpfs",
        "/quictrain/source/pusht",
    ]
    assert spec.mounts[0]["source_uri"] == "bmcpfs://filesystem/"
    assert "7de663972b7817d2c4cf2d84c821153dfea772e9" in spec.command[-1]
    assert "policy_preprocessor.json" in spec.command[-1]
