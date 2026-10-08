from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol


class TrackingClient(Protocol):
    def create_run(self, experiment_id: str, tags: dict[str, str]) -> str: ...

    def log_metrics(self, run_id: str, metrics: dict[str, float], step: int) -> None: ...

    def set_terminated(self, run_id: str, status: str) -> None: ...


class TrackingAdapter:
    """MLflow boundary with a local durable buffer for degraded operation."""

    def __init__(
        self, client: TrackingClient | None, buffer_dir: Path, strict: bool = False
    ) -> None:
        self.client = client
        self.buffer_dir = buffer_dir
        self.strict = strict
        self.buffer_dir.mkdir(parents=True, exist_ok=True)

    def create_run(self, experiment_id: str, tags: dict[str, str]) -> str:
        if self.client is not None:
            return self.client.create_run(experiment_id, tags)
        if self.strict:
            raise RuntimeError("TRACKING_UNAVAILABLE")
        run_id = f"local-{tags['quictrain.job_id']}"
        self._append(run_id, {"type": "run.created", "tags": tags})
        return run_id

    def log_metrics(self, run_id: str, metrics: dict[str, float], step: int) -> None:
        if self.client is not None:
            self.client.log_metrics(run_id, metrics, step)
        else:
            self._append(run_id, {"type": "metric", "step": step, "metrics": metrics})

    def finish(self, run_id: str, status: str) -> None:
        if self.client is not None:
            self.client.set_terminated(run_id, status)
        else:
            self._append(run_id, {"type": "run.terminated", "status": status})

    def _append(self, run_id: str, item: dict[str, Any]) -> None:
        path = self.buffer_dir / f"{run_id}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
