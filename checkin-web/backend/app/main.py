import os
import logging
import shutil
from pathlib import Path
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.routers import admin, public, user
from core.config import get_settings
from core.security import credential_key_id
from services.admin_service import bootstrap_admin
from services.auth_service import retry_pending_storage_cleanup
from services.admin_event_service import admin_event_broker
from services.checkin_service import (
    auto_checkin_scheduler_healthy,
    start_auto_checkin_scheduler,
    stop_auto_checkin_scheduler,
)
from services.notification_service import (
    start_notification_worker,
    stop_notification_worker,
    notification_worker_healthy,
    validate_email_configuration,
)
from storage.database import connect, init_database

logger = logging.getLogger(__name__)


def _admin_page_route(path: str, base: str) -> bool:
    path = path.rstrip("/") or "/"
    return path == base or path == f"{base}/dashboard" or path.startswith(f"{base}/dashboard/")


def _harden_path_permissions(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError as exc:
        # Windows Docker bind mounts can reject chmod from the non-root user.
        # The host ACL remains authoritative there; Linux production volumes
        # should accept these permissions.
        logger.warning("Unable to tighten runtime permissions for %s: %s", path.name, exc.__class__.__name__)


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if os.name == "posix":
            os.umask(0o077)
        anyio.to_thread.current_default_thread_limiter().total_tokens = settings.api_thread_limit
        validate_email_configuration()
        if settings.env == "production":
            missing = []
            if not settings.admin_username:
                missing.append("CHECKIN_WEB_ADMIN_USERNAME")
            if not settings.admin_password:
                missing.append("CHECKIN_WEB_ADMIN_PASSWORD_FILE")
            if not settings.credential_key:
                missing.append("CHECKIN_WEB_CREDENTIAL_KEY_FILE")
            if missing:
                raise RuntimeError(f"生产环境缺少必要配置: {', '.join(missing)}")
            if not settings.secure_cookies:
                raise RuntimeError("生产环境必须启用 CHECKIN_WEB_SECURE_COOKIES")
            # Decode and validate the key before the service starts accepting traffic.
            credential_key_id()
        init_database(settings.database_path)
        if os.name == "posix":
            _harden_path_permissions(settings.data_dir, 0o700)
            for database_file in (
                settings.database_path,
                Path(f"{settings.database_path}-wal"),
                Path(f"{settings.database_path}-shm"),
            ):
                if database_file.exists():
                    _harden_path_permissions(database_file, 0o600)
        with connect(settings.database_path) as db:
            bootstrap_admin(db)
            retry_pending_storage_cleanup(db)
        start_auto_checkin_scheduler()
        start_notification_worker()
        await admin_event_broker.start()
        try:
            yield
        finally:
            await admin_event_broker.stop()
            stop_notification_worker()
            stop_auto_checkin_scheduler()

    app = FastAPI(
        title="SWU Daka Multi-user",
        version="1.2.0",
        docs_url=None if settings.env == "production" else "/docs",
        redoc_url=None if settings.env == "production" else "/redoc",
        openapi_url=None if settings.env == "production" else "/openapi.json",
        lifespan=lifespan,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    app.add_middleware(RequestBodyLimitMiddleware, max_body_size=settings.max_request_body_bytes)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "X-CSRF-Token"],
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; object-src 'none'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if request.url.path.startswith("/api/") or request.url.path == "/health" or _admin_page_route(request.url.path, settings.admin_page_path):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        if settings.secure_cookies:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.get("/health")
    async def health() -> dict:
        database_healthy = False
        cleanup_pending = False
        integrity_issues = False
        try:
            with connect(settings.database_path) as db:
                database_healthy = db.execute("SELECT 1").fetchone()[0] == 1
                cleanup_pending = bool(
                    db.execute("SELECT 1 FROM storage_cleanup_queue LIMIT 1").fetchone()
                )
                integrity_issues = bool(
                    db.execute("SELECT 1 FROM migration_integrity_issues LIMIT 1").fetchone()
                )
        except Exception:
            logger.exception("Health database check failed")
        try:
            storage_healthy = shutil.disk_usage(settings.data_dir).free >= 256 * 1024 * 1024
        except OSError:
            storage_healthy = False
        scheduler_healthy = auto_checkin_scheduler_healthy()
        mail_worker_healthy = notification_worker_healthy()
        realtime_status = admin_event_broker.status()
        essential_healthy = all(
            (database_healthy, storage_healthy, scheduler_healthy, mail_worker_healthy, realtime_status["healthy"])
        )
        return {
            "status": "ok" if essential_healthy else "degraded",
            "env": settings.env,
            "admin_configured": bool(settings.admin_username and settings.admin_password),
            "credential_key_configured": bool(settings.credential_key),
            "email_notifications_enabled": settings.email_notifications_enabled,
            "email_configured": settings.email_configured,
            "verification_email_configured": settings.verification_email_configured,
            "failure_email_configured": settings.failure_email_configured,
            "checks": {
                "database": database_healthy,
                "storage": storage_healthy,
                "scheduler": scheduler_healthy,
                "notification_worker": mail_worker_healthy,
                "realtime_updates": bool(realtime_status["healthy"]),
            },
            "maintenance_required": cleanup_pending or integrity_issues,
        }

    app.include_router(public.router, prefix="/api/public", tags=["public"])
    app.include_router(user.router, prefix="/api/user", tags=["user"])
    app.include_router(admin.router, prefix=settings.admin_api_prefix, tags=["admin"])

    frontend_dist = settings.project_root / "frontend" / "dist"
    assets_dir = frontend_dist / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def frontend(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse(status_code=404, content={"detail": "API route not found"})
        if settings.env == "production" and full_path in {"docs", "redoc", "openapi.json"}:
            return JSONResponse(status_code=404, content={"detail": "Not found"})
        index_file: Path = frontend_dist / "index.html"
        if index_file.exists():
            if _admin_page_route(f"/{full_path}", settings.admin_page_path):
                index_html = index_file.read_text(encoding="utf-8")
                if "</head>" not in index_html:
                    return JSONResponse(status_code=503, content={"detail": "Frontend unavailable"})
                admin_meta = (
                    f'<meta name="swu-admin-page-path" content="{settings.admin_page_path}" />'
                    f'<meta name="swu-admin-api-prefix" content="{settings.admin_api_prefix}" />'
                )
                return HTMLResponse(index_html.replace("</head>", f"{admin_meta}</head>", 1))
            return FileResponse(index_file)
        return JSONResponse(
            status_code=404,
            content={"detail": "Frontend has not been built. Use the Vite dev server or build frontend/dist."},
        )

    return app


app = create_app()
