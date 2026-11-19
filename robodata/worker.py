"""Single-node worker for the durable local job queue.

The worker never runs conversion logic in-process. It starts the frozen plan
through the normal ``robodata process`` entry point, records the child PID, and
renews a lease while waiting. Cancellation kills the child tree; a crashed
worker's job is adopted by the next worker instead of being started twice.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .config import PROJECT_ROOT
from .jobs import DEFAULT_LEASE_SECONDS, JobStore, append_event, kill_job_process

POLL_SECONDS = 2.0
HEARTBEAT_SECONDS = 10.0


def _env() -> dict:
    return {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1", "HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1"}


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _tail(path: Path, limit: int = 2000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


def _spawn(job_dir: Path, row) -> subprocess.Popen:
    result_path = job_dir / "result.json"
    if result_path.exists():
        result_path.unlink()
    command = [sys.executable, "-m", "robodata", "process", "--plan",
               str(job_dir / "plan.json"), "--result-json", str(result_path)]
    if row["business_root"]:
        command.extend(["--business-root", row["business_root"]])
    if row["runs_root"]:
        command.extend(["--runs-root", row["runs_root"]])
    stdout = (job_dir / "stdout.log").open("ab")
    stderr = (job_dir / "stderr.log").open("ab")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {
        "start_new_session": True}
    try:
        process = subprocess.Popen(command, cwd=PROJECT_ROOT, env=_env(),
                                   stdin=subprocess.DEVNULL, stdout=stdout,
                                   stderr=stderr, close_fds=True, **options)
    finally:
        stdout.close()
        stderr.close()
    return process


def run_once(store: JobStore | None = None, *, owner: str | None = None,
             lease_seconds: float = DEFAULT_LEASE_SECONDS,
             poll_seconds: float = POLL_SECONDS) -> dict | None:
    from .runtime import _alive, _identity

    store = store or JobStore()
    owner = owner or f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    job = store.claim(owner, lease_seconds)
    if job is None:
        return None
    job_id = job["id"]
    directory = store.job_dir(job_id)
    row = store._row(job_id)
    process: subprocess.Popen | None = None
    adopted = bool(job.get("adopt"))
    try:
        if not adopted:
            process = _spawn(directory, row)
            store.set_pid(job_id, owner, process.pid, _identity(process.pid))
            append_event(directory, "child_started", "已启动处理子进程",
                         pid=process.pid)
        else:
            append_event(directory, "child_adopted", "已接管原有处理子进程",
                         pid=row["pid"])
        last_heartbeat = 0.0
        canceled = False
        while True:
            current = store._row(job_id)
            if current is None:
                return job
            if current["cancel_requested"] and not canceled:
                canceled = True
                append_event(directory, "canceling", "正在终止处理子进程")
                kill_job_process(current)
            now = time.monotonic()
            if now - last_heartbeat >= min(HEARTBEAT_SECONDS, max(1.0, lease_seconds / 3)):
                last_heartbeat = now
                cancel_flag = store.heartbeat(job_id, owner, lease_seconds)
                if cancel_flag is None:
                    append_event(directory, "lease_lost", "租约已被其他 worker 接管")
                    return job
                if cancel_flag and not canceled:
                    canceled = True
                    append_event(directory, "canceling", "正在终止处理子进程")
                    kill_job_process(store._row(job_id))
            if adopted:
                current = store._row(job_id)
                if current is None or not current["pid"] or not _alive(
                        current["pid"], current["pid_identity"]):
                    break
            elif process is not None:
                if process.poll() is not None:
                    break
            time.sleep(max(0.2, float(poll_seconds)))
        result = _read_json(directory / "result.json")
        code = None if process is None else process.returncode
        if adopted and result is not None:
            code = int(result.get("exit_code", 0))
        if canceled:
            store.finish(job_id, owner, "canceled", result=result,
                         error="已按请求取消", exit_code=code)
            return store.get(job_id)
        if result is None:
            error = "处理进程没有写出 result.json；查看 stderr.log"
            tail = _tail(directory / "stderr.log")
            if tail:
                error += "\n" + tail[-800:]
            store.finish(job_id, owner, "failed", error=error, exit_code=code)
            return store.get(job_id)
        exit_code = int(result.get("exit_code", code if code is not None else 5))
        if exit_code == 0:
            store.finish(job_id, owner, "succeeded", result=result, exit_code=exit_code)
        else:
            message = result.get("message") or f"计划执行失败（退出码 {exit_code}）"
            store.finish(job_id, owner, "failed", result=result, error=message,
                         exit_code=exit_code)
        return store.get(job_id)
    except Exception as exc:  # noqa: BLE001 - the job must reach a terminal state
        append_event(directory, "worker_error", f"worker 异常：{exc}",
                     exception_type=type(exc).__name__)
        store.finish(job_id, owner, "failed", error=f"worker 异常：{exc}")
        return store.get(job_id)
    finally:
        if process is not None and process.poll() is None:
            kill_job_process(store._row(job_id))


def run_loop(store: JobStore | None = None, *, once: bool = False,
             poll_seconds: float = 2.0, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> int:
    store = store or JobStore()
    while True:
        job = run_once(store, lease_seconds=lease_seconds, poll_seconds=poll_seconds)
        if once:
            return 0
        if job is None:
            time.sleep(max(0.5, float(poll_seconds)))


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="RoboData 本地任务 worker")
    parser.add_argument("--business-root", type=Path, default=None)
    parser.add_argument("--once", action="store_true", help="最多执行一个任务后退出")
    parser.add_argument("--poll", type=float, default=2.0, help="空闲轮询秒数")
    parser.add_argument("--lease-seconds", type=float, default=DEFAULT_LEASE_SECONDS)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    store = JobStore(args.business_root)
    if args.once:
        job = run_once(store, lease_seconds=args.lease_seconds, poll_seconds=args.poll)
        if args.as_json:
            print(json.dumps(job, ensure_ascii=False, indent=2, default=str))
        elif job is None:
            print("队列为空")
        else:
            print(f"{job['id']} {job['state']}")
        return 0
    return run_loop(store, poll_seconds=args.poll, lease_seconds=args.lease_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
