"""Durable single-node job queue for long-running processing plans.

The queue is deliberately local: one SQLite file, one worker by default, no
distributed scheduling and no multi-user claims. A job is a frozen ``process``
plan; the worker runs it in a child process and records the result. Leases make
a crashed worker recoverable, and the plan runner's state file provides the
second half of recovery.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

QUEUE_SCHEMA_VERSION = 2
JOB_STATES = ("queued", "running", "succeeded", "failed", "canceled")
TERMINAL_STATES = frozenset({"succeeded", "failed", "canceled"})
DEFAULT_LEASE_SECONDS = 60
DEFAULT_MAX_ATTEMPTS = 1


def utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _clean_root(root=None) -> Path:
    if root is None:
        from .business import get_store
        root = get_store().root
    return Path(root).resolve()


def default_root(root=None) -> Path:
    """Queue lives beside the business database, not in a global temp dir."""
    return _clean_root(root) / "queue"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _initialize(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        state TEXT NOT NULL,
        plan_digest TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        plan_path TEXT NOT NULL,
        business_root TEXT,
        runs_root TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        lease_owner TEXT,
        lease_expires REAL,
        pid INTEGER,
        pid_identity TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 1,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        exit_code INTEGER,
        result_json TEXT,
        error TEXT
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS jobs_state_created ON jobs(state, created_at)")
    connection.execute("CREATE INDEX IF NOT EXISTS jobs_lease ON jobs(state, lease_expires)")
    connection.execute(f"PRAGMA user_version={QUEUE_SCHEMA_VERSION}")


class JobStore:
    def __init__(self, root=None):
        self.root = default_root(root)
        self.db_path = self.root / "jobs.sqlite3"
        with _connect(self.db_path) as connection:
            _initialize(connection)

    def connect(self) -> sqlite3.Connection:
        return _connect(self.db_path)

    def job_dir(self, job_id: str) -> Path:
        return self.root / "jobs" / job_id

    def _row(self, job_id: str) -> sqlite3.Row | None:
        with _connect(self.db_path) as connection:
            return connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    def submit(self, plan: dict, *, plan_path=None, business_root=None, runs_root=None,
               max_attempts: int = DEFAULT_MAX_ATTEMPTS, idempotency_key: str | None = None) -> dict:
        from .process_plan import normalize_plan, plan_digest

        normalized = normalize_plan(plan)
        source_path = str(normalized["input"].get("path") or "").strip()
        if source_path and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", source_path):
            candidate = Path(source_path)
            if not candidate.is_absolute():
                candidate = (Path.cwd() / candidate).resolve()
            if candidate.exists():
                normalized["input"]["path"] = str(candidate)
        digest = plan_digest(normalized)
        resolved_business = str(_clean_root(business_root)) if business_root else None
        resolved_runs = str(Path(runs_root).resolve()) if runs_root else None
        key = idempotency_key or hashlib.sha256(
            _json({"plan_digest": digest, "business_root": resolved_business,
                   "runs_root": resolved_runs}).encode("utf-8")).hexdigest()
        with _connect(self.db_path) as connection:
            existing = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (key,)).fetchone()
            if existing is not None:
                return {"status": "existing", "job": public_job(existing)}
            job_id = "job_" + uuid.uuid4().hex[:16]
            directory = self.job_dir(job_id)
            directory.mkdir(parents=True, exist_ok=False)
            snapshot = directory / "plan.json"
            snapshot.write_text(json.dumps(normalized, ensure_ascii=False, indent=2,
                                           allow_nan=False), encoding="utf-8")
            now = utc_now()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("""INSERT INTO jobs (
                    id, kind, state, plan_digest, idempotency_key, plan_path,
                    business_root, runs_root, created_at, updated_at, max_attempts
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)""", (
                    job_id, "process_plan", digest, key, str(snapshot),
                    resolved_business, resolved_runs, now, now, max(1, int(max_attempts))))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            append_event(directory, "queued", "任务已入队")
            return {"status": "queued", "job": public_job(self._row(job_id))}

    def list(self, state: str | None = None, limit: int = 100) -> list[dict]:
        if state is not None and state not in JOB_STATES:
            raise ValueError(f"未知任务状态：{state}")
        query = "SELECT * FROM jobs"
        args: list = []
        if state:
            query += " WHERE state = ?"
            args.append(state)
        query += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        with _connect(self.db_path) as connection:
            return [public_job(row) for row in connection.execute(query, args)]

    def get(self, job_id: str) -> dict:
        row = self._row(job_id)
        if row is None:
            raise KeyError(f"任务不存在：{job_id}")
        return public_job(row, include_plan=False)

    def events(self, job_id: str, limit: int = 50) -> list[dict]:
        path = self.job_dir(job_id) / "events.jsonl"
        if not path.is_file():
            return []
        records = []
        for line in path.read_text(encoding="utf-8").splitlines()[-max(1, int(limit)):]:
            if line.strip():
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
        return records

    def _update(self, job_id: str, **values) -> None:
        if not values:
            return
        columns = ", ".join(f"{key} = ?" for key in values)
        args = list(values.values()) + [utc_now(), job_id]
        with _connect(self.db_path) as connection:
            connection.execute(f"UPDATE jobs SET {columns}, updated_at = ? WHERE id = ?", args)

    def cancel(self, job_id: str) -> dict:
        row = self._row(job_id)
        if row is None:
            raise KeyError(f"任务不存在：{job_id}")
        if row["state"] in TERMINAL_STATES:
            return public_job(row)
        now = utc_now()
        with _connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if row["state"] == "queued":
                    connection.execute(
                        "UPDATE jobs SET state='canceled', cancel_requested=1, finished_at=?, "
                        "updated_at=? WHERE id=? AND state='queued'", (now, now, job_id))
                else:
                    connection.execute(
                        "UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=?",
                        (now, job_id))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        append_event(self.job_dir(job_id), "cancel_requested", "已请求取消；运行中的阶段会在安全检查点停止")
        kill_job_process(self._row(job_id))
        return public_job(self._row(job_id))

    def retry(self, job_id: str) -> dict:
        row = self._row(job_id)
        if row is None:
            raise KeyError(f"任务不存在：{job_id}")
        if row["state"] not in {"failed", "canceled"}:
            raise ValueError("只有失败或已取消的任务可以重试")
        self._update(job_id, state="queued", started_at=None, finished_at=None,
                     lease_owner=None, lease_expires=None, pid=None, pid_identity=None,
                     attempts=0, cancel_requested=0, exit_code=None, result_json=None, error=None)
        append_event(self.job_dir(job_id), "retried", "任务已重新入队")
        return public_job(self._row(job_id))

    def claim(self, owner: str, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> dict | None:
        """Claim one job. An expired lease with a live child is adopted, not duplicated."""
        now_epoch = time.time()
        now = utc_now()
        with _connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                expired = connection.execute(
                    "SELECT * FROM jobs WHERE state='running' AND lease_expires IS NOT NULL "
                    "AND lease_expires < ?", (now_epoch,)).fetchall()
                for row in expired:
                    alive = bool(row["pid"]) and _alive(row["pid"], row["pid_identity"])
                    if alive:
                        continue
                    if row["cancel_requested"]:
                        connection.execute(
                            "UPDATE jobs SET state='canceled', lease_owner=NULL, lease_expires=NULL, "
                            "finished_at=?, updated_at=?, error=? WHERE id=?",
                            (now, now, "已请求取消", row["id"]))
                        append_event(self.job_dir(row["id"]), "canceled", "租约过期后按取消请求收尾")
                    elif int(row["attempts"]) >= int(row["max_attempts"]):
                        connection.execute(
                            "UPDATE jobs SET state='failed', lease_owner=NULL, lease_expires=NULL, "
                            "finished_at=?, updated_at=?, error=? WHERE id=?",
                            (now, now, "worker 租约过期且达到最大尝试次数", row["id"]))
                        append_event(self.job_dir(row["id"]), "failed",
                                     "worker 租约过期且达到最大尝试次数")
                    else:
                        connection.execute(
                            "UPDATE jobs SET state='queued', lease_owner=NULL, lease_expires=NULL, "
                            "pid=NULL, pid_identity=NULL, updated_at=? WHERE id=?",
                            (now, row["id"]))
                        append_event(self.job_dir(row["id"]), "requeued",
                                     "worker 租约过期，任务重新入队")
                row = connection.execute(
                    "SELECT * FROM jobs WHERE state='queued' ORDER BY created_at LIMIT 1").fetchone()
                adopt = False
                if row is None:
                    row = connection.execute(
                        "SELECT * FROM jobs WHERE state='running' AND lease_expires IS NOT NULL "
                        "AND lease_expires < ? AND pid IS NOT NULL ORDER BY created_at LIMIT 1",
                        (now_epoch,)).fetchone()
                    adopt = row is not None
                if row is None:
                    connection.execute("COMMIT")
                    return None
                lease = now_epoch + max(5.0, float(lease_seconds))
                if adopt:
                    connection.execute(
                        "UPDATE jobs SET lease_owner=?, lease_expires=?, updated_at=? WHERE id=?",
                        (owner, lease, now, row["id"]))
                else:
                    connection.execute(
                        "UPDATE jobs SET state='running', lease_owner=?, lease_expires=?, "
                        "started_at=COALESCE(started_at, ?), updated_at=?, "
                        "attempts=attempts + 1 WHERE id=?",
                        (owner, lease, now, now, row["id"]))
                connection.execute("COMMIT")
                result = public_job(self._row(row["id"]))
                result["adopt"] = adopt
                append_event(self.job_dir(row["id"]), "adopted" if adopt else "started",
                             "接管已有后台进程" if adopt else "worker 已领取任务")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def heartbeat(self, job_id: str, owner: str,
                  lease_seconds: float = DEFAULT_LEASE_SECONDS) -> bool | None:
        """Return the cancel flag, or ``None`` when the lease was lost."""
        now = time.time() + max(5.0, float(lease_seconds))
        with _connect(self.db_path) as connection:
            cursor = connection.execute(
                "UPDATE jobs SET lease_expires=?, updated_at=? "
                "WHERE id=? AND state='running' AND lease_owner=?",
                (now, utc_now(), job_id, owner))
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
            return bool(row and row["cancel_requested"])

    def set_pid(self, job_id: str, owner: str, pid: int, identity: str | None) -> None:
        with _connect(self.db_path) as connection:
            connection.execute(
                "UPDATE jobs SET pid=?, pid_identity=?, updated_at=? "
                "WHERE id=? AND state='running' AND lease_owner=?",
                (int(pid), identity, utc_now(), job_id, owner))

    def finish(self, job_id: str, owner: str, state: str, *, result=None, error=None,
               exit_code=None) -> bool:
        if state not in TERMINAL_STATES:
            raise ValueError(f"非法终态：{state}")
        now = utc_now()
        with _connect(self.db_path) as connection:
            cursor = connection.execute(
                "UPDATE jobs SET state=?, finished_at=?, updated_at=?, lease_owner=NULL, "
                "lease_expires=NULL, result_json=?, error=?, exit_code=? "
                "WHERE id=? AND state='running' AND lease_owner=?",
                (state, now, now, _json(result) if result is not None else None,
                 error, exit_code, job_id, owner))
            if cursor.rowcount != 1:
                return False
        append_event(self.job_dir(job_id), state, error or f"任务结束：{state}")
        return True

    def summary(self) -> dict:
        with _connect(self.db_path) as connection:
            counts = {state: 0 for state in JOB_STATES}
            for row in connection.execute(
                    "SELECT state, COUNT(*) AS count FROM jobs GROUP BY state"):
                counts[row["state"]] = int(row["count"])
            running = connection.execute(
                "SELECT * FROM jobs WHERE state='running' ORDER BY started_at LIMIT 1").fetchone()
        return {"counts": counts, "total": sum(counts.values()),
                "running": public_job(running) if running else None,
                "root": str(self.root)}


def public_job(row: sqlite3.Row | None, *, include_plan: bool = False) -> dict | None:
    if row is None:
        return None
    value = {key: row[key] for key in row.keys()
             if key not in {"result_json", "plan_path", "business_root", "runs_root"}}
    value["cancel_requested"] = bool(value.get("cancel_requested"))
    if include_plan and row["plan_path"]:
        try:
            value["plan"] = json.loads(Path(row["plan_path"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            value["plan"] = None
    if row["result_json"]:
        try:
            value["result"] = json.loads(row["result_json"])
        except ValueError:
            value["result"] = None
    return value


def append_event(directory: Path, event: str, message: str, **details) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    record = {"timestamp": utc_now(), "event": event, "message": message, **details}
    line = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    descriptor = os.open(directory / "events.jsonl",
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, line)
    finally:
        os.close(descriptor)


def _alive(pid, identity=None) -> bool:
    from .runtime import _alive as runtime_alive
    return runtime_alive(pid, identity)


def kill_job_process(row: sqlite3.Row | None) -> bool:
    """Kill a recorded child tree without guessing a reused PID."""
    if row is None or not row["pid"] or not _alive(row["pid"], row["pid_identity"]):
        return False
    pid = int(row["pid"])
    if os.name == "nt":
        import subprocess
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, check=False)
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return False
    return True


def store_for(root=None) -> JobStore:
    return JobStore(root)
