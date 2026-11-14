"""Loopback-only JSON bridge and static frontend host for the workbench."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from robodata_business import ConflictError

from . import business, ui_runs
from .config import PROJECT_ROOT
from .dataset import load_episode, load_metadata
from .errors import explain_error
from .importing import save_upload
from .quality import json_safe
from .video import inspect_episode_camera


FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
ALLOWED_ORIGINS = {
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
    "http://localhost:8765",
    "http://127.0.0.1:8765",
}
# 明确的本机路径字段：整键不回显。故意不含通用的 path / file —— 交付清单里
# 有大量 `{"path": "data/..."}` 形式的文件名，属于需要展示的业务证据。
PRIVATE_KEYS = {
    "input_path", "archive_path", "business_root", "data_root", "hdf5_path", "output_path",
    "managed_path", "manifest_path", "artifact_path", "zip_path", "report_path",
    "basic_report_path", "quality_report_path", "request_path", "dataset_path",
    "v21_work_path", "managed_dir", "staging_path", "preview_root", "unpacked_path", "log_path",
    "traceback", "exception_message", "stderr", "stdout",
}
# 人类可读文本：只对这些键做「内嵌路径」脱敏，避免改动任意业务字段。
TEXT_KEYS = {"detail", "message", "message_zh", "error", "original_message", "diagnostics",
             "reason", "selection_reason", "notes", "task", "limitations", "mapping_claim",
             "diagnostic", "hint", "warning"}
_POSIX_ROOTS = r"(?:Users|home|root|tmp|var|mnt|opt|srv|data|workspace|Volumes|private)"
# 整串就是本机路径（含 UNC 与 /opt 等）：直接退化成文件名。
_LOCAL_PATH = re.compile(
    rf"^(?:[A-Za-z]:[\\/]|\\\\[^\s\\]+[\\/]|/{_POSIX_ROOTS}/)")
# 文本内嵌路径：前置 (?<![A-Za-z0-9]) 专门避免把 https:// 中的 "p:/" 当成盘符。
_INLINE_PATH = re.compile(
    rf"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/][^\s\"'<>]*|\\\\[^\s\"'<>]+|/{_POSIX_ROOTS}/[^\s\"'<>]*)")


def _safe_path_name(value: str) -> str:
    return Path(value.replace("\\", "/")).name or "本机文件"


def _scrub_inline(value: str) -> str:
    return _INLINE_PATH.sub("[本机路径已省略]", value)


# 恢复登记的进程内最小节流（本机单用户，不需要分布式限流）。
_recover_guard = threading.Lock()
_recover_last: dict[str, float] = {}


def _pending_registration(run_id: str) -> dict:
    """只读探测待恢复登记的类型，供破坏性操作的事前确认门禁使用。

    这里绝不调用 reconcile_run：探测本身属于读路径，不能产生任何业务回写。
    """
    from .runtime import _read_json, _run_dir
    try:
        directory = _run_dir(run_id, ui_runs.runs_root())
        pending = _read_json(directory / "registration.json", {})
        # 历史记录可能缺 registration_operation；用与 _register 完全相同的
        # 回退口径补齐，否则这类旧记录能无声绕过破坏性确认门禁。
        return {**pending, "registration_operation":
                business.registration_operation_of(directory) or pending.get("registration_operation")}
    except (ValueError, OSError, KeyError, TypeError):
        return {}


class RunRequest(BaseModel):
    operation: Literal["import", "quality", "final_quality", "conversion", "delivery", "verify_delivery"]
    # 重跑质检会把已提交 / 已通过的审核打回草稿，必须由调用方显式确认。
    invalidate_reviews: bool | None = None
    source_kind: Literal["hdf5", "so100", "aloha"] | None = None
    path: str | None = None
    label: str | None = None
    test_label: str | None = None
    batch_id: str | None = None
    revision: int | None = None
    output_id: str | None = None
    archive_path: str | None = None


class RecoverRequest(BaseModel):
    # 恢复「批次质检登记」会重置审核与产物，必须由调用方显式确认。
    invalidate_reviews: bool | None = None


class DispositionRequest(BaseModel):
    revision: int
    disposition: Literal["keep", "quarantine", "exclude"]
    reason: str = ""
    # 改筛选决定会重置全部保留任务的审核并使产物失效，必须由调用方显式确认。
    invalidate_reviews: bool | None = None


class AnnotationRequest(BaseModel):
    revision: int
    task: str = ""
    outcome: Literal["unlabeled", "success", "failure", "uncertain"] = "unlabeled"
    tags: list[str] = Field(default_factory=list)
    notes: str = ""


class ReviewRequest(BaseModel):
    revision: int
    approve: bool
    reason: str = ""


class RevisionRequest(BaseModel):
    revision: int


def _public_value(value, parent_key: str = ""):
    if isinstance(value, dict):
        return {str(key): _public_value(item, str(key)) for key, item in value.items()
                if str(key) not in PRIVATE_KEYS}
    if isinstance(value, (list, tuple)):
        return [_public_value(item, parent_key) for item in value]
    if isinstance(value, Path):
        return value.name
    if isinstance(value, str):
        # 未知键只要「整个值就是本机路径」也脱敏，作为兜底防线；
        # 其余业务字符串（URL、文件名、用户备注）保持原样。
        if _LOCAL_PATH.match(value):
            return _safe_path_name(value)
        if parent_key in TEXT_KEYS:
            return _scrub_inline(value)
    return json_safe(value)


def _public_batch(batch: dict) -> dict:
    value = copy.deepcopy(batch)
    source = value.get("source", {})
    for key in PRIVATE_KEYS:
        source.pop(key, None)
    return _public_value(value)


def _episode_root(batch: dict) -> Path:
    """HDF5 批次的可读取副本在 preview_root；SO-100 / ALOHA 在 data_root。"""
    source = batch.get("source", {})
    raw = source.get("preview_root") or source.get("data_root") or source.get("hdf5_path")
    if not raw:
        raise ValueError("批次没有可读取的源目录")
    return Path(raw)


def _store():
    return business.get_store()


def _start_request(request: RunRequest) -> str:
    op = request.operation
    if op == "import":
        if not request.label or not request.source_kind:
            raise ValueError("导入需要批次名称和数据源类型")
        if request.source_kind == "aloha":
            from .aloha import ALOHA_REPO_ID, ALOHA_REVISION
            path = request.path or f"pinned://{ALOHA_REPO_ID}@{ALOHA_REVISION}"
        else:
            path = request.path
        if not path:
            raise ValueError("请填写本机文件或目录路径")
        return business.start_import(path, request.source_kind, request.label, request.test_label)

    if not request.batch_id or request.revision is None:
        raise ValueError("批次操作需要批次编号和当前版本号")
    if op == "quality":
        return business.start_batch_quality(request.batch_id, request.revision)
    if op == "final_quality":
        return business.start_final_quality(request.batch_id, request.revision)
    if op == "conversion":
        return business.start_batch_conversion(request.batch_id, request.revision)
    if op == "delivery":
        if not request.output_id:
            raise ValueError("打包交付需要选择一个已验证输出")
        return business.start_delivery(request.batch_id, request.output_id, request.revision)
    if op == "verify_delivery":
        if not request.archive_path:
            raise ValueError("独立验包需要填写交付 ZIP 路径")
        return business.start_verify_delivery(request.archive_path)
    raise ValueError("不支持的运行操作")


def _load_state(run_id: str) -> dict:
    """只读运行快照。

    严禁在这里触发 reconcile：GET/SSE 会被浏览器无限重连反复调用，而
    reconcile 里的 apply_quality_report 会把已提交的审核静默打回草稿。
    恢复登记改由显式的 POST /api/runs/{id}/recover 提供。
    """
    try:
        return _public_value(ui_runs.load_run(run_id, mark_interrupted=False))
    except (KeyError, ValueError, OSError) as exc:
        raise HTTPException(status_code=404, detail=explain_error(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="运行记录暂时不可读，请稍后重试") from exc


def create_app() -> FastAPI:
    app = FastAPI(title="RoboData local workbench", version="0.9.0", docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def reject_nonlocal_origins(request, call_next):
        origin = request.headers.get("origin")
        if origin and origin not in ALLOWED_ORIGINS:
            return JSONResponse({"detail": "仅允许本机工作台来源访问"}, status_code=403)
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(ALLOWED_ORIGINS),
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Content-Type"],
    )

    @app.get("/api/health")
    def health():
        return {"status": "ok", "app": "RoboData Workbench", "version": "0.9.0", "scope": "loopback"}

    @app.get("/api/batches")
    def batches():
        result = business.safe_list_batches()
        return {"batches": [_public_batch(item) for item in result["batches"]],
                "errors": _public_value(result["errors"])}

    @app.get("/api/batches/{batch_id}")
    def batch_detail(batch_id: str):
        try:
            return _public_batch(_store().get_batch(batch_id))
        except (ValueError, OSError, KeyError) as exc:
            raise HTTPException(status_code=404, detail=explain_error(exc)) from exc

    @app.get("/api/batches/{batch_id}/episodes/{episode_index}")
    def episode_detail(batch_id: str, episode_index: int):
        try:
            batch = _store().get_batch(batch_id)
            root = _episode_root(batch)
            episode = load_episode(root, episode_index)
            table = episode.table
            stride = max(1, (len(table) + 1199) // 1200)
            rows = table.iloc[::stride]
            cameras = []
            for name, path in (episode.camera_paths or {}).items():
                segment = (episode.camera_segments or {}).get(name, {})
                cameras.append({"name": name, "video_url": f"/api/batches/{batch_id}/episodes/{episode_index}/video/{name}",
                                "from_timestamp": segment.get("from_timestamp", 0.0),
                                "to_timestamp": segment.get("to_timestamp"), "available": Path(path).is_file()})
            episode_record = next((item for item in batch["episodes"] if item["episode_index"] == episode_index), None)
            if episode_record is None:
                raise ValueError("批次中不存在该 episode")
            return {
                "episode_index": episode_index,
                "row_count": len(table),
                "fps": episode.fps,
                "state_names": episode.state_names,
                "action_names": episode.action_names,
                "cameras": cameras,
                "quality": _public_value(episode_record.get("quality", {})),
                "disposition": episode_record.get("disposition"),
                "selection_reason": episode_record.get("selection_reason", ""),
                "annotation": _public_value(episode_record.get("annotation", {})),
                "review": _public_value(episode_record.get("review", {})),
                "samples": _public_value(rows.to_dict(orient="records")),
            }
        except (ValueError, OSError, KeyError, IndexError) as exc:
            raise HTTPException(status_code=404, detail=explain_error(exc)) from exc

    @app.get("/api/batches/{batch_id}/episodes/{episode_index}/cameras/{camera_name:path}")
    def camera_detail(batch_id: str, episode_index: int, camera_name: str):
        try:
            batch = _store().get_batch(batch_id)
            root = _episode_root(batch)
            episode = load_episode(root, episode_index)
            result = inspect_episode_camera(episode, camera_name)
            segment = (episode.camera_segments or {}).get(camera_name, {})
            return {"name": camera_name, "fps": result.get("fps"), "frame_count": result.get("frame_count"),
                    "width": result.get("width"), "height": result.get("height"), "codec": result.get("codec"),
                    "from_timestamp": segment.get("from_timestamp", 0.0), "to_timestamp": segment.get("to_timestamp"),
                    "mapping_claim": "文件级时间映射检查；不代表硬件同步"}
        except (ValueError, OSError, KeyError, IndexError) as exc:
            raise HTTPException(status_code=404, detail=explain_error(exc)) from exc

    @app.get("/api/batches/{batch_id}/episodes/{episode_index}/video/{camera_name:path}")
    def camera_video(batch_id: str, episode_index: int, camera_name: str):
        try:
            batch = _store().get_batch(batch_id)
            root = _episode_root(batch)
            episode = load_episode(root, episode_index)
            path = (episode.camera_paths or {}).get(camera_name)
            if path is None or not Path(path).is_file():
                raise HTTPException(status_code=404, detail="该相机视频不存在")
            return FileResponse(Path(path), media_type="video/mp4", filename=Path(path).name, content_disposition_type="inline")
        except HTTPException:
            raise
        except (ValueError, OSError, KeyError, IndexError) as exc:
            raise HTTPException(status_code=404, detail=explain_error(exc)) from exc

    def _guard_review_invalidation(request: RunRequest):
        """重跑质检会静默把已提交 / 已通过的审核打回草稿，必须先显式确认。"""
        if request.operation != "quality" or request.invalidate_reviews or not request.batch_id:
            return
        try:
            batch = _store().get_batch(request.batch_id)
        except (ValueError, OSError, KeyError):
            return  # 交给 _start_request 给出统一的 422，不在门禁层抛 500
        affected = [item["episode_index"] for item in batch["episodes"]
                    if item.get("review", {}).get("status") in {"submitted", "approved"}]
        if affected:
            preview = "、".join(str(index) for index in affected[:8])
            more = "…" if len(affected) > 8 else ""
            raise HTTPException(
                status_code=409,
                detail=(f"该批次有 {len(affected)} 个任务已提交或已通过审核（Episode {preview}{more}）。"
                        "重跑批次质检会使审核失效并退回筛选确认阶段；已验证转换产物与交付包也会变为非当前版本，"
                        "需重新质检、转换与打包。如确认继续，请显式确认后再提交。"),
            )

    def _guard_disposition_invalidation(batch_id: str, request: DispositionRequest):
        """改筛选决定会让全部保留任务的审核与已验证产物一起失效，须先确认。"""
        if request.invalidate_reviews:
            return
        try:
            batch = _store().get_batch(batch_id)
        except (ValueError, OSError, KeyError):
            return  # 交给写操作给出统一的 409，不在门禁层抛 500
        protected = [item["episode_index"] for item in batch["episodes"]
                     if item.get("review", {}).get("status") in {"submitted", "approved"}]
        current = [item for item in [*batch.get("outputs", []), *batch.get("deliveries", [])]
                   if item.get("current")]
        if protected or current:
            raise HTTPException(
                status_code=409,
                detail=(f"该批次有 {len(protected)} 个任务已提交或已通过审核、{len(current)} 项当前产物/交付包。"
                        "修改筛选决定会把全部保留任务的审核退回草稿、清洗确认失效，已验证转换产物与交付包"
                        "也会变为非当前版本，需重新确认、转换与打包。如确认继续，请显式确认后再提交。"),
            )

    @app.post("/api/runs", status_code=202)
    def start_run(request: RunRequest):
        _guard_review_invalidation(request)
        try:
            run_id = _start_request(request)
            return {"run_id": run_id, "status": "queued"}
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=explain_error(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=503, detail=explain_error(exc)) from exc
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=explain_error(exc)) from exc

    @app.post("/api/imports/upload", status_code=202)
    async def upload_hdf5(file: UploadFile = File(...), label: str = Form(...), test_label: str | None = Form(None)):
        if Path(file.filename or "").suffix.lower() not in {".h5", ".hdf5"}:
            raise HTTPException(status_code=422, detail="仅支持 .h5 或 .hdf5 文件")
        try:
            path = save_upload(file.file, file.filename or "upload.hdf5")
            run_id = business.start_import(path, "hdf5", label, test_label)
            return {"run_id": run_id, "status": "queued"}
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=422, detail=explain_error(exc)) from exc
        finally:
            await file.close()

    @app.patch("/api/batches/{batch_id}/episodes/{episode_index}/disposition")
    def set_disposition(batch_id: str, episode_index: int, request: DispositionRequest):
        _guard_disposition_invalidation(batch_id, request)
        try:
            result = _store().set_disposition(batch_id, episode_index, request.disposition,
                                              reason=request.reason, expected_revision=request.revision)
            return _public_batch(result)
        except (ValueError, OSError, KeyError) as exc:
            raise HTTPException(status_code=409, detail=explain_error(exc)) from exc

    @app.post("/api/batches/{batch_id}/cleaning/confirm")
    def confirm_cleaning(batch_id: str, request: RevisionRequest):
        try:
            return _public_batch(_store().confirm_cleaning(batch_id, expected_revision=request.revision))
        except (ValueError, OSError, KeyError) as exc:
            raise HTTPException(status_code=409, detail=explain_error(exc)) from exc

    @app.patch("/api/batches/{batch_id}/episodes/{episode_index}/annotation")
    def save_annotation(batch_id: str, episode_index: int, request: AnnotationRequest):
        try:
            result = _store().save_annotation(batch_id, episode_index, task=request.task, outcome=request.outcome,
                                              tags=request.tags, notes=request.notes, expected_revision=request.revision)
            return _public_batch(result)
        except (ValueError, OSError, KeyError) as exc:
            raise HTTPException(status_code=409, detail=explain_error(exc)) from exc

    @app.post("/api/batches/{batch_id}/episodes/{episode_index}/annotation/submit")
    def submit_annotation(batch_id: str, episode_index: int, request: RevisionRequest):
        try:
            return _public_batch(_store().submit_annotation(batch_id, episode_index, expected_revision=request.revision))
        except (ValueError, OSError, KeyError) as exc:
            raise HTTPException(status_code=409, detail=explain_error(exc)) from exc

    @app.post("/api/batches/{batch_id}/episodes/{episode_index}/review")
    def review_annotation(batch_id: str, episode_index: int, request: ReviewRequest):
        try:
            result = _store().review_annotation(batch_id, episode_index, approve=request.approve,
                                                reason=request.reason, expected_revision=request.revision)
            return _public_batch(result)
        except (ValueError, OSError, KeyError) as exc:
            raise HTTPException(status_code=409, detail=explain_error(exc)) from exc

    @app.get("/api/runs")
    def runs():
        return {"runs": [_public_value(item) for item in ui_runs.available_runs()]}

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str):
        return _load_state(run_id)

    @app.post("/api/runs/{run_id}/recover")
    def recover_run(run_id: str, request: RecoverRequest | None = None):
        """显式恢复中断运行的业务登记，只核对已有产物，不重跑任何处理步骤。"""
        # 质检登记是破坏性回写：apply_quality_report 会把所有任务的审核打回草稿、
        # 清掉清洗确认，并让已验证输出与交付包变为非当前版本。用户点「恢复登记」
        # 时未必预期这个后果，因此必须先显式确认。门禁放在节流之前，保证被拦下
        # 不会占用用户的重试额度。
        pending = _pending_registration(run_id)
        if (pending.get("status") in {"pending", "failed"}
                and pending.get("registration_operation") == "quality"
                and not (request and request.invalidate_reviews)):
            raise HTTPException(
                status_code=409,
                detail=("该运行待恢复的是批次质检登记。恢复会重置全部任务的审核状态、"
                        "退回筛选确认阶段，已验证转换产物与交付包也会变为非当前版本，"
                        "需重新质检、转换与打包。如确认继续，请显式确认后再提交。"),
            )
        # 本机单用户模型没有会话概念，仍对同一运行的重复点击做最小节流，
        # 避免按钮连击 / 前端重试把过期证据反复回写。
        now = time.monotonic()
        with _recover_guard:
            if now - _recover_last.get(run_id, -1e9) < 2.0:
                raise HTTPException(status_code=429, detail="恢复登记操作过于频繁，请稍后重试",
                                    headers={"Retry-After": "2"})
            _recover_last[run_id] = now
        try:
            result = business.reconcile_run(run_id, runs_root=ui_runs.runs_root(), force=True)
        except (ValueError, OSError, KeyError) as exc:
            raise HTTPException(status_code=409, detail=explain_error(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        status = str(result.get("status", ""))
        if status == "stale":
            raise HTTPException(status_code=409, detail=str(result.get("message") or "版本已变化，登记未恢复"))
        if status == "failed":
            raise HTTPException(status_code=422, detail=str(result.get("message") or "恢复登记未完成"))
        return _public_value(result)

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str):
        # 终态必须显式下发 done，否则浏览器 EventSource 会在服务端 break 后无限自动重连。
        safe_id = Path(run_id).name == run_id and "/" not in run_id and "\\" not in run_id
        events_path = ui_runs.runs_root() / (run_id if safe_id else "__invalid__") / "events.jsonl"

        async def stream():
            previous = None
            events_stamp = None
            cached_events = []
            started = last_heartbeat = time.monotonic()
            while True:
                try:
                    state = _load_state(run_id)
                    try:
                        info = events_path.stat()
                        stamp = (info.st_mtime_ns, info.st_size)
                    except OSError:
                        stamp = (0, 0)
                    # events.jsonl 只增不改：文件未变时复用内存快照，避免每 0.8s 全量重读。
                    if stamp != events_stamp:
                        cached_events = _public_value(ui_runs.load_events(run_id))
                        events_stamp = stamp
                    payload = json.dumps({"state": state, "events": cached_events}, ensure_ascii=False, allow_nan=False)
                    if payload != previous:
                        yield f"event: snapshot\ndata: {payload}\n\n"
                        previous = payload
                    if state.get("status") not in ui_runs.ACTIVE_STATUSES:
                        yield "event: done\ndata: {}\n\n"
                        break
                except HTTPException as exc:
                    payload = json.dumps({"error": exc.detail}, ensure_ascii=False)
                    yield f"event: error\ndata: {payload}\n\n"
                    yield "event: done\ndata: {}\n\n"
                    break
                now = time.monotonic()
                if now - started > 7200:
                    yield 'event: done\ndata: {"reason":"stream_timeout"}\n\n'
                    break
                if now - last_heartbeat > 15:
                    yield ": keepalive\n\n"
                    last_heartbeat = now
                await asyncio.sleep(0.8)
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    if FRONTEND_DIST.is_dir() and (FRONTEND_DIST / "index.html").is_file():
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("robodata.web:app", host="127.0.0.1", port=int(os.getenv("ROBODATA_WEB_PORT", "8765")), reload=False)
