from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import signal
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class JobSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: str = "v1alpha1"
    job_id: str
    attempt_id: str
    model_id: str
    model_version_id: str
    recipe_id: str
    adapter_version: str
    dataset: dict[str, Any]
    resolved_config: dict[str, Any]
    source: dict[str, Any]
    output_dir: str
    output_uri: str | None = None
    control_output_dir: str | None = None
    control_output_uri: str | None = None
    mlflow: dict[str, Any] = Field(default_factory=dict)


class RuntimePlugin(Protocol):
    def preflight(self, job: JobSpec) -> dict[str, Any]: ...

    def train(self, job: JobSpec, reporter: Reporter) -> dict[str, Any]: ...

    def collect_artifacts(self, job: JobSpec, result: dict[str, Any]) -> list[Path]: ...

    def cancel(self) -> None: ...


class Reporter:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.metrics: dict[str, float] = {}

    def stage(self, name: str, message: str) -> None:
        event = {"type": "stage", "stage": name, "message": message, "at": now()}
        self.events.append(event)
        self._emit(event)

    def metric(self, name: str, value: float, step: int) -> None:
        self.metrics[name] = value
        event = {"type": "metric", "name": name, "value": value, "step": step, "at": now()}
        self.events.append(event)
        self._emit(event)

    @staticmethod
    def _emit(event: dict[str, Any]) -> None:
        print(f"QUICTRAIN_EVENT {json.dumps(event, separators=(',', ':'))}", flush=True)


class RunnerError(RuntimeError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


class Runner:
    CONTROL_MIRROR_EXTENSIONS = {
        ".csv",
        ".json",
        ".log",
        ".md",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
    CONTROL_MIRROR_MAX_BYTES = 2 * 1024 * 1024

    def __init__(self, plugin: RuntimePlugin) -> None:
        self.plugin = plugin
        self.cancel_requested = False

    def _handle_sigterm(self, _signum: int, _frame: object) -> None:
        self.cancel_requested = True
        cancel = getattr(self.plugin, "cancel", None)
        if cancel is not None:
            cancel()

    def run(self, job: JobSpec) -> dict[str, Any]:
        if job.protocol_version != "v1alpha1":
            raise RunnerError("PROTOCOL", f"Unsupported protocol {job.protocol_version}")

        output = Path(job.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        reporter = Reporter()
        previous_handler = signal.signal(signal.SIGTERM, self._handle_sigterm)
        started_at = now()
        try:
            reporter.stage("PREFLIGHT", "Validating dataset, mounts and runtime")
            preflight = self.plugin.preflight(job)
            if not preflight.get("valid", False):
                raise RunnerError("DATA_INVALID", preflight.get("message", "Preflight failed"))

            reporter.stage("TRAIN", "Starting model runtime")
            result = self.plugin.train(job, reporter)
            if self.cancel_requested:
                raise RunnerError("CANCELLED", "SIGTERM received")

            reporter.stage("ARTIFACT", "Collecting and verifying artifacts")
            artifacts = self.plugin.collect_artifacts(job, result)
            self._write_json(output / "resolved_config.json", job.resolved_config)
            self._write_json(output / "source.json", job.source)
            self._write_json(
                output / "environment.json",
                {
                    "python": platform.python_version(),
                    "platform": platform.platform(),
                    "hostname": platform.node(),
                },
            )
            summary = {
                "job_id": job.job_id,
                "attempt_id": job.attempt_id,
                "started_at": started_at,
                "finished_at": now(),
                "metrics": reporter.metrics,
                "events": reporter.events,
                "result": result,
            }
            self._write_json(output / "summary.json", summary)
            required = artifacts + [
                output / "resolved_config.json",
                output / "source.json",
                output / "environment.json",
                output / "summary.json",
            ]
            manifest = self._manifest(required, output, job.output_uri)
            self._write_json(output / "artifact_manifest.json", manifest)
            (output / "_SUCCESS").write_text(now(), encoding="utf-8")
            if job.control_output_dir:
                control = Path(job.control_output_dir)
                control.mkdir(parents=True, exist_ok=True)
                for path in required:
                    if (
                        path.suffix.lower() in self.CONTROL_MIRROR_EXTENSIONS
                        and path.stat().st_size <= self.CONTROL_MIRROR_MAX_BYTES
                    ):
                        relative = path.relative_to(output)
                        destination = control / relative
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, destination)
                self._write_json(control / "artifact_manifest.json", manifest)
                (control / "_SUCCESS").write_text(now(), encoding="utf-8")
            return summary
        except RunnerError:
            (output / "_SUCCESS").unlink(missing_ok=True)
            raise
        finally:
            signal.signal(signal.SIGTERM, previous_handler)

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _manifest(paths: list[Path], output_dir: Path, output_uri: str | None) -> dict[str, Any]:
        items = []
        for path in paths:
            if not path.exists():
                raise RunnerError("ARTIFACT_UPLOAD", f"Required artifact missing: {path.name}")
            digest = Runner._sha256(path)
            relative = path.relative_to(output_dir)
            uri = (
                f"{output_uri.rstrip('/')}/{relative.as_posix()}"
                if output_uri
                else f"file://{path.resolve()}"
            )
            items.append(
                {
                    "name": path.name,
                    "size_bytes": path.stat().st_size,
                    "sha256": digest,
                    "uri": uri,
                }
            )
        return {"protocol_version": "v1alpha1", "artifacts": items, "created_at": now()}

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


def load_job_spec(path: str | os.PathLike[str]) -> JobSpec:
    value = str(path)
    if value.startswith("env://"):
        variable = value.removeprefix("env://")
        payload = os.environ.get(variable)
        if payload is None:
            raise RunnerError("PROTOCOL", f"Missing job spec environment variable: {variable}")
        return JobSpec.model_validate_json(payload)
    return JobSpec.model_validate_json(Path(path).read_text(encoding="utf-8"))
