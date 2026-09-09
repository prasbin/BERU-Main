"""BERU application entrypoint.

Exposes :func:`create_app` (an application factory) and a module-level ``app``
for ASGI servers (``uvicorn backend.main:app``). Running this module directly
starts a development server using the configured host/port.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from backend.api.routers import (
    auth,
    browser,
    chat,
    conversations,
    desktop,
    facts,
    health,
    monitor,
    plans,
    projects,
    reliability,
    scheduler,
    sync,
    system,
    system_control,
    voice,
    websocket,
)
from backend.core.config import get_settings
from backend.core.errors import register_exception_handlers
from backend.core.logging import configure_logging, get_logger, request_id_ctx
from backend.database.base import get_engine, get_sessionmaker
from backend.database.init_db import init_db
from backend.engines.llm.registry import get_llm_provider

logger = get_logger(__name__)

API_V1_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown: initialise the database, release resources on exit."""
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    logger.info("Starting %s v%s (%s)", settings.app_name, settings.version, settings.environment)
    # Enforce the single-instance assumption before touching the database: two
    # processes must never run Alembic migrations against the same file. The
    # lock is held for the entire lifetime and released on every exit path.
    from backend.database.lock import acquire_db_lock

    db_lock = acquire_db_lock(settings.database_url)
    try:
        await init_db()
        # Stage 5.1 bootstrap: create the owner account on first run (when auth
        # is enabled) and restore durable login sessions from the DB so users
        # stay signed in across restarts.
        from backend.services.user_service import ensure_owner

        async with get_sessionmaker() as bootstrap_session:
            await ensure_owner(bootstrap_session)
        from backend.api.security import restore_sessions

        restored_sessions = await restore_sessions(get_sessionmaker())
        if restored_sessions:
            logger.info("Restored %d login session(s)", restored_sessions)
        logger.info("LLM provider: %s", get_llm_provider().name)
        # Durable reliability ledger: persist entries to the DB and restore the
        # most recent ones into memory (pruning rows beyond the bounded window).
        from backend.services.activity_ledger import (
            restore_activity_ledger,
            set_ledger_session_factory,
        )

        set_ledger_session_factory(get_sessionmaker)
        restored = await restore_activity_ledger()
        if restored["activity"] or restored["audit"]:
            logger.info(
                "Restored reliability ledger (%d activity, %d audit)",
                restored["activity"],
                restored["audit"],
            )
        if settings.proactive_enabled:
            from backend.services.proactive_service import start_proactive_runtime

            await start_proactive_runtime()
        # Bind durable global hotkeys (Windows) on the listener thread at startup.
        from backend.engines.hotkeys import get_hotkey_engine

        get_hotkey_engine().start()
        try:
            yield
        finally:
            get_hotkey_engine().shutdown()
            if settings.proactive_enabled:
                from backend.services.proactive_service import stop_proactive_runtime

                await stop_proactive_runtime()
            # Drain best-effort reliability ledger writes so the durable trail is
            # complete before connections close.
            from backend.services.activity_ledger import get_activity_ledger

            await get_activity_ledger().flush()
            # Release network clients and database connections.
            await get_llm_provider().aclose()
            await get_engine().dispose()
            logger.info("BERU shut down cleanly.")
    finally:
        if db_lock is not None:
            db_lock.release()


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    # Safety: refuse to bind to a non-localhost interface without auth.
    if not settings.auth_enabled and not settings.is_localhost_host:
        raise SystemExit(
            "Refusing to start: BERU is bound to a non-localhost interface "
            f"({settings.host}) without a BERU_API_KEY configured. "
            "Set BERU_API_KEY or bind to 127.0.0.1 / localhost."
        )

    app = FastAPI(
        title=f"{settings.app_name} API",
        version=settings.version,
        description="BERU — a modular personal AI operating system / agent backend.",
        lifespan=lifespan,
    )

    # ---- Middleware ----
    # Credentials (cookies) are only relayed for a browser when CORS origins are
    # explicit. With the default wildcard origin, allow_credentials would let any
    # website issue credentialed requests against the local API.
    cors_origins = settings.cors_origin_list
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials="*" not in cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    max_body = settings.max_request_body_bytes

    @app.middleware("http")
    async def limit_request_body(request: Request, call_next):
        """Reject requests with a body exceeding the configured size limit."""
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                size = int(content_length)
            except ValueError:
                pass
            else:
                if size > max_body:
                    from fastapi.responses import JSONResponse

                    return JSONResponse(
                        status_code=400,
                        content={
                            "error": {
                                "type": "bad_request",
                                "message": (
                                    f"Request body too large. Maximum size is {max_body} bytes."
                                ),
                                "detail": {
                                    "max_bytes": max_body,
                                    "received_bytes": size,
                                },
                            }
                        },
                    )
        return await call_next(request)

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        token = request_id_ctx.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_ctx.reset(token)
        response.headers["X-Request-ID"] = rid
        return response

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        """Apply baseline browser security headers to every response.

        The UI is served same-origin; the CSP below reflects that (scripts
        must come from 'self', connections keep to the same origin plus
        WebSocket, inline styles are the only relaxation so the inline
        <style> block in index.html keeps working).
        """
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self' ws: wss:; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
        )
        return response

    @app.middleware("http")
    async def record_request_metrics(request: Request, call_next):
        """Count every response for the /metrics observability endpoint."""
        from backend.core.errors import ERROR_TYPE_HEADER
        from backend.services.observability import get_request_tracker

        response = await call_next(request)
        get_request_tracker().record(
            response.status_code,
            error_type=response.headers.get(ERROR_TYPE_HEADER),
        )
        return response

    # ---- Error handlers ----
    register_exception_handlers(app)

    # ---- Routes ----
    app.include_router(health.router)  # /health (unversioned)

    # Serve frontend static files
    from pathlib import Path

    from fastapi.responses import FileResponse

    frontend_dir = Path(__file__).parent.parent / "frontend"
    if frontend_dir.exists():
        @app.get("/", tags=["system"], summary="BERU Frontend")
        async def root() -> FileResponse:
            return FileResponse(frontend_dir / "index.html")

        @app.get("/app.js", tags=["system"], summary="Frontend JavaScript")
        async def serve_app_js() -> FileResponse:
            return FileResponse(frontend_dir / "app.js", media_type="application/javascript")
    else:
        @app.get("/", tags=["system"], summary="Service root")
        async def root() -> dict:
            return {
                "app": settings.app_name,
                "version": settings.version,
                "docs": "/docs",
                "health": "/health",
                "api": API_V1_PREFIX,
            }

    app.include_router(auth.router, prefix=API_V1_PREFIX)
    app.include_router(browser.router, prefix=API_V1_PREFIX)
    app.include_router(chat.router, prefix=API_V1_PREFIX)
    app.include_router(conversations.router, prefix=API_V1_PREFIX)
    app.include_router(desktop.router, prefix=API_V1_PREFIX)
    app.include_router(facts.router, prefix=API_V1_PREFIX)
    app.include_router(monitor.router, prefix=API_V1_PREFIX)
    app.include_router(plans.router, prefix=API_V1_PREFIX)
    app.include_router(projects.router, prefix=API_V1_PREFIX)
    app.include_router(reliability.router, prefix=API_V1_PREFIX)
    app.include_router(scheduler.router, prefix=API_V1_PREFIX)
    app.include_router(sync.router, prefix=API_V1_PREFIX)
    app.include_router(system.router, prefix=API_V1_PREFIX)
    app.include_router(system_control.router, prefix=API_V1_PREFIX)
    app.include_router(voice.router, prefix=API_V1_PREFIX)
    app.include_router(voice.ws_router, prefix=API_V1_PREFIX)
    app.include_router(websocket.router)

    return app


app = create_app()


def main() -> None:
    """Run a development server (``python -m backend.main``)."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "backend.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    main()
