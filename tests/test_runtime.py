"""Actual process supervision with explicitly synthetic fixtures, no source mutations."""
from pathlib import Path
import json
import sys
import time
import subprocess

import av
import numpy as np
import pandas as pd
import pytest

from robodata import runtime
from robodata.config import CAMERA


@pytest.fixture
def tiny_dataset(tmp_path):
    root = tmp_path / "explicit-test-fixture"
    (root / "meta").mkdir(parents=True)
    info = {"fps": 30, "features": {"action": {"shape": [6]}, "observation.state": {"shape": [6]}}}
    (root / "meta/info.json").write_text(json.dumps(info), encoding="utf-8")
    episodes = [{"episode_index": i, "length": 5, "tasks": ["synthetic test"]} for i in range(2)]
    (root / "meta/episodes.jsonl").write_text("\n".join(json.dumps(e) for e in episodes), encoding="utf-8")
    (root / "meta/tasks.jsonl").write_text(json.dumps({"task_index": 0, "task": "synthetic test"}), encoding="utf-8")
    data = root / "data/chunk-000"
    videos = root / f"videos/chunk-000/{CAMERA}"
    data.mkdir(parents=True)
    videos.mkdir(parents=True)
    for i in range(2):
        table = pd.DataFrame({"episode_index": [i] * 5, "frame_index": list(range(5)),
                              "timestamp": np.arange(5) / 30, "action": [np.ones(6)] * 5,
                              "observation.state": [np.zeros(6)] * 5})
        table.to_parquet(data / f"episode_{i:06}.parquet")
        with av.open(str(videos / f"episode_{i:06}.mp4"), "w") as container:
            stream = container.add_stream("libx264", rate=30)
            stream.width, stream.height, stream.pix_fmt = 32, 32, "yuv420p"
            for frame_index in range(5):
                image = np.full((32, 32, 3), frame_index * 20, dtype=np.uint8)
                frame = av.VideoFrame.from_ndarray(image, format="rgb24")
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
    return root


def launch(root, tmp_path, **options):
    runs_root = tmp_path / "runs"
    request = {"data_root": str(root), "episode_indices": [0, 1], "source_kind": "injected_test", **options}
    run_id = runtime.start_run(request, runs_root)
    return runtime.wait_run(run_id, runs_root, timeout=60), runs_root


def test_real_processes_preserve_digest_and_history(tiny_dataset, tmp_path):
    first, runs = launch(tiny_dataset, tmp_path)
    second, _ = launch(tiny_dataset, tmp_path)
    assert first["run_id"] != second["run_id"]
    assert first["result_digest"] == second["result_digest"]
    assert first["status"] == "completed"
    assert first["summary"]["coverage_performed"] == 22
    assert all(n["status"] == "completed" for n in first["nodes"])
    assert len(runtime.list_runs(runs)) == 2
    events = runtime.read_events(first["run_id"], runs, episode_index=1, node_id="table_read")
    assert events and all(e["episode_index"] == 1 and e["node_id"] == "table_read" for e in events)
    assert Path(first["report_path"]).is_file()
    report = json.loads(Path(first["report_path"]).read_text(encoding="utf-8"))
    assert all(n["status"] == "completed" for n in report["execution"]["nodes"])
    assert first["coordinator_pid"] != __import__("os").getpid()


def test_video_timeout_is_reaped_keeps_table_and_continues(tiny_dataset, tmp_path):
    state, runs = launch(tiny_dataset, tmp_path, execution_demo="video_timeout")
    assert state["status"] == "completed_with_errors"
    timed_out = next(n for n in state["nodes"] if n["status"] == "timed_out")
    assert timed_out["node_id"] == "video_decode" and timed_out["episode_index"] == 0
    assert timed_out["worker_terminated"] and not runtime._alive(timed_out["worker_pid"])
    assert state["summary"]["coverage_performed"] == 19
    assert state["summary"]["load_failure_count"] == 1
    assert state["summary"]["not_checked_count"] == 2
    assert state["summary"]["passed_episode_count"] == 1
    assert any(e["event"] == "injected_wait" for e in runtime.read_events(state["run_id"], runs))


def test_missing_video_and_nan_are_different_node_outcomes(tiny_dataset, tmp_path):
    (tiny_dataset / f"videos/chunk-000/{CAMERA}/episode_000000.mp4").unlink()
    path = tiny_dataset / "data/chunk-000/episode_000001.parquet"
    table = pd.read_parquet(path)
    table.at[2, "action"] = np.array([np.nan] * 6)
    table.to_parquet(path)
    state, runs = launch(tiny_dataset, tmp_path)
    failed = next(n for n in state["nodes"] if n["status"] == "failed")
    assert (failed["node_id"], failed["episode_index"]) == ("video_decode", 0)
    assert "traceback" in failed["diagnostics"]
    check = next(n for n in state["nodes"] if n["node_id"] == "table_checks" and n["episode_index"] == 1)
    assert check["status"] == "completed" and check["data_issue_count"] == 1
    assert state["summary"]["data_issue_count"] == 1
    assert state["summary"]["load_failure_count"] == 1


def test_invalid_metadata_retains_history_and_chinese_diagnostics(tiny_dataset, tmp_path):
    (tiny_dataset / "meta/info.json").write_text("{broken", encoding="utf-8")
    state, runs = launch(tiny_dataset, tmp_path)
    assert state["status"] == "failed" and state["report_path"] is None
    assert state["nodes"][0]["status"] == "failed"
    assert "JSON" in state["nodes"][0]["message"]
    assert "traceback" in state["nodes"][0]["diagnostics"]
    assert runtime.list_runs(runs)[0]["run_id"] == state["run_id"]


def test_report_write_failure_does_not_erase_checks(tiny_dataset, tmp_path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("intentional report write failure", encoding="utf-8")
    state, runs = launch(tiny_dataset, tmp_path, output_dir=str(blocked))
    assert state["status"] == "failed"
    assert state["nodes"][-1]["status"] == "failed"
    assert all(n["status"] == "completed" for n in state["nodes"][:-1])
    assert len(list((runs / state["run_id"] / "results").glob("*.json"))) == 2
    assert "traceback" in state["nodes"][-1]["diagnostics"]


def test_single_active_run_and_coordinator_death(tiny_dataset, tmp_path):
    runs = tmp_path / "runs"
    run_id = runtime.start_run({"data_root": str(tiny_dataset), "episode_indices": [0, 1],
                                "execution_demo": "video_timeout"}, runs)
    with pytest.raises(RuntimeError, match="已有处理运行"):
        runtime.start_run({"data_root": str(tiny_dataset)}, runs)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        state = runtime.get_run(run_id, runs)
        if state.get("worker_pid"):
            break
        time.sleep(0.05)
    else:
        pytest.fail("coordinator did not start a worker")
    worker_pid = state["worker_pid"]
    runtime._terminate_identity(state["coordinator_pid"], state["coordinator_identity"])
    time.sleep(0.2)
    state = runtime.get_run(run_id, runs)
    assert state["status"] == "interrupted"
    assert any(n["status"] == "interrupted" for n in state["nodes"])
    for _ in range(20):
        if not runtime._alive(worker_pid):
            break
        time.sleep(0.05)
    assert not runtime._alive(worker_pid)


@pytest.mark.parametrize("exit_code", [0, 23])
def test_supervisor_records_abrupt_worker_exit(tmp_path, exit_code):
    directory = tmp_path / "test-abrupt-worker-exit"
    directory.mkdir()
    state = {"run_id": directory.name, "started_at": runtime.utc_now(), "request": {"timeouts": {"default": 5}, "execution_demo": "none"},
             "nodes": [runtime._node("table_read", 0)]}
    assert not runtime._supervise(directory, state, "episode", 0,
                                  command=[sys.executable, "-c", f"import os; os._exit({exit_code})"])
    assert state["nodes"][0]["status"] == "failed"
    result = json.loads((directory / "results/episode_0.json").read_text(encoding="utf-8"))
    assert result["status"] == "load_failed"
    assert result["issues"][0]["node_id"] == "table_read"
    assert state["nodes"][0]["exit_code"] == exit_code


def test_event_reader_preserves_prior_logs_after_incomplete_utf8_append(tmp_path):
    directory = tmp_path / "explicit-incomplete-event-fixture"
    directory.mkdir()
    runtime._event(directory, "table_checks", "completed", "已完成中文检查", 0)
    with (directory / "events.jsonl").open("ab") as stream:
        stream.write(b'{"message":"\xe4\xb8')  # interrupted inside a Chinese UTF-8 character
    events = runtime.read_events(directory.name, tmp_path)
    assert len(events) == 1 and events[0]["message"] == "已完成中文检查"


def test_concurrent_starters_cannot_create_two_active_runs(tiny_dataset, tmp_path):
    runs = tmp_path / "parallel-launch-runs"
    code = """
import sys
from robodata.runtime import start_run
try:
    print(start_run({'data_root': sys.argv[1], 'episode_indices': [0,1], 'execution_demo': 'video_timeout'}, sys.argv[2]), flush=True)
except RuntimeError:
    sys.exit(3)
"""
    children = [subprocess.Popen([sys.executable, "-c", code, str(tiny_dataset), str(runs)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    output = [p.communicate(timeout=20) for p in children]
    assert sorted(p.returncode for p in children) == [0, 3], output
    accepted = next(stdout.decode().strip() for process, (stdout, stderr) in zip(children, output) if process.returncode == 0)
    state = runtime.wait_run(accepted, runs, 60)
    assert state["status"] == "completed_with_errors"
    assert len(runtime.list_runs(runs)) == 1


TREE_WORKER = """
import subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(30)
"""


def test_timeout_terminates_worker_and_encoder_descendant(tmp_path):
    directory = tmp_path / "explicit-tree-timeout-fixture"
    directory.mkdir()
    child_pid_file = directory / "encoder.pid"
    state = {"run_id": directory.name, "started_at": runtime.utc_now(),
             "request": {"timeouts": {"default": 1.5}, "execution_demo": "none"},
             "nodes": [runtime._node("format_write")]}
    assert not runtime.run_command_step(directory, state, "format_write",
                                        [sys.executable, "-c", TREE_WORKER, str(child_pid_file)])
    child_pid = int(child_pid_file.read_text())
    assert state["nodes"][0]["status"] == "timed_out"
    for _ in range(40):
        if not runtime._alive(child_pid):
            break
        time.sleep(0.05)
    assert not runtime._alive(child_pid), "encoder descendant survived timeout"


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows Job Object automatic parent-death guarantee")
def test_coordinator_death_os_reaps_worker_and_encoder_without_ui_poll(tmp_path):
    directory = tmp_path / "explicit-tree-coordinator-death-fixture"
    directory.mkdir()
    child_pid_file = directory / "encoder.pid"
    coordinator_code = """
import sys
from pathlib import Path
from robodata import runtime
directory = Path(sys.argv[1])
state = {'run_id': directory.name, 'started_at': runtime.utc_now(),
         'request': {'timeouts': {'default': 30}, 'execution_demo': 'none'},
         'nodes': [runtime._node('format_write')]}
runtime.run_command_step(directory, state, 'format_write', [sys.executable, '-c', sys.argv[2], str(directory / 'encoder.pid')])
"""
    coordinator = subprocess.Popen([sys.executable, "-c", coordinator_code, str(directory), TREE_WORKER],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while not child_pid_file.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert child_pid_file.is_file()
        child_pid = int(child_pid_file.read_text())
        state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
        worker_pid = state["worker_pid"]
        assert runtime._alive(child_pid) and runtime._alive(worker_pid)
        coordinator.kill()
        coordinator.wait(timeout=10)
        # No get_run call, no explicit worker kill, no worker watchdog in this fixture.
        for _ in range(50):
            if not runtime._alive(child_pid) and not runtime._alive(worker_pid):
                break
            time.sleep(0.05)
        assert not runtime._alive(child_pid) and not runtime._alive(worker_pid)
    finally:
        if coordinator.poll() is None:
            coordinator.kill()
            coordinator.wait(timeout=10)
def test_checkpoint_read_retries_transient_windows_sharing_error(tmp_path, monkeypatch):
    from pathlib import Path
    from robodata.runtime import _read_json
    target = tmp_path / 'state.json'
    target.write_text('{"status":"completed"}', encoding='utf-8')
    original = Path.read_text
    attempts = []
    def busy_then_read(path, *args, **kwargs):
        if path == target:
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError('simulated short sharing violation')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', busy_then_read)
    assert _read_json(target)['status'] == 'completed'
    assert len(attempts) == 3
