"""Read persisted runs for the UI without depending on source metadata."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .config import PROJECT_ROOT

RUN_STATUS = {
    "queued": "等待启动", "running": "运行中", "completed": "运行完成",
    "completed_with_errors": "运行结束，有节点失败", "failed": "运行失败",
    "interrupted": "运行已中断",
}
NODE_STATUS = {
    "pending": "等待执行", "running": "执行中", "completed": "执行完成",
    "failed": "执行失败", "timed_out": "执行超时", "skipped": "未执行",
    "interrupted": "执行中断",
}
ACTIVE_STATUSES = {"queued", "running"}


def runs_root() -> Path:
    return Path(os.environ.get("ROBODATA_RUNS_ROOT", str(PROJECT_ROOT / "work" / "runs"))).expanduser().resolve()


def start_quality(data_root: Path, params: dict, *, source_kind: str, execution_demo: str,
                  timeouts: dict | None = None) -> str:
    from .runtime import start_run
    from .config import EPISODES
    from .dataset import load_metadata

    try:
        indices = load_metadata(data_root).get("episode_indices", list(EPISODES))
    except Exception:
        indices = list(EPISODES)  # The runtime records unreadable input as a real failed run.

    return start_run({
        "kind": "quality", "data_root": str(data_root), "params": params,
        "episode_indices": indices,
        "check_video": True, "source_kind": source_kind,
        "timeouts": {"default": 60, "video_decode": 120, **(timeouts or {})}, "execution_demo": execution_demo,
    }, runs_root=runs_root())


def available_runs() -> list[dict]:
    from .runtime import list_runs

    return list_runs(runs_root=runs_root())


def load_run(run_id: str) -> dict:
    from .runtime import get_run

    return get_run(run_id, runs_root=runs_root())


def load_events(run_id: str) -> list[dict]:
    from .runtime import read_events

    return read_events(run_id, runs_root=runs_root())


def artifact_path(state: dict, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else runs_root() / state["run_id"] / path


def load_report(state: dict) -> dict | None:
    value = state.get("report_path")
    if not value:
        return None
    result = json.loads(artifact_path(state, value).read_text(encoding="utf-8"))
    if not isinstance(result, dict) or not isinstance(result.get("summary"), dict):
        raise ValueError("运行的报告文件结构不完整，请查看该运行的日志与产物")
    return result


def filter_events(events: list[dict], episode=None, node=None, level=None) -> list[dict]:
    return [event for event in events
            if (episode is None or event.get("episode_index") == episode)
            and (node is None or event.get("node_id") == node)
            and (level is None or str(event.get("level", "INFO")).upper() == level)]


def events_jsonl(events: list[dict]) -> str:
    return "".join(json.dumps(event, ensure_ascii=False, default=str, allow_nan=False) + "\n" for event in events)


def process_logs(state: dict) -> list[Path]:
    run_id = state["run_id"]
    if Path(run_id).name != run_id or "/" in run_id or "\\" in run_id:
        raise ValueError("运行编号无效")
    return sorted((runs_root() / run_id).glob("*.log"))
