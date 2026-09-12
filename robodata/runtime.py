"""Persistent local runs, supervised child processes, and append-only node events.

Only the coordinator writes state.json. Workers publish atomic progress/checkpoints;
the UI can disappear without owning process lifetime. No private data is sent anywhere.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import DEFAULT_DATA_ROOT, EPISODES, PROJECT_ROOT

DEFAULT_RUNS_ROOT = PROJECT_ROOT / "work" / "runs"
ACTIVE_STATUSES = frozenset({"queued", "running"})
TERMINAL_STATUSES = frozenset({"completed", "completed_with_errors", "failed", "interrupted"})
NODE_LABELS = {"input_validation": "输入核验", "table_read": "任务表读取",
               "table_checks": "表格质检", "video_decode": "视频解码",
               "video_checks": "视频帧数与时间对应检查", "report": "汇总报告"}
EPISODE_NODES = ("table_read", "table_checks", "video_decode", "video_checks")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
        # A reader briefly holding the file on Windows must not break a run.
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.025)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path, default=None):
    # Windows may briefly deny readers while another process atomically replaces
    # a checkpoint. A transient sharing violation is not a failed run.
    for attempt in range(20):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.025)


def _root(runs_root=None) -> Path:
    return Path(runs_root or DEFAULT_RUNS_ROOT).resolve()


def _run_dir(run_id: str, runs_root=None) -> Path:
    if not isinstance(run_id, str) or not run_id or Path(run_id).name != run_id or "/" in run_id or "\\" in run_id:
        raise ValueError("运行编号无效")
    path = _root(runs_root) / run_id
    if path.parent != _root(runs_root):
        raise ValueError("运行编号不能越过运行目录")
    return path


def _identity(pid: int | None) -> str | None:
    """Creation identity avoids treating a recycled PID as the same coordinator."""
    if not pid or pid < 1:
        return None
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            return None
        try:
            if kernel.WaitForSingleObject(handle, 0) != 0x102:
                return None
            created, exited, kernel_time, user = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user)):
                return None
            return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        parts = stat[stat.rfind(")") + 2:].split()
        return None if parts[0] == "Z" else parts[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def _alive(pid, identity=None) -> bool:
    current = _identity(pid)
    return current is not None and (identity is None or current == identity)


def _event(directory: Path, stage: str, event: str, message: str, episode_index=None,
           level="INFO", **details) -> None:
    record = {"run_id": directory.name, "timestamp": utc_now(), "episode_index": episode_index,
              "node_id": stage, "event": event, "level": level, "message": message, **details}
    line = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    descriptor = os.open(directory / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, line)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _node(stage: str, episode_index=None) -> dict:
    return {"id": f"{episode_index}:{stage}" if episode_index is not None else stage,
            "node_id": stage, "label": NODE_LABELS.get(stage, stage), "episode_index": episode_index,
            "status": "pending", "started_at": None, "completed_at": None,
            "elapsed_seconds": 0, "message": "等待执行", "data_issue_count": 0}


def _set_node(node: dict, status: str, message: str, **details) -> None:
    now = utc_now()
    node.update(status=status, message=message, **details)
    if status == "running":
        node["started_at"] = now
    elif status != "pending":
        node["completed_at"] = now
        if node["started_at"]:
            node["elapsed_seconds"] = round((datetime.fromisoformat(now) - datetime.fromisoformat(node["started_at"])).total_seconds(), 4)


def _node_for(state: dict, stage: str, index=None) -> dict:
    return next(n for n in state["nodes"] if n["node_id"] == stage and n["episode_index"] == index)


def _save_state(directory: Path, state: dict) -> None:
    state["updated_at"] = utc_now()
    running = [n for n in state["nodes"] if n["status"] == "running"]
    state["current_node"] = running[-1]["id"] if running else None
    state["elapsed_seconds"] = round((datetime.now(timezone.utc) - datetime.fromisoformat(state["started_at"])).total_seconds(), 3)
    _atomic_json(directory / "state.json", state)


def _normalize_request(request: dict) -> dict:
    from .quality import normalize_params
    request = dict(request)
    kind = request.setdefault("kind", "quality")
    if kind not in {"quality", "conversion", "business"}:
        raise ValueError("运行类型必须为 quality、conversion 或 business")
    if kind == "business":
        from .business import BUSINESS_STAGES
        if request.get("operation") not in BUSINESS_STAGES:
            raise ValueError("业务处理操作无效")
    request["data_root"] = str(Path(request.get("data_root", DEFAULT_DATA_ROOT)).resolve())
    request["params"] = normalize_params(request.get("params"))
    indices = request.get("episode_indices") if kind == "quality" else []
    if indices is not None and kind == "quality" and (not indices or any(isinstance(i, bool) or not isinstance(i, int) or i < 0 for i in indices) or len(indices) != len(set(indices))):
        raise ValueError("任务编号必须是非空且不重复的非负整数列表")
    request["episode_indices"] = list(indices) if indices is not None else None
    request["check_video"] = bool(request.get("check_video", True))
    request.setdefault("source_kind", "original_public")
    defaults = {"default": 60.0, "video_decode": 120.0}
    if kind == "conversion":
        from .conversion import STAGE_TIMEOUTS
        defaults.update(STAGE_TIMEOUTS)
    limits = {**defaults, **request.get("timeouts", {})}
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in limits.values()):
        raise ValueError("节点超时必须是大于零的有限秒数")
    request["timeouts"] = limits
    request.setdefault("execution_demo", "none")
    if request["execution_demo"] not in {"none", "video_timeout"}:
        raise ValueError("仅支持正常运行或人为视频节点超时演示")
    if request.get("output_dir"):
        request["output_dir"] = str(Path(request["output_dir"]).resolve())
    return request


def _spawn(command: list[str], stdout, stderr, env_overrides=None, **kwargs) -> subprocess.Popen:
    environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", **(env_overrides or {})}
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    return subprocess.Popen(command, cwd=PROJECT_ROOT, env=environment, stdin=subprocess.DEVNULL,
                            stdout=stdout, stderr=stderr, close_fds=True, **options, **kwargs)


class _ProcessTree:
    """Windows kill-on-close job owns the worker and every encoder descendant.

    The coordinator holds the only job handle. An abrupt coordinator exit closes
    it in the OS, so cleanup does not depend on a UI poll or a Python finally.
    Unix workers already start in their own session and use the process group.
    """
    def __init__(self):
        self.handle = None
        self.pid = None
        if os.name != "nt":
            return
        from ctypes import wintypes
        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", IoCounters),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def attach(self, process: subprocess.Popen):
        self.pid = process.pid
        if os.name == "nt" and not self.kernel.AssignProcessToJobObject(self.handle, process._handle):
            error = ctypes.WinError(ctypes.get_last_error())
            process.kill()
            process.wait()
            raise error

    def close(self):
        if os.name == "nt":
            if self.handle:
                self.kernel.CloseHandle(self.handle)
                self.handle = None
        elif self.pid:
            import signal
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.pid = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


@contextmanager
def _startup_guard(root: Path):
    """OS byte lock serializes stale-lock recovery as well as process creation."""
    guard = (root / ".startup.guard").open("a+b")
    acquired = False
    try:
        if guard.seek(0, 2) == 0:
            guard.write(b"0")
            guard.flush()
        deadline = time.monotonic() + 10
        while True:
            guard.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("另一个操作正在登记运行，请稍后查看运行记录") from None
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            guard.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(guard.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(guard.fileno(), fcntl.LOCK_UN)
        guard.close()


def start_run(request: dict, runs_root: Path | None = None) -> str:
    root = _root(runs_root)
    root.mkdir(parents=True, exist_ok=True)
    with _startup_guard(root):
        return _start_run_locked(request, root)


def _start_run_locked(request: dict, runs_root: Path) -> str:
    request = _normalize_request(request)
    root = _root(runs_root)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".active.lock"
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:12]
    for _ in range(3):
        try:
            handle = lock_path.open("x", encoding="utf-8")
        except FileExistsError:
            try:
                lock = _read_json(lock_path, {})
            except json.JSONDecodeError:
                raise RuntimeError("另一个运行正在启动，请稍后重试") from None
            prior = _read_json(root / str(lock.get("run_id", "")) / "state.json", {})
            if prior and prior.get("status") in ACTIVE_STATUSES:
                prior = get_run(lock["run_id"], root)
            if _alive(lock.get("pid"), lock.get("identity")) and prior.get("status") not in TERMINAL_STATUSES:
                raise RuntimeError(f"已有处理运行 {lock.get('run_id', '')}，请先查看运行记录")
            if lock.get("run_id") and prior:
                get_run(lock["run_id"], root)
            # 锁持有者已退出。文件可能在检查与本行之间被其他进程清理，
            # 先判断存在再删除，避免在受限的删除实现下抛出不相关的错误。
            if lock_path.exists():
                lock_path.unlink()
        else:
            with handle:
                json.dump({"pid": os.getpid(), "identity": _identity(os.getpid()), "run_id": run_id}, handle)
            break
    else:
        raise RuntimeError("无法获取本机运行锁，请稍后重试")
    directory = root / run_id
    try:
        directory.mkdir()
        if request["kind"] == "business":
            from .business import business_nodes
            nodes = business_nodes(request["operation"], request)
        elif request["kind"] == "conversion":
            from .conversion import CONVERSION_STAGES
            stages = ("file_validation", "official_verify", "output_quality") if request.get("verify_only") else tuple(CONVERSION_STAGES)
            nodes = []
            for stage in stages:
                node = _node(stage)
                node["label"] = CONVERSION_STAGES[stage]
                nodes.append(node)
        else:
            nodes = [_node("input_validation")]
            for index in request["episode_indices"] or []:
                nodes.extend(_node(stage, index) for stage in EPISODE_NODES)
            nodes.append(_node("report"))
        state = {"run_id": run_id, "status": "queued", "request": request, "nodes": nodes,
                 "started_at": utc_now(), "completed_at": None, "coordinator_pid": None,
                 "launcher_pid": os.getpid(), "launcher_identity": _identity(os.getpid()),
                 "report_path": None, "artifacts": {}, "episode_completed": 0,
                 "episode_total": len(request["episode_indices"] or []), "current_node": None,
                 "message": "等待后台协调进程启动"}
        _atomic_json(directory / "request.json", request)
        _save_state(directory, state)
        _event(directory, "run", "created", "已创建独立运行；关闭浏览器不会停止后台处理")
        with (directory / "coordinator.stdout.log").open("ab") as out, (directory / "coordinator.stderr.log").open("ab") as err:
            process = _spawn([sys.executable, "-m", "robodata.runtime", "--coordinate", str(directory)], out, err)
        _atomic_json(lock_path, {"run_id": run_id, "pid": process.pid, "identity": _identity(process.pid)})
        # The child owns subsequent state and lock updates.
        return run_id
    except BaseException:
        if _read_json(lock_path, {}).get("run_id") == run_id and lock_path.exists():
            lock_path.unlink()
        raise


def _terminate_identity(pid: int, identity: str | None) -> None:
    if not _alive(pid, identity):
        return
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(1, False, pid)
        if handle:
            try:
                kernel.TerminateProcess(handle, 137)
            finally:
                kernel.CloseHandle(handle)
    else:
        import signal
        os.kill(pid, signal.SIGKILL)


def get_run(run_id: str, runs_root: Path | None = None) -> dict:
    directory = _run_dir(run_id, runs_root)
    state = _read_json(directory / "state.json")
    if state is None:
        raise FileNotFoundError(f"运行记录不存在：{run_id}")
    if state["status"] in ACTIVE_STATUSES:
        pid = state.get("coordinator_pid")
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(state["started_at"])).total_seconds()
        dead = not _alive(pid, state.get("coordinator_identity")) if pid else age > 20
        if dead:
            _terminate_identity(state.get("worker_pid"), state.get("worker_identity"))
            state.update(status="interrupted", completed_at=utc_now(), message="后台协调进程已退出；此前完成的阶段结果和日志仍保留")
            for node in state["nodes"]:
                if node["status"] == "running":
                    _set_node(node, "interrupted", "协调进程退出，节点中断")
                elif node["status"] == "pending":
                    _set_node(node, "skipped", "运行中断，未执行")
            _event(directory, "run", "interrupted", state["message"], level="ERROR")
            _save_state(directory, state)
    return state


def list_runs(runs_root: Path | None = None) -> list[dict]:
    root = _root(runs_root)
    result = []
    for path in sorted(root.glob("*/state.json"), reverse=True):
        try:
            result.append(get_run(path.parent.name, root))
        except (OSError, ValueError):
            continue
    return result


def read_events(run_id: str, runs_root: Path | None = None, *, episode_index=None,
                node_id=None, level=None, **filters) -> list[dict]:
    path = _run_dir(run_id, runs_root) / "events.jsonl"
    if not path.is_file():
        return []
    for attempt in range(20):
        try:
            contents = path.read_bytes()
            break
        except FileNotFoundError:
            return []
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.025)
    result = []
    # A killed writer can leave an incomplete JSON record or even half a UTF-8
    # character. Decode complete records independently so earlier logs survive.
    for line in contents.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            continue
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue  # A concurrent writer may not yet have completed its last line.
        if not isinstance(record, dict):
            continue
        if episode_index is not None and record.get("episode_index") != episode_index:
            continue
        if node_id is not None and record.get("node_id") != node_id:
            continue
        if level is not None and record.get("level") != level:
            continue
        result.append(record)
    return result


def wait_run(run_id: str, runs_root: Path | None = None, timeout: float = 600) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = get_run(run_id, runs_root)
        if state["status"] in TERMINAL_STATUSES:
            return state
        time.sleep(0.2)
    raise TimeoutError("等待运行结果超时；后台运行仍可从运行记录查看")


class Progress:
    """Worker-local checkpoint writer. All data saved before stage completion event."""
    def __init__(self, directory: Path, index=None):
        self.directory, self.index = directory, index
        self.nodes = {}
        self.current = None

    def __call__(self, stage: str, event: str, details: dict | None = None):
        details = details or {}
        node = self.nodes.setdefault(stage, _node(stage, self.index))
        status = "running" if event == "started" else event
        result = details.get("result")
        if result is not None and event != "started":
            _atomic_json(self.directory / "checkpoints" / f"episode_{self.index}.json", result)
        count = sum(i["category"] == "data_issue" for i in (result or {}).get("issues", []))
        previous = sum(n.get("data_issue_count", 0) for name, n in self.nodes.items() if name != stage)
        count = max(0, count - previous)
        message = details.get("message") or ("开始执行" if status == "running" else
                  "检查完成，发现数据异常" if count else "节点已完成")
        clean_details = {k: v for k, v in details.items() if k != "result"}
        clean_details.pop("message", None)
        _set_node(node, status, message, data_issue_count=count, **clean_details)
        self.current = stage if status == "running" else None
        _atomic_json(self.directory / "progress.json", {"nodes": list(self.nodes.values()), "current": self.current})
        _event(self.directory, stage, event, message, self.index,
               level="ERROR" if status in {"failed", "timed_out"} else "WARNING" if count else "INFO", **clean_details)
        if event == "completed" and result:
            for item in result.get("issues", []):
                if item.get("node_id") == stage:
                    _event(self.directory, stage, "issue", item["message"], self.index,
                           level="WARNING" if item["category"] == "data_issue" else "INFO",
                           row_index=item.get("row_index"), rule=item["rule"], evidence=item.get("evidence", {}))


def _parent_watchdog(pid, identity):
    while True:
        time.sleep(0.25)
        if not _alive(pid, identity):
            os._exit(143)


def _worker(directory: Path, operation: str, index=None) -> int:
    from .dataset import load_episode, load_metadata
    from .errors import error_details, explain_error
    from .quality import assemble_report, failed_episode, prepare_batch, run_checks
    from .reports import write_report
    request = _read_json(directory / "request.json")
    state = _read_json(directory / "state.json")
    threading.Thread(target=_parent_watchdog, args=(state["coordinator_pid"], state["coordinator_identity"]), daemon=True).start()
    progress = Progress(directory, index)
    stage = {"prepare": "input_validation", "report": "report"}.get(operation, "table_read")
    try:
        if operation == "prepare":
            progress(stage, "started", {"input_path": request["data_root"]})
            load_metadata(Path(request["data_root"]))  # Fail clearly at the input node.
            context = prepare_batch(Path(request["data_root"]), request["episode_indices"], request["params"],
                                    request["check_video"], request["source_kind"])
            _atomic_json(directory / "context.json", context)
            progress(stage, "completed")
        elif operation == "episode":
            progress(stage, "started", {"input_path": request["data_root"]})
            episode = load_episode(Path(request["data_root"]), index)
            checkpoint = {"episode_index": index, "row_count": len(episode.table), "expected_length": episode.expected_length,
                          "issues": [], "coverage": [], "video": {"status": "not_checked"}, "status": "incomplete"}
            progress(stage, "completed", {"result": checkpoint, "input_path": str(episode.data_path)})
            def callback(name, event, details):
                nonlocal stage
                stage = name
                progress(name, event, details)
                if name == "video_decode" and event == "started" and index == 0 and request["execution_demo"] == "video_timeout":
                    _event(directory, name, "injected_wait", "人为运行故障演示：主动等待以触发超时，不代表原始视频损坏", index, level="WARNING")
                    time.sleep(30)
            result = run_checks(episode, request["params"], request["check_video"], callback)
            _atomic_json(directory / "results" / f"episode_{index}.json", result)
        elif operation == "report":
            progress(stage, "started")
            context = _read_json(directory / "context.json")
            episodes = [_read_json(directory / "results" / f"episode_{i}.json") for i in request["episode_indices"]]
            report = assemble_report(context, episodes, (datetime.now(timezone.utc) - datetime.fromisoformat(state["started_at"])).total_seconds())
            report["run_id"] = directory.name
            report["execution"] = {"nodes": state["nodes"], "events_path": "events.jsonl",
                                   "execution_demo": request["execution_demo"]}
            # The embedded record describes this report's completed generation.
            # The authoritative state remains running until all file writes finish;
            # a write error marks that node failed and registers no usable artifacts.
            report_node = next(n for n in report["execution"]["nodes"] if n["node_id"] == "report")
            _set_node(report_node, "completed", "本报告已生成；最终发布状态以运行记录为准")
            paths = write_report(report, directory / "reports")
            if request.get("output_dir"):
                destination = Path(request["output_dir"])
                destination.mkdir(parents=True, exist_ok=True)
                for path in paths.values():
                    if path.resolve().parent != destination.resolve():
                        shutil.copy2(path, destination / path.name)
            _atomic_json(directory / "artifacts.json", {key: str(path) for key, path in paths.items()})
            progress(stage, "completed")
        else:
            raise ValueError("未知后台工作类型")
        return 0
    except Exception as exc:
        diagnostics = error_details(exc, include_traceback=True)
        message = explain_error(exc, NODE_LABELS.get(stage, stage))
        progress(stage, "failed", {"message": message, "diagnostics": diagnostics})
        if operation == "episode":
            result = failed_episode(index, exc, stage, _read_json(directory / "checkpoints" / f"episode_{index}.json"))
            _atomic_json(directory / "results" / f"episode_{index}.json", result)
        return 1


def _supervise(directory: Path, state: dict, operation: str, index=None,
               command: list[str] | None = None, fixed_stage: str | None = None,
               env_overrides: dict | None = None) -> bool:
    """Run one process, track checkpoint transitions, enforce current-node deadline."""
    progress_path = directory / "progress.json"
    progress_path.unlink(missing_ok=True)
    stage = fixed_stage or {"prepare": "input_validation", "report": "report"}.get(operation, "table_read")
    command = command or [sys.executable, "-m", "robodata.runtime", "--worker", operation,
                           "--directory", str(directory), *(["--index", str(index)] if index is not None else [])]
    output_name = f"{operation}_{index}" if index is not None else operation
    initial = _node_for(state, stage, index)
    _set_node(initial, "running", "正在启动工作进程")
    _event(directory, stage, "process_starting", "正在启动受监督的工作进程", index)
    with _ProcessTree() as process_tree:
        with (directory / f"{output_name}.stdout.log").open("ab") as out, (directory / f"{output_name}.stderr.log").open("ab") as err:
            process = _spawn(command, out, err, env_overrides=env_overrides)
        process_tree.attach(process)
        state.update(worker_pid=process.pid, worker_identity=_identity(process.pid))
        start = time.monotonic()
        last_key = None
        failure_status = None
        failure_message = None
        _save_state(directory, state)
        while True:
            progress = _read_json(progress_path, {})
            for update in progress.get("nodes", []):
                _node_for(state, update["node_id"], update["episode_index"]).update(update)
            failed_nodes = [n for n in progress.get("nodes", []) if n["status"] == "failed"]
            if failed_nodes:
                stage = failed_nodes[-1]["node_id"]
            running = [n for n in state["nodes"] if n["status"] == "running"]
            if running:
                stage = running[-1]["node_id"]
                key = (stage, running[-1]["started_at"])
                if key != last_key:
                    last_key, start = key, time.monotonic()
            exit_code = process.poll()
            if exit_code is not None:
                failure_status = "failed" if exit_code else None
                break
            limit = state["request"]["timeouts"].get(stage, state["request"]["timeouts"]["default"])
            if stage == "video_decode" and index == 0 and state["request"]["execution_demo"] == "video_timeout":
                limit = min(limit, 1.0)
            if time.monotonic() - start > limit:
                process_tree.close()
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
                failure_status = "timed_out"
                node = _node_for(state, stage, index)
                message = f"节点超过 {limit:g} 秒预算，工作进程已终止；保留已完成检查并继续后续任务"
                _set_node(node, "timed_out", message, worker_pid=process.pid, worker_terminated=True)
                _event(directory, stage, "timed_out", message, index, level="ERROR", worker_pid=process.pid, worker_terminated=True)
                break
            _save_state(directory, state)
            time.sleep(0.1)
        process.wait()
        # Poll one final time after process exit to catch its last atomic update.
        if failure_status != "timed_out":
            for update in _read_json(progress_path, {}).get("nodes", []):
                _node_for(state, update["node_id"], update["episode_index"]).update(update)
        expected_artifact = {"prepare": directory / "context.json",
                             "episode": directory / "results" / f"episode_{index}.json",
                             "report": directory / "artifacts.json"}.get(operation)
        if not fixed_stage and failure_status is None and expected_artifact and not expected_artifact.is_file():
            failure_status = "failed"
            failure_message = "工作进程已退出，但没有保存本阶段结果；已保留此前检查并记录为执行失败"
        if fixed_stage and failure_status is None:
            node = _node_for(state, fixed_stage, index)
            if node["status"] == "running":
                _set_node(node, "completed", "外部工作进程执行完成")
                _event(directory, fixed_stage, "completed", node["message"], index)
        state.update(worker_pid=None, worker_identity=None)
        if failure_status:
            node = _node_for(state, stage, index)
            if node["status"] not in {"failed", "timed_out"}:
                message = failure_message or f"工作进程意外退出（退出码 {process.returncode}），请查看节点诊断日志"
                _set_node(node, "failed", message, exit_code=process.returncode)
                _event(directory, stage, "failed", message, index, level="ERROR", exit_code=process.returncode,
                       stderr_path=str(directory / f"{output_name}.stderr.log"))
            if index is not None:
                from .quality import failed_episode
                result_path = directory / "results" / f"episode_{index}.json"
                if not result_path.is_file():
                    checkpoint = _read_json(directory / "checkpoints" / f"episode_{index}.json")
                    exc = TimeoutError(node["message"]) if failure_status == "timed_out" else RuntimeError(node["message"])
                    _atomic_json(result_path, failed_episode(index, exc, stage, checkpoint, node["message"]))
        for node in state["nodes"]:
            if node["episode_index"] == index and node["status"] == "pending" and index is not None:
                _set_node(node, "skipped", "前置节点未完成，未执行")
                _event(directory, node["node_id"], "skipped", node["message"], index)
        _save_state(directory, state)
        return failure_status is None


def run_command_step(directory: Path, state: dict, stage: str, command: list[str],
                     timeout: float | None = None, env_overrides: dict | None = None) -> bool:
    """Supervise one external-environment step using the same persisted run/logs."""
    if not any(n["node_id"] == stage and n["episode_index"] is None for n in state["nodes"]):
        state["nodes"].append(_node(stage))
    if timeout is not None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("外部节点超时必须大于零")
        state["request"]["timeouts"][stage] = timeout
    return _supervise(directory, state, stage, command=command, fixed_stage=stage, env_overrides=env_overrides)


def coordinate(directory: Path) -> None:
    from .errors import error_details, explain_error
    state = _read_json(directory / "state.json")
    state.update(status="running", coordinator_pid=os.getpid(), coordinator_identity=_identity(os.getpid()),
                 message="后台运行中")
    _atomic_json(directory.parent / ".active.lock", {"run_id": directory.name, "pid": os.getpid(), "identity": _identity(os.getpid())})
    _save_state(directory, state)
    try:
        if state["request"]["kind"] == "business":
            from .business import coordinate_business
            coordinate_business(directory, state)
            return
        if state["request"]["kind"] != "quality":
            # Reuse the same lock, history, and process supervision for conversion.
            from .conversion import coordinate_conversion
            coordinate_conversion(directory, state, _supervise)
            return
        if not _supervise(directory, state, "prepare"):
            state.update(status="failed", message="输入核验失败，未开始任务检查；请查看输入节点日志")
        else:
            context = _read_json(directory / "context.json")
            if state["request"]["episode_indices"] is None:
                state["request"]["episode_indices"] = context["indices"]
                state["episode_total"] = len(context["indices"])
                state["nodes"][1:1] = [_node(stage, index) for index in context["indices"] for stage in EPISODE_NODES]
                _atomic_json(directory / "request.json", state["request"])
                _save_state(directory, state)
            for index in state["request"]["episode_indices"]:
                _supervise(directory, state, "episode", index)
                state["episode_completed"] += 1
                _save_state(directory, state)
            if _supervise(directory, state, "report"):
                artifacts = _read_json(directory / "artifacts.json", {})
                state.update(artifacts=artifacts, report_path=artifacts.get("json"))
                report = _read_json(Path(state["report_path"]))
                state["summary"] = report["summary"]
                state["result_digest"] = report["result_digest"]
                errors = any(n["status"] in {"failed", "timed_out", "interrupted"} for n in state["nodes"])
                state.update(status="completed_with_errors" if errors else "completed",
                             message="检查流程已完成，部分节点执行失败；已生成完整性说明和报告" if errors else
                             "检查流程已完成，发现数据异常" if report["summary"]["data_issue_count"] else "检查流程已完成")
            else:
                state.update(status="failed", message="报告节点失败；此前的检查结果和日志保留在本次运行目录")
    except BaseException as exc:
        state.update(status="failed", message=explain_error(exc, "协调运行"), diagnostics=error_details(exc, include_traceback=True))
        _event(directory, "run", "failed", state["message"], level="ERROR", diagnostics=state["diagnostics"])
    finally:
        for node in state["nodes"]:
            if node["status"] == "pending":
                _set_node(node, "skipped", "前置步骤失败，未执行")
            elif node["status"] == "running":
                _set_node(node, "failed", "协调运行异常退出；参见运行诊断", diagnostics=state.get("diagnostics", {}))
        state["completed_at"] = utc_now()
        _save_state(directory, state)
        _event(directory, "run", state["status"], state["message"], level="ERROR" if state["status"] == "failed" else "INFO")
        lock = directory.parent / ".active.lock"
        if _read_json(lock, {}).get("run_id") == directory.name:
            lock.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coordinate", type=Path)
    parser.add_argument("--worker", choices=["prepare", "episode", "report"])
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--index", type=int)
    args = parser.parse_args()
    if args.coordinate:
        coordinate(args.coordinate)
    elif args.worker and args.directory:
        raise SystemExit(_worker(args.directory, args.worker, args.index))
    else:
        parser.error("请选择后台协调或工作进程入口")


if __name__ == "__main__":
    main()
