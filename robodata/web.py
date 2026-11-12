"""Loopback-only JSON bridge and static frontend host for the workbench."""

from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

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
PRIVATE_KEYS = {"input_path", "archive_path", "business_root", "data_root", "hdf5_path", "output_path"}


class RunRequest(BaseModel):
    operation: Literal["import", "quality", "final_quality", "conversion", "delivery", "verify_delivery"]
    source_kind: Literal["hdf5", "so100", "aloha"] | None = None
    path: str | None = None
    label: str | None = None
    test_label: str | None = None
    batch_id: str | None = None
    revision: int | None = None
    output_id: str | None = None
    archive_path: str | None = None


class DispositionRequest(BaseModel):
    revision: int
    disposition: Literal["keep", "quarantine", "exclude"]
    reason: str = ""


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
    return json_safe(value)


def _public_batch(batch: dict) -> dict:
    value = copy.deepcopy(batch)
    source = value.get("source", {})
    for key in PRIVATE_KEYS:
        source.pop(key, None)
    return _public_value(value)


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
    try:
        state = ui_runs.load_run(run_id)
        if state.get("status") not in ui_runs.ACTIVE_STATUSES:
            business.reconcile_run(run_id)
            state = ui_runs.load_run(run_id)
        return _public_value(state)
    except (KeyError, ValueError, OSError) as exc:
        raise HTTPException(status_code=404, detail=explain_error(exc)) from exc


def create_app() -> FastAPI:
    app = FastAPI(title="RoboData local workbench", version="0.8.0", docs_url=None, redoc_url=None)
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
        return {"status": "ok", "app": "RoboData Workbench", "version": "0.8.0", "scope": "loopback"}

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
            raw_root = batch["source"].get("data_root") or batch["source"].get("hdf5_path")
            if not raw_root:
                raise ValueError("批次没有可读取的源目录")
            root = Path(raw_root)
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
            raw_root = batch["source"].get("data_root") or batch["source"].get("hdf5_path")
            if not raw_root:
                raise ValueError("批次没有可读取的源目录")
            root = Path(raw_root)
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
            raw_root = batch["source"].get("data_root") or batch["source"].get("hdf5_path")
            if not raw_root:
                raise ValueError("批次没有可读取的源目录")
            root = Path(raw_root)
            episode = load_episode(root, episode_index)
            path = (episode.camera_paths or {}).get(camera_name)
            if path is None or not Path(path).is_file():
                raise HTTPException(status_code=404, detail="该相机视频不存在")
            return FileResponse(Path(path), media_type="video/mp4", filename=Path(path).name, content_disposition_type="inline")
        except HTTPException:
            raise
        except (ValueError, OSError, KeyError, IndexError) as exc:
            raise HTTPException(status_code=404, detail=explain_error(exc)) from exc

    @app.post("/api/runs", status_code=202)
    def start_run(request: RunRequest):
        try:
            run_id = _start_request(request)
            return {"run_id": run_id, "status": "queued"}
        except (ValueError, KeyError, OSError) as exc:
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

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str):
        async def stream():
            previous = None
            while True:
                try:
                    state = _load_state(run_id)
                    events = _public_value(ui_runs.load_events(run_id))
                    payload = json.dumps({"state": state, "events": events}, ensure_ascii=False, allow_nan=False)
                    if payload != previous:
                        yield f"event: snapshot\ndata: {payload}\n\n"
                        previous = payload
                    if state.get("status") not in ui_runs.ACTIVE_STATUSES:
                        break
                except HTTPException as exc:
                    payload = json.dumps({"error": exc.detail}, ensure_ascii=False)
                    yield f"event: error\ndata: {payload}\n\n"
                    break
                await asyncio.sleep(0.8)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    if FRONTEND_DIST.is_dir() and (FRONTEND_DIST / "index.html").is_file():
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("robodata.web:app", host="127.0.0.1", port=int(os.getenv("ROBODATA_WEB_PORT", "8765")), reload=False)
