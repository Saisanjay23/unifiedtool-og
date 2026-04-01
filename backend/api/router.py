"""
API Gateway and Inversion-of-Control (IoC) Bindings.
Governs the ASGI application bootstrap sequence, middleware pipelines (CORS, Rate Limiting),
and global exception shielding to prevent stacktrace propagation.
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from collections import defaultdict
import time

from backend.core.config import settings
from backend.core.db import init_indexes
from backend.core.health import HealthManager
from backend.core.logger import get_logger

logger = get_logger("api.router")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application bootstrap and teardown sequence.
    Seeds persistent connections (MongoDB indexes), spawns background daemon threads (`HealthWriter`), 
    and guarantees graceful socket disconnection during pod termination or SIGTERM.
    """
    # startup
    logger.info("Unified Social Media Tool API starting up...")
    settings.ensure_directories()

    # initialize MongoDB indexes
    try:
        await init_indexes()
        logger.info("Database indexes initialized")
    except Exception as exc:
        logger.error(f"Failed to initialize database: {exc}")

    # start background health writer
    import asyncio

    # Suppress noisy Windows ConnectionResetError [WinError 10054] tracebacks
    # These fire harmlessly when WebSocket clients disconnect
    loop = asyncio.get_running_loop()
    _default_handler = loop.get_exception_handler()

    def _quiet_exception_handler(loop, context):
        exc = context.get("exception")
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
            return  # silently ignore
        if _default_handler:
            _default_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(_quiet_exception_handler)

    health_mgr = HealthManager()
    health_task = asyncio.create_task(health_mgr.run_health_writer())

    # start background job cleanup
    from backend.core.jobs import JobManager
    job_mgr = JobManager()
    cleanup_task = asyncio.create_task(job_mgr.run_cleanup_loop())

    yield

    # shutdown
    logger.info("Shutting down...")

    # cancel health writer
    health_task.cancel()
    try:
        await health_task
    except asyncio.CancelledError:
        pass

    # cancel job cleanup
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass

    # cancel all running jobs
    await job_mgr.cancel_all()

    # close MongoDB connection
    from backend.core.db import close_connection

    await close_connection()

    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    """
    App Factory Pattern initialization.
    Constructs the ASGI application instance, assembling route graphs and static asset mounts.
    """
    app = FastAPI(
        title="Unified Social Media Tool",
        description="OSINT Social Media Intelligence Platform v2",
        version="2.0.0",
        lifespan=lifespan,
    )

    # CORS — explicitly allow local frontend URLs and typical production UI origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_CORS_ORIGINS.split(","),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    # Global Rate Limiting State (Memory-bound token bucket)
    _rate_limit_state = defaultdict(list)

    @app.middleware("http")
    async def rate_limit_middleware(request, call_next):
        client_ip = request.client.host if request.client else "127.0.0.1"
        path = request.url.path

        # Apply standard rate limiting to these critical external endpoints
        if path.startswith("/api/jobs/create") or path.startswith(
            "/api/sessions/login"
        ):
            now = time.time()
            # Clean up old timestamps (older than 60 seconds)
            _rate_limit_state[client_ip] = [
                ts for ts in _rate_limit_state[client_ip] if now - ts < 60
            ]

            # Max 30 requests per minute per IP to these routes
            if len(_rate_limit_state[client_ip]) >= 30:
                from fastapi.responses import JSONResponse

                logger.warning(
                    f"Rate limit exceeded for IP: {client_ip} on path {path}"
                )
                return JSONResponse(
                    status_code=429, content={"detail": "Too Many Requests"}
                )

            _rate_limit_state[client_ip].append(now)

        return await call_next(request)

    # register API routes
    from backend.api.routes.health import router as health_router
    from backend.api.routes.clients import router as clients_router
    from backend.api.routes.sessions import router as sessions_router
    from backend.api.routes.jobs import router as jobs_router
    from backend.api.routes.results import router as results_router
    from backend.api.routes.presets import router as presets_router

    app.include_router(health_router)
    app.include_router(clients_router)
    app.include_router(sessions_router)
    app.include_router(jobs_router)
    app.include_router(results_router)
    app.include_router(presets_router)

    # WebSocket endpoint for job progress
    from backend.api.websocket import job_progress_websocket

    app.add_api_websocket_route("/ws/jobs/{job_id}", job_progress_websocket)

    # serve React frontend if built
    frontend_dist = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "frontend", "dist"
    )
    if os.path.isdir(frontend_dist):
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
        logger.info(f"Serving frontend from {frontend_dist}")
    else:
        logger.info("Frontend not built — run 'python home.py --build' to build it")

        @app.get("/")
        async def root():
            return {
                "tool": "Unified Social Media Tool",
                "version": "2.0.0",
                "status": "API running",
                "frontend": "not built — run 'python home.py --build'",
                "docs": "/docs",
            }

    # Fallback Catch-All Shield: Prevents arbitrary exceptions from leaking architectural structure to clients
    from fastapi import Request
    from fastapi.responses import JSONResponse

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.error(f"Unhandled Server Error: {exc}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "detail": "An internal server error occurred. Please try again later."
            },
        )

    return app


# module-level app instance used by uvicorn
app = create_app()
