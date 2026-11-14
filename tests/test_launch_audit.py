"""商业化上线级审计回归套件。

覆盖范围：
  A. 状态持久化与「离开一小时回来回退」复现
  B. 负向 / 边界 / 异常输入
  C. 并发一致性与幂等
  D. 性能与压力
  E. SSE 生命周期与资源

所有用例使用隔离的临时 business / runs 根目录，不读写真实 work/ 数据。
"""
from __future__ import annotations

import json
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from robodata_business import BatchStore, ConflictError
from robodata.web import create_app

FINGERPRINT = "a" * 64
ABS_WIN = r"C:\private\robot-data"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    business = tmp_path / "business"
    runs = tmp_path / "runs"
    business.mkdir()
    runs.mkdir()
    monkeypatch.setenv("ROBODATA_BUSINESS_ROOT", str(business))
    monkeypatch.setenv("ROBODATA_RUNS_ROOT", str(runs))
    return SimpleNamespace(root=tmp_path, business=business, runs=runs)


def _source():
    # 故意塞入多个绝对本机路径，验证 /api 响应不会把它们回显给浏览器。
    return {"kind": "so100", "input_fingerprint": FINGERPRINT,
            "data_root": ABS_WIN, "managed_path": ABS_WIN + r"\managed",
            "manifest_path": ABS_WIN + r"\manifest.json",
            "required_rules": ["required_fields"]}


def _quality_report(count, fingerprint=FINGERPRINT):
    return {"input_fingerprint": fingerprint,
            "episodes": [{"episode_index": i, "status": "passed", "row_count": 120,
                          "coverage": [{"rule": "required_fields", "status": "performed"}],
                          "issues": []} for i in range(count)]}


def make_batch(root, count=3, submit=True):
    """走完整业务门禁：导入 → 质检 → 筛选确认 → 标注 → 提交审核。"""
    store = BatchStore(root)
    batch = store.create_batch("上线审计批次", _source(),
                               [{"episode_index": i, "row_count": 120} for i in range(count)],
                               run_id="audit_import_run")
    batch = store.apply_quality_report(batch["batch_id"], _quality_report(count),
                                       run_id="audit_quality_run", expected_revision=batch["revision"])
    batch = store.confirm_cleaning(batch["batch_id"], expected_revision=batch["revision"])
    batch = store.save_annotation(batch["batch_id"], 0, task="拾取糖果", outcome="success",
                                  tags=["验收"], notes="审计样例", expected_revision=batch["revision"])
    if submit:
        batch = store.submit_annotation(batch["batch_id"], 0, expected_revision=batch["revision"])
    return store, batch


def _terminal_run(root, run_id="20260101T000000_aaaaaaaaaaaa", registration="registered"):
    directory = root / run_id
    directory.mkdir(parents=True, exist_ok=True)
    state = {"run_id": run_id, "status": "completed", "started_at": "2026-01-01T00:00:00+00:00",
             "completed_at": "2026-01-01T00:01:00+00:00", "request": {"operation": "quality"},
             "nodes": [{"node_id": "quality", "label": "批次基础质检", "status": "completed"}],
             "message": "完成", "artifacts": {}, "coordinator_pid": None}
    (directory / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (directory / "events.jsonl").write_text("", encoding="utf-8")
    (directory / "registration.json").write_text(json.dumps({"status": registration}), encoding="utf-8")
    return directory


def _absolute_paths(value, prefix=""):
    hits = []
    if isinstance(value, dict):
        for key, item in value.items():
            hits.extend(_absolute_paths(item, prefix + "/" + str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value[:5]):
            hits.extend(_absolute_paths(item, prefix + f"[{index}]"))
    elif isinstance(value, str) and (re.match(r"^[A-Za-z]:[\\/]", value)
                                     or value.startswith("/Users/") or value.startswith("/home/")):
        hits.append((prefix, value))
    return hits


# --------------------------------------------------------------------------
# A. 状态持久化与回退复现
# --------------------------------------------------------------------------

def test_backend_state_is_durable_across_new_app_instance(isolated):
    """后端是磁盘原子快照：重建 app / 重建 store 不应丢失已提交审核。"""
    store, batch = make_batch(isolated.business)
    assert batch["episodes"][0]["review"]["status"] == "submitted"
    fresh = TestClient(create_app()).get(f"/api/batches/{batch['batch_id']}").json()
    assert fresh["episodes"][0]["review"]["status"] == "submitted"
    assert fresh["cleaning_confirmed"] is True
    assert fresh["revision"] == batch["revision"]
    assert BatchStore(isolated.business).get_batch(batch["batch_id"])["revision"] == batch["revision"]


def test_api_does_not_leak_local_absolute_paths(isolated):
    """P1：响应中任何字段都不得回显本机绝对路径。"""
    _, batch = make_batch(isolated.business)
    # 模拟真实历史记录里带路径的输出与交付物
    store = BatchStore(isolated.business)
    state = store._read(batch["batch_id"])
    state["outputs"] = [{"output_id": "output_x", "content_version": state["content_version"],
                         "artifact_path": ABS_WIN + r"\out",
                         "validation": {"quality_report_path": ABS_WIN + r"\q.json"}}]
    state["deliveries"] = [{"delivery_id": "delivery_x", "content_version": state["content_version"],
                            "artifact_path": ABS_WIN + r"\out",
                            "zip_path": ABS_WIN + r"\bundle.zip", "source": _source()}]
    store._write(state)
    response = TestClient(create_app()).get(f"/api/batches/{batch['batch_id']}")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert _absolute_paths(payload) == [], f"泄漏字段：{_absolute_paths(payload)}"


def test_characterization_quality_rerun_silently_resets_submitted_review(isolated):
    """【已确认的业务级回退】重跑质检会把 submitted/approved 全部打回 draft。

    这不是磁盘丢失，而是 apply_quality_report 的静默失效逻辑。长时间后台运行
    （ALOHA 质检超时上限 1800s）结束后回写，用户回来就会看到「回到导入阶段」。
    """
    store, batch = make_batch(isolated.business)
    assert batch["episodes"][0]["review"]["status"] == "submitted"
    after = store.apply_quality_report(batch["batch_id"], _quality_report(3),
                                       run_id="quality_rerun_late", expected_revision=batch["revision"])
    assert after["episodes"][0]["review"]["status"] == "draft"
    assert after["cleaning_confirmed"] is False
    assert all(e["disposition"] == "keep" for e in after["episodes"])


def test_api_blocks_quality_rerun_without_explicit_acknowledgement(isolated, monkeypatch):
    """P0 修复：存在已提交/已通过审核时，重跑质检必须先得到明确确认。"""
    _, batch = make_batch(isolated.business)
    monkeypatch.setattr("robodata.web.business.start_batch_quality", lambda *a, **k: "audit_run_fake")
    client = TestClient(create_app())
    payload = {"operation": "quality", "batch_id": batch["batch_id"], "revision": batch["revision"]}
    denied = client.post("/api/runs", json=payload)
    assert denied.status_code == 409, denied.text
    assert "审核" in denied.json()["detail"]
    accepted = client.post("/api/runs", json={**payload, "invalidate_reviews": True})
    assert accepted.status_code == 202, accepted.text


def test_reconcile_cannot_overwrite_newer_revision(isolated):
    """P0 安全项：迟到的运行回写不得覆盖用户期间产生的新版本。"""
    from robodata import business
    from robodata.runtime import _atomic_json

    store, batch = make_batch(isolated.business)
    run_id = "20260101T000001_bbbbbbbbbbbb"
    directory = _terminal_run(isolated.runs, run_id, "pending")
    request = {"business_root": str(isolated.business), "operation": "quality",
               "batch_id": batch["batch_id"], "expected_revision": batch["revision"] - 1,
               "source": _source(), "source_kind": "so100"}
    _atomic_json(directory / "requests" / "registration.json", request)
    (directory / "registration.json").write_text(json.dumps({
        "status": "pending", "stage": "registration", "registration_operation": "quality",
        "request_path": str(directory / "requests" / "registration.json")}), encoding="utf-8")
    result = business.reconcile_run(run_id)
    assert result["status"] in {"stale", "failed"}
    assert store.get_batch(batch["batch_id"])["episodes"][0]["review"]["status"] == "submitted"


# --------------------------------------------------------------------------
# B. 负向 / 边界 / 异常输入
# --------------------------------------------------------------------------

@pytest.mark.parametrize("batch_id", ["", "not-a-batch", "batch_" + "z" * 32,
                                      "batch_" + "a" * 31 + "../evil", "../../windows/win.ini"])
def test_invalid_batch_ids_are_rejected(isolated, batch_id):
    response = TestClient(create_app()).get(f"/api/batches/{batch_id}")
    assert response.status_code in (404, 422), (batch_id, response.status_code)


def test_unknown_run_and_traversal_run_ids_are_rejected(isolated):
    client = TestClient(create_app())
    for run_id in ["missing-run", "../../etc", "..%2f..%2fetc", "a" * 300]:
        response = client.get(f"/api/runs/{run_id}")
        assert response.status_code in (404, 422), (run_id, response.status_code)


@pytest.mark.parametrize("payload", [
    {"operation": "unknown"},
    {"operation": "import", "source_kind": "so100", "label": "缺路径"},
    {"operation": "import", "source_kind": "hdf5", "label": ""},
    {"operation": "quality", "batch_id": "batch_" + "a" * 32},
    {"operation": "delivery", "batch_id": "batch_" + "a" * 32, "revision": 1},
    {"operation": "verify_delivery"},
    {"operation": "quality", "batch_id": "batch_" + "a" * 32, "revision": "not-int"},
])
def test_malformed_run_requests_are_rejected(isolated, payload):
    response = TestClient(create_app()).post("/api/runs", json=payload)
    assert response.status_code == 422, (payload, response.text)


def test_nonlocal_origin_and_untrusted_host_are_rejected(isolated):
    client = TestClient(create_app())
    assert client.get("/api/health", headers={"Origin": "https://evil.invalid"}).status_code == 403
    assert client.get("/api/health", headers={"Host": "evil.invalid"}).status_code in (400, 403)


def test_stale_revision_writes_are_rejected(isolated):
    _, batch = make_batch(isolated.business)
    client = TestClient(create_app())
    url = f"/api/batches/{batch['batch_id']}/episodes/0/annotation"
    response = client.patch(url, json={"revision": batch["revision"] - 1, "task": "旧版本",
                                       "outcome": "success", "tags": [], "notes": ""})
    assert response.status_code == 409
    assert "过期" in response.json()["detail"]


def test_duplicate_submit_and_invalid_review_are_rejected(isolated):
    _, batch = make_batch(isolated.business)
    client = TestClient(create_app())
    submit = f"/api/batches/{batch['batch_id']}/episodes/0/annotation/submit"
    assert client.post(submit, json={"revision": batch["revision"]}).status_code == 409
    review = f"/api/batches/{batch['batch_id']}/episodes/1/review"
    assert client.post(review, json={"revision": batch["revision"], "approve": True}).status_code == 409


def test_invalid_annotation_payloads_are_rejected(isolated):
    _, batch = make_batch(isolated.business)
    client = TestClient(create_app())
    url = f"/api/batches/{batch['batch_id']}/episodes/0/annotation"
    for body in ({"revision": batch["revision"], "task": "x", "outcome": "made-up"},
                 {"revision": batch["revision"], "task": "x", "outcome": "success", "tags": [123]},
                 {"revision": batch["revision"], "task": 123, "outcome": "success"}):
        assert client.patch(url, json=body).status_code in (409, 422), body


def test_upload_rejects_wrong_extension(isolated):
    client = TestClient(create_app())
    response = client.post("/api/imports/upload", data={"label": "测试"},
                           files={"file": ("payload.txt", b"not-hdf5", "text/plain")})
    assert response.status_code == 422


def test_damaged_batch_is_isolated_not_fatal(isolated):
    _, batch = make_batch(isolated.business)
    (isolated.business / ("batch_" + "b" * 32 + ".json")).write_text("{ broken", encoding="utf-8")
    response = TestClient(create_app()).get("/api/batches")
    assert response.status_code == 200
    body = response.json()
    assert [item["batch_id"] for item in body["batches"]] == [batch["batch_id"]]
    assert len(body["errors"]) == 1


def test_camera_name_cannot_escape_declared_streams(isolated):
    """camera_name:path 路由必须只在 episode 声明过的相机字典里查找。"""
    from dataclasses import dataclass
    from robodata.video import episode_camera

    @dataclass
    class FakeEpisode:
        camera_paths: dict
        camera_segments: dict
        source: dict
        video_path: Path
        video_start_time: float
        video_end_time: float | None

    episode = FakeEpisode(
        camera_paths={"observation.images.laptop": Path(ABS_WIN) / "cam.mp4"},
        camera_segments={}, source={"camera": "observation.images.laptop"},
        video_path=Path(ABS_WIN) / "cam.mp4", video_start_time=0.0, video_end_time=None)
    for candidate in ["../../../../Windows/win.ini", "..", "C:\\Windows\\win.ini", "laptop/../../../etc"]:
        with pytest.raises(KeyError):
            episode_camera(episode, candidate)


# --------------------------------------------------------------------------
# C. 并发一致性与幂等
# --------------------------------------------------------------------------

def test_concurrent_writes_with_same_revision_only_one_wins(isolated):
    store, batch = make_batch(isolated.business)

    def worker(index):
        try:
            store.save_annotation(batch["batch_id"], 0, task=f"并发{index}", outcome="success",
                                  tags=[], notes="", expected_revision=batch["revision"])
            return "ok"
        except ConflictError:
            return "conflict"
        except Exception as exc:  # 锁竞争不应变成 500
            return f"error:{type(exc).__name__}"

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(worker, range(12)))
    assert results.count("ok") == 1, results
    assert all(item == "ok" or item == "conflict" for item in results), results
    assert store.get_batch(batch["batch_id"])["revision"] == batch["revision"] + 1


def test_quality_registration_is_idempotent(isolated):
    store, batch = make_batch(isolated.business)
    # 重放相同 run_id 不应产生第二次内容版本推进
    replay = store.apply_quality_report(batch["batch_id"], _quality_report(3),
                                        run_id="audit_quality_run", expected_revision=batch["revision"] - 4)
    assert replay["revision"] == batch["revision"]
    assert replay["applied_runs"]["apply_quality_report:audit_quality_run"]["run_id"] == "audit_quality_run"


# --------------------------------------------------------------------------
# D. 性能与压力
# --------------------------------------------------------------------------

def test_concurrent_read_latency(isolated):
    make_batch(isolated.business, count=50)

    def one(_):
        client = TestClient(create_app())
        start = time.perf_counter()
        response = client.get("/api/batches")
        return time.perf_counter() - start, response.status_code

    with ThreadPoolExecutor(max_workers=16) as pool:
        samples = list(pool.map(one, range(80)))
    assert all(code == 200 for _, code in samples)
    latencies = sorted(item[0] for item in samples)
    p95 = latencies[int(len(latencies) * 0.95) - 1]
    assert p95 < 2.0, f"p95={p95:.3f}s"
    print(f"\n[PERF] /api/batches 并发读 n=80 p50={statistics.median(latencies):.4f}s p95={p95:.4f}s")


def test_large_batch_read_latency(isolated):
    store = BatchStore(isolated.business)
    batch = store.create_batch("规模批次", _source(),
                               [{"episode_index": i, "row_count": 100} for i in range(5000)],
                               run_id="bulk")
    start = time.perf_counter()
    loaded = store.get_batch(batch["batch_id"])
    elapsed = time.perf_counter() - start
    assert loaded["total_episodes"] == 5000
    assert elapsed < 1.5, f"get_batch(5000) = {elapsed:.3f}s"
    print(f"\n[PERF] 5000 episode 读取 {elapsed:.4f}s")


def test_run_list_stress(isolated):
    for index in range(300):
        _terminal_run(isolated.runs, f"20260101T{index // 60:02d}{index % 60:02d}00_{index:012d}")
    start = time.perf_counter()
    response = TestClient(create_app()).get("/api/runs")
    elapsed = time.perf_counter() - start
    assert response.status_code == 200
    assert len(response.json()["runs"]) == 300
    assert elapsed < 5.0, f"/api/runs(300) = {elapsed:.3f}s"
    print(f"\n[PERF] /api/runs 300 条 {elapsed:.4f}s")


def test_repeated_reconcile_reads_are_stable(isolated):
    """SSE 断线重连会反复触发 _load_state；终态读不得改写业务状态。"""
    _, batch = make_batch(isolated.business)
    run_id = "20260101T000002_cccccccccccc"
    _terminal_run(isolated.runs, run_id, "registered")
    client = TestClient(create_app())
    start = time.perf_counter()
    for _ in range(60):
        assert client.get(f"/api/runs/{run_id}").status_code == 200
    elapsed = time.perf_counter() - start
    assert BatchStore(isolated.business).get_batch(batch["batch_id"])["revision"] == batch["revision"]
    assert elapsed < 5.0, f"60 次终态读 = {elapsed:.3f}s"
    print(f"\n[PERF] 60 次终态 reconcile 读 {elapsed:.4f}s")


# --------------------------------------------------------------------------
# E. SSE 生命周期
# --------------------------------------------------------------------------

def test_sse_terminal_stream_emits_done_event(isolated):
    """P0 修复：终态必须显式通知客户端关闭 EventSource，避免无限重连。"""
    run_id = "20260101T000003_dddddddddddd"
    _terminal_run(isolated.runs, run_id, "registered")
    client = TestClient(create_app())
    with client.stream("GET", f"/api/runs/{run_id}/events") as response:
        body = "".join(response.iter_text())
    assert response.status_code == 200
    assert "event: done" in body, body[:400]


def test_reading_runs_never_triggers_reconciliation(isolated):
    """P0 根因护栏：GET/SSE 属于读路径，绝不能改写业务状态或重试失败登记。"""
    _, batch = make_batch(isolated.business)
    run_id = "20260101T000004_eeeeeeeeeeee"
    directory = _terminal_run(isolated.runs, run_id, "pending")
    before = (directory / "registration.json").read_bytes()
    client = TestClient(create_app())
    for _ in range(25):
        assert client.get(f"/api/runs/{run_id}").status_code == 200
    assert (directory / "registration.json").read_bytes() == before
    assert BatchStore(isolated.business).get_batch(batch["batch_id"])["revision"] == batch["revision"]


def test_explicit_recover_endpoint_replaces_read_side_effect(isolated):
    """恢复登记必须走显式 POST，且重复调用保持幂等。"""
    run_id = "20260101T000005_ffffffffffff"
    _terminal_run(isolated.runs, run_id, "registered")
    client = TestClient(create_app())
    first = client.post(f"/api/runs/{run_id}/recover")
    second = client.post(f"/api/runs/{run_id}/recover")
    assert first.status_code == 200
    assert first.json()["status"] == "registered"
    assert second.status_code == 429  # 连击被节流，避免重复回写
    assert second.headers.get("Retry-After") == "2"


def test_inline_paths_and_tracebacks_are_scrubbed(isolated):
    """错误文本里内嵌的本机路径与 traceback 字段也不得回显。"""
    from robodata.web import _public_value

    payload = _public_value({
        "message": f"读取 {ABS_WIN}\\dataset\\a.h5 失败",
        "traceback": "Traceback ... " + ABS_WIN,
        "detail": "一切正常",
        "source": {"repo_id": "jmrog/so100_sweet_pick", "data_root": ABS_WIN},
        "archive_path": ABS_WIN + r"\bundle.zip",
    })
    assert "C:\\private" not in json.dumps(payload, ensure_ascii=False)
    assert "traceback" not in payload and "archive_path" not in payload
    assert payload["source"]["repo_id"] == "jmrog/so100_sweet_pick"
    assert payload["message"] == "读取 [本机路径已省略] 失败"
    assert payload["detail"] == "一切正常"


def test_scrubbing_keeps_urls_legit_filenames_and_covers_unc_posix(isolated):
    """脱敏不能破坏 URL 与交付清单文件名；UNC / /opt 路径同样要遮住。"""
    from robodata.web import _public_value

    payload = _public_value({
        "source": {"url": "https://huggingface.co/datasets/jmrog/so100_sweet_pick",
                   "repo_id": "jmrog/so100_sweet_pick"},
        "files": [{"path": "data/chunk-000/file-000.parquet", "size": 120},
                  {"path": "meta/info.json", "size": 8}],
        "detail": f"读取 {ABS_WIN}\\dataset\\a.h5 失败",
        "unc": r"\\NAS-01\share\robot\cam.mp4",
        "posix": "/opt/robodata/data/cam.mp4",
    })
    text = json.dumps(payload, ensure_ascii=False)
    assert "https://huggingface.co/datasets/jmrog/so100_sweet_pick" in text
    assert "data/chunk-000/file-000.parquet" in text
    assert "C:\\private" not in text
    assert "NAS-01" not in text
    assert "/opt/robodata" not in text
    assert payload["files"][0]["path"] == "data/chunk-000/file-000.parquet"


def test_dot_run_ids_are_rejected(isolated):
    """HTTP 客户端会先把 '.' / '..' 规范化掉，所以直接校验路径构造层。"""
    from robodata.runtime import _run_dir

    for run_id in ["..", ".", ".hidden", "../escape", "a/b", "a" * 200]:
        with pytest.raises(ValueError):
            _run_dir(run_id, isolated.runs)
    assert _run_dir("20260101T000000_aaaaaaaaaaaa", isolated.runs).parent == isolated.runs.resolve()


def test_recover_quality_registration_requires_explicit_acknowledgement(isolated):
    """T-P0-2：即便走显式 POST /recover，也不能让「恢复」静默打回已提交审核。"""
    _, batch = make_batch(isolated.business)
    run_id = "20260101T000006_1234567890ab"
    directory = _terminal_run(isolated.runs, run_id, "pending")
    registration = json.loads((directory / "registration.json").read_text(encoding="utf-8"))
    registration["registration_operation"] = "quality"
    (directory / "registration.json").write_text(json.dumps(registration), encoding="utf-8")
    before = (directory / "registration.json").read_bytes()
    client = TestClient(create_app())

    blocked = client.post(f"/api/runs/{run_id}/recover")
    assert blocked.status_code == 409
    assert "审核" in blocked.json()["detail"]
    # 门禁属于纯读前置检查：登记文件与业务快照都必须原封不动。
    assert (directory / "registration.json").read_bytes() == before
    fresh = BatchStore(isolated.business).get_batch(batch["batch_id"])
    assert fresh["revision"] == batch["revision"]
    assert fresh["cleaning_confirmed"] is True
    assert fresh["episodes"][0]["review"]["status"] == "submitted"

    # 显式确认后门禁放行；测试夹具没有完整处理证据，后端按既有规则给出失败/过期结果，
    # 但绝不能再返回「请显式确认」这条门禁提示，也不应占用节流额度。
    accepted = client.post(f"/api/runs/{run_id}/recover", json={"invalidate_reviews": True})
    assert "请显式确认后再提交" not in str(accepted.json().get("detail", ""))
    assert accepted.status_code != 429


def test_quality_rerun_guard_discloses_output_and_delivery_impact(isolated):
    """拒绝重跑质检时，必须把产物失效的后果讲清楚，而不是只说「审核会失效」。"""
    _, batch = make_batch(isolated.business)
    client = TestClient(create_app())
    blocked = client.post("/api/runs", json={"operation": "quality",
                                             "batch_id": batch["batch_id"],
                                             "revision": batch["revision"]})
    assert blocked.status_code == 409
    detail = blocked.json()["detail"]
    assert "转换产物" in detail and "交付包" in detail


def test_recover_gate_falls_back_to_request_operation(isolated):
    """历史登记缺 registration_operation 时，门禁必须回退到 request.json 判定。"""
    _, batch = make_batch(isolated.business)
    run_id = "20260101T000007_abcdef012345"
    directory = _terminal_run(isolated.runs, run_id, "pending")
    (directory / "registration.json").write_text(json.dumps({
        "status": "pending", "stage": "registration",
        "request_path": str(directory / "request.json")}), encoding="utf-8")
    (directory / "request.json").write_text(json.dumps({"operation": "quality"}), encoding="utf-8")
    before = (directory / "registration.json").read_bytes()
    client = TestClient(create_app())

    blocked = client.post(f"/api/runs/{run_id}/recover")
    assert blocked.status_code == 409
    assert (directory / "registration.json").read_bytes() == before
    assert BatchStore(isolated.business).get_batch(batch["batch_id"])["episodes"][0]["review"]["status"] == "submitted"


def test_disposition_change_requires_explicit_acknowledgement(isolated):
    """改筛选决定会把全部保留任务的审核与产物一起失效，不能静默发生。"""
    _, batch = make_batch(isolated.business)
    client = TestClient(create_app())
    path = f"/api/batches/{batch['batch_id']}/episodes/0/disposition"
    payload = {"revision": batch["revision"], "disposition": "quarantine", "reason": "复核"}

    blocked = client.patch(path, json=payload)
    assert blocked.status_code == 409
    detail = blocked.json()["detail"]
    assert "审核" in detail and "交付包" in detail
    fresh = BatchStore(isolated.business).get_batch(batch["batch_id"])
    assert fresh["revision"] == batch["revision"]
    assert fresh["episodes"][0]["review"]["status"] == "submitted"

    accepted = client.patch(path, json={**payload, "invalidate_reviews": True})
    assert accepted.status_code == 200
    changed = BatchStore(isolated.business).get_batch(batch["batch_id"])
    assert changed["revision"] == batch["revision"] + 1
    assert changed["cleaning_confirmed"] is False


def test_legacy_streamlit_read_paths_are_purely_read_only(isolated):
    """旧 Streamlit 入口每次 rerun（含 1s 轮询片段）都不得自动回写业务登记。"""
    import inspect
    from robodata import ui_business, ui_runs

    # Streamlit 的 st.session_state 需 AppTest 上下文，这里用源码护栏防止
    # 有人在 sync_last_run 里重新引入自动 reconcile（与 SSE 无限重连同源）。
    assert inspect.signature(ui_runs.load_run).parameters["mark_interrupted"].default is False
    assert "service().reconcile_run(" not in inspect.getsource(ui_business.sync_last_run)


def test_ui_load_run_default_does_not_persist_interrupted_view(isolated):
    """读路径默认只读：即使派生出 interrupted 视图也不能改写 state.json。"""
    from robodata import ui_runs

    run_id = "20260101T000008_deadbeef0001"
    directory = isolated.runs / run_id
    directory.mkdir()
    state = {"run_id": run_id, "status": "running", "started_at": "2026-01-01T00:00:00+00:00",
             "request": {"operation": "quality"}, "nodes": [], "message": "运行中",
             "artifacts": {}, "coordinator_pid": 999999}
    (directory / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (directory / "events.jsonl").write_text("", encoding="utf-8")
    before = (directory / "state.json").read_bytes()

    view = ui_runs.load_run(run_id)
    assert view["status"] == "interrupted"
    assert (directory / "state.json").read_bytes() == before
