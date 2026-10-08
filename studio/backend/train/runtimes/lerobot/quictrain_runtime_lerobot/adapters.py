from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from quictrain_runner import JobSpec
from quictrain_runner.runner import Reporter


def parse_lerobot_metric_line(line: str) -> tuple[int, dict[str, float]] | None:
    """Convert LeRobot's MetricsTracker line into QuicTrain metric events."""

    step_match = re.search(r"(?:^|\s)step[:=]\s*(\d+)", line)
    if step_match is None:
        return None
    aliases = {
        "loss": "train/loss",
        "lr": "train/lr",
        "grdn": "train/grad_norm",
        "updt_s": "train/update_seconds",
        "data_s": "train/dataloading_seconds",
        "epch": "train/epoch",
    }
    metrics: dict[str, float] = {}
    for key, value in re.findall(
        r"(?:^|\s)(loss|lr|grdn|updt_s|data_s|epch)[:=]([0-9.eE+-]+)", line
    ):
        metrics[aliases[key]] = float(value)
    return (int(step_match.group(1)), metrics) if metrics else None


class LeRobotRuntime:
    policy_type = "unknown"

    def __init__(self) -> None:
        self.process: subprocess.Popen[str] | None = None

    def preflight(self, job: JobSpec) -> dict[str, Any]:
        dataset = job.dataset
        required = ["uri", "checksum", "camera_keys", "action_dim"]
        missing = [field for field in required if not dataset.get(field)]
        if missing:
            return {"valid": False, "message": f"Missing dataset fields: {missing}"}
        return {"valid": True, "sampled_episodes": min(dataset.get("episodes", 1), 8)}

    @staticmethod
    def work_dir(job: JobSpec) -> Path:
        root = Path(os.getenv("QUICTRAIN_WORK_ROOT", "/tmp/quictrain"))  # nosec B108 - isolated job workspace default
        return root / job.job_id / job.attempt_id

    def command(self, job: JobSpec) -> list[str]:
        training = job.resolved_config.get("training", {})
        optimizer = job.resolved_config.get("optimizer", {})
        steps = int(training.get("steps", 30000))
        log_freq = int(training.get("log_freq", max(1, min(50, steps // 100))))
        dataset_repo_id = job.dataset.get("repo_id", job.dataset["uri"])
        lerobot_output = self.work_dir(job) / "lerobot"
        command = [
            "lerobot-train",
            f"--policy.type={self.policy_type}",
            f"--dataset.repo_id={dataset_repo_id}",
            f"--output_dir={lerobot_output}",
            f"--job_name={job.job_id}",
            f"--steps={steps}",
            f"--log_freq={log_freq}",
            f"--batch_size={training.get('batch_size', 4)}",
            f"--seed={training.get('seed', 42)}",
            f"--num_workers={training.get('num_workers', 0)}",
            "--eval_freq=0",
            "--wandb.enable=false",
            "--policy.device=cuda",
            "--policy.push_to_hub=false",
            f"--policy.optimizer_lr={optimizer.get('learning_rate', 5e-5)}",
            f"--policy.optimizer_weight_decay={optimizer.get('weight_decay', 0.01)}",
        ]
        if root := job.dataset.get("root"):
            command.append(f"--dataset.root={root}")
        if revision := job.dataset.get("revision"):
            command.append(f"--dataset.revision={revision}")
        if self.policy_type == "act":
            chunk_size = job.resolved_config.get("chunk_size", 100)
            pretrained_backbone_weights = job.resolved_config.get("pretrained_backbone_weights")
            command.extend(
                [
                    f"--policy.chunk_size={chunk_size}",
                    f"--policy.n_action_steps={chunk_size}",
                    "--policy.pretrained_backbone_weights="
                    + (pretrained_backbone_weights or "null"),
                ]
            )
        if self.policy_type == "pi05":
            pretrained_path = job.resolved_config.get("pretrained_path", "lerobot/pi05_base")
            train_expert_only = str(job.resolved_config.get("action_expert_only", True)).lower()
            gradient_checkpointing = str(training.get("gradient_checkpointing", True)).lower()
            dtype = "bfloat16" if training.get("precision", "bf16") == "bf16" else "float32"
            command.extend(
                [
                    f"--policy.pretrained_path={pretrained_path}",
                    f"--policy.train_expert_only={train_expert_only}",
                    f"--policy.max_action_dim={job.resolved_config.get('max_action_dim', 32)}",
                    f"--policy.gradient_checkpointing={gradient_checkpointing}",
                    f"--policy.dtype={dtype}",
                ]
            )
        return command

    def train(self, job: JobSpec, reporter: Reporter) -> dict[str, Any]:
        """Runtime contract implementation.

        GPU images set QUICTRAIN_EXECUTE_TRAINING=1 and execute `command(job)`.
        Contract/local smoke intentionally emits deterministic metrics without
        importing LeRobot, keeping CPU CI lightweight.
        """
        steps = int(job.resolved_config["training"]["steps"])
        if os.getenv("QUICTRAIN_EXECUTE_TRAINING") == "1":
            command = self.command(job)
            work_dir = self.work_dir(job)
            work_dir.mkdir(parents=True, exist_ok=True)
            log_path = work_dir / "lerobot.log"
            with log_path.open("w", encoding="utf-8") as log:
                self.process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                assert self.process.stdout is not None
                for line in self.process.stdout:
                    log.write(line)
                    log.flush()
                    print(line, end="", flush=True)
                    parsed = parse_lerobot_metric_line(line)
                    if parsed:
                        step, metrics = parsed
                        for name, value in metrics.items():
                            reporter.metric(name, value, step)
                return_code = self.process.wait()
                self.process = None
            if return_code != 0:
                raise RuntimeError(f"LeRobot exited with status {return_code}")
            return {
                "policy_type": self.policy_type,
                "command": command,
                "steps": steps,
                "mode": "lerobot",
                "log": str(log_path),
                "lerobot_output": str(work_dir / "lerobot"),
            }

        reporter.metric("train/loss", 0.842, min(100, steps))
        reporter.metric("train/loss", 0.421, min(500, steps))
        reporter.metric("train/lr", job.resolved_config["optimizer"]["learning_rate"], 500)
        return {
            "policy_type": self.policy_type,
            "command": self.command(job),
            "steps": steps,
            "mode": "contract-smoke",
        }

    def collect_artifacts(self, job: JobSpec, result: dict[str, Any]) -> list[Path]:
        output = Path(job.output_dir)
        if result.get("mode") == "lerobot":
            lerobot_output = Path(result["lerobot_output"])
            checkpoints_root = lerobot_output / "checkpoints"
            checkpoints = sorted(
                (
                    path
                    for path in checkpoints_root.iterdir()
                    if path.is_dir() and path.name.isdigit()
                ),
                key=lambda path: int(path.name),
            )
            if not checkpoints:
                raise RuntimeError("LeRobot completed without a safetensors checkpoint")
            source_checkpoint = checkpoints[-1]
            if not list(source_checkpoint.rglob("*.safetensors")):
                raise RuntimeError("LeRobot completed without a safetensors checkpoint")

            final_checkpoint = output / "checkpoint"
            shutil.copytree(
                source_checkpoint,
                final_checkpoint,
                symlinks=False,
                dirs_exist_ok=True,
            )
            final_log = output / "lerobot.log"
            shutil.copy2(Path(result["log"]), final_log)
            result["checkpoint"] = str(final_checkpoint)
            result["log"] = str(final_log)
            return sorted(path for path in final_checkpoint.rglob("*") if path.is_file()) + [
                final_log
            ]
        final_model = output / "final_model.safetensors"
        final_model.write_bytes(b"quictrain-contract-smoke-model")
        runtime_result = output / "runtime_result.json"
        runtime_result.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return [final_model, runtime_result]

    def cancel(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()


class ActRuntime(LeRobotRuntime):
    policy_type = "act"


class Pi05Runtime(LeRobotRuntime):
    policy_type = "pi05"
