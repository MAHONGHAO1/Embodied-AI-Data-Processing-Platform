import logging
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from data import runtime
from data.bootstrap import qrdf_available, qrdf_vendor_hint
from data.config import settings
from data.database import SessionLocal
from data.infra.redis_client import RedisUnavailableError, redis_service
from data.realtime.socketio import (
    bind_realtime_event_loop,
    create_socket_application,
    start_realtime_outbox_dispatcher,
    stop_realtime_outbox_dispatcher,
)
from data.routers import (
    annotation_work_items,
    auth,
    catalog_datasets,
    collection_dashboard,
    collection_device_models,
    collection_devices,
    collection_intake_review,
    collection_labels,
    collection_overview,
    collection_packages,
    collection_projects,
    collection_tasks,
    collection_upload_sessions,
    collector_profiles,
    dashboard,
    data_assets,
    data_batches,
    datasets,
    episodes,
    jobs,
    native_lerobot_direct_uploads,
    platform_settings,
    review_work_items,
    task_labels,
    work_queue,
    workspace,
)
from data.routers import (
    fetch_manifests as fetch_manifests,
)
from data.routers import (
    tokens as tokens,
)
from data.security.headers import SecurityHeadersMiddleware
from data.security.logging_setup import attach_secret_log_filter
from data.services.runtime_health import (
    DatabaseUnavailableError,
    require_database_health,
)
from data.static_compression import StaticAssetCompressionMiddleware
from data.utils.helpers import decode_token, success
from data.version import PRODUCT_VERSION

# frontend/ is sibling to backend/, pure static assets, no Node.js required
FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

# Attach secret log filter as early as possible (including TestClient import paths)
attach_secret_log_filter()
http_logger = logging.getLogger("quicdata.http")


@asynccontextmanager
async def lifespan(app: FastAPI):
    attach_secret_log_filter()
    settings.validate_security_baseline()
    settings.validate_supply_chain_config()
    settings.validate_storage_deployment_config()
    runtime.assert_schema_current()
    redis_service.connect_required()
    bind_realtime_event_loop()
    if settings.realtime_dispatcher_in_api:
        start_realtime_outbox_dispatcher()
    train_started = False
    try:
        if _train_mounted:
            from train.mount import startup_train

            startup_train()
            train_started = True
        yield
    finally:
        if train_started:
            from train.mount import shutdown_train

            shutdown_train()
        if settings.realtime_dispatcher_in_api:
            await stop_realtime_outbox_dispatcher()


app = FastAPI(
    title="QuicData API",
    description="QuicData 数据流转平台 MVP 后端服务",
    version=PRODUCT_VERSION,
    lifespan=lifespan,
)

_cors_origins = settings.cors_origin_list
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(StaticAssetCompressionMiddleware)


@app.exception_handler(RedisUnavailableError)
async def redis_unavailable_handler(_request: Request, _error: RedisUnavailableError):
    return JSONResponse(
        status_code=503,
        content={"code": 503, "message": "redis_unavailable", "data": None},
    )


@app.exception_handler(DatabaseUnavailableError)
async def database_unavailable_handler(_request: Request, _error: DatabaseUnavailableError):
    return JSONResponse(
        status_code=503,
        content={"code": 503, "message": "database_unavailable", "data": None},
    )


@app.middleware("http")
async def api_request_observability(request: Request, call_next):
    """Log API route outcomes without retaining headers, bodies, or query values."""
    if not request.url.path.startswith(settings.api_prefix):
        return await call_next(request)
    started_at = perf_counter()
    try:
        response = await call_next(request)
    except Exception:  # noqa: BLE001 - provider SDK errors are intentionally readiness-only
        http_logger.info(
            "api_request method=%s route=%s status=%s elapsed_ms=%s",
            request.method,
            request.url.path,
            500,
            int((perf_counter() - started_at) * 1000),
        )
        raise
    route = getattr(request.scope.get("route"), "path", request.url.path)
    if "{full_path:path}" in route:
        route = request.url.path
    elif route.startswith("/") and not route.startswith(settings.api_prefix):
        route = f"{settings.api_prefix}{route}"
    http_logger.info(
        "api_request method=%s route=%s status=%s elapsed_ms=%s",
        request.method,
        route,
        response.status_code,
        int((perf_counter() - started_at) * 1000),
    )
    return response


@app.middleware("http")
async def frontend_revalidation_headers(request: Request, call_next):
    """Keep the static console shell and modules aligned with its API contract."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith(("/js/", "/css/", "/vendor/")):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.middleware("http")
async def require_initial_password_change(request: Request, call_next):
    """Restrict one-time bootstrap accounts until they set a private password."""
    allowed_paths = {
        "/health",
        f"{settings.api_prefix}/version",
        f"{settings.api_prefix}/auth/login",
        f"{settings.api_prefix}/auth/refresh",
        f"{settings.api_prefix}/auth/logout",
        f"{settings.api_prefix}/auth/change-password",
    }
    authorization = request.headers.get("authorization", "")
    if request.url.path not in allowed_paths and authorization.lower().startswith("bearer "):
        payload = decode_token(authorization[7:].strip(), expected_type="access")
        if payload and payload.get("must_change_password"):
            return JSONResponse(
                status_code=403,
                content={"code": 403, "message": "请先修改初始密码", "data": None},
            )
    return await call_next(request)


prefix = settings.api_prefix
app.include_router(auth.router, prefix=prefix)
app.include_router(tokens.router, prefix=prefix)
app.include_router(fetch_manifests.router, prefix=prefix)
app.include_router(workspace.router, prefix=prefix)

app.include_router(native_lerobot_direct_uploads.router, prefix=prefix)
app.include_router(jobs.router, prefix=prefix)
app.include_router(platform_settings.router, prefix=prefix)
app.include_router(dashboard.router, prefix=prefix)
app.include_router(episodes.router, prefix=prefix)
app.include_router(work_queue.router, prefix=prefix)
app.include_router(collector_profiles.router, prefix=prefix)
app.include_router(collection_device_models.router, prefix=prefix)
app.include_router(collection_devices.router, prefix=prefix)
app.include_router(collection_intake_review.router, prefix=prefix)
app.include_router(collection_labels.router, prefix=prefix)
app.include_router(collection_overview.router, prefix=prefix)
app.include_router(collection_dashboard.router, prefix=prefix)
app.include_router(collection_packages.router, prefix=prefix)
app.include_router(collection_projects.router, prefix=prefix)
app.include_router(collection_tasks.router, prefix=prefix)
app.include_router(collection_upload_sessions.router, prefix=prefix)
app.include_router(data_batches.router, prefix=prefix)
app.include_router(data_assets.router, prefix=prefix)
app.include_router(catalog_datasets.router, prefix=prefix)
app.include_router(annotation_work_items.router, prefix=prefix)
app.include_router(review_work_items.router, prefix=prefix)
app.include_router(task_labels.router, prefix=prefix)
app.include_router(datasets.router, prefix=prefix)
app.include_router(datasets.revision_router, prefix=prefix)
app.include_router(datasets.export_router, prefix=prefix)
from train.catalog_bridge import router as train_catalog_router

app.include_router(train_catalog_router, prefix=prefix)


@app.get(f"{settings.api_prefix}/version")
def product_version():
    """Public product version shown by the web console (PEP 440, e.g. 1.0.0a1)."""
    return success({"version": PRODUCT_VERSION})


@app.get("/health")
def health():
    from data.infra.object_storage import ObjectStorageError, StorageNotReady
    from data.infra.storage_provider import get_storage_provider
    from data.services.task_dispatcher import worker_status_snapshot

    db = SessionLocal()
    try:
        require_database_health(db)
    finally:
        db.close()
    redis_service.ping_required()
    worker_status = worker_status_snapshot()

    storage_ready = True
    try:
        provider = get_storage_provider()
        provider.healthcheck()
    except (StorageNotReady, ObjectStorageError, OSError):
        storage_ready = False

    ready = storage_ready and bool(worker_status["available"])
    payload = {
        "status": "ok" if ready else "degraded",
        "ready": ready,
        "platform": "QuicData",
        "version": PRODUCT_VERSION,
        "infra": {
            "database": True,
            "redis": True,
            "celery": bool(worker_status["available"]),
            "qrdf_sdk": qrdf_available(),
            "qrdf_vendor": qrdf_vendor_hint(),
            "storage": {
                "provider": settings.storage_provider,
                "endpoint_configured": bool(settings.storage_endpoint),
                "ready": storage_ready,
                "buckets": {
                    "raw": settings.oss_bucket_raw,
                    "process": settings.oss_bucket_process,
                    "export": settings.oss_bucket_export,
                },
            },
        },
    }
    if not ready:
        return JSONResponse(status_code=503, content=payload)
    return payload


def _mount_frontend() -> None:
    if not FRONTEND_DIR.is_dir():
        return

    css_dir = FRONTEND_DIR / "css"
    js_dir = FRONTEND_DIR / "js"
    if css_dir.is_dir():
        app.mount("/css", StaticFiles(directory=css_dir), name="css")
    if js_dir.is_dir():
        app.mount("/js", StaticFiles(directory=js_dir), name="js")

    @app.get("/")
    async def serve_index():
        return FileResponse(FRONTEND_DIR / "index.html")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        if full_path.startswith("api/") or full_path in ("health", "docs", "openapi.json", "redoc"):
            raise HTTPException(status_code=404, detail="Not Found") from None
        frontend_root = FRONTEND_DIR.resolve()
        try:
            target = (frontend_root / full_path).resolve()
            target.relative_to(frontend_root)
        except (OSError, ValueError):
            raise HTTPException(status_code=404, detail="Not Found") from None
        if full_path and target.is_file():
            return FileResponse(target)
        return FileResponse(FRONTEND_DIR / "index.html")


_train_mounted = False
try:
    from train.mount import mount_train

    _train_mounted = mount_train(app)
except Exception:
    logging.getLogger("quicdata.train").warning("train routes not mounted", exc_info=True)

_mount_frontend()

http_app = app
app, socket_server = create_socket_application(http_app)
