"""FastAPI application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.routes import ROUTERS
from .api_common import ErrorResponse
from .config import Settings
from .container import build_container
from .db import Database
from .emailer import EmailService
from .errors import install_error_handlers
from .logging_setup import configure_logging
from .middleware import RequestContextMiddleware
from .spec import load_spec

log = logging.getLogger("app")


def create_app(settings: Settings | None = None, *, database: Database | None = None, email: EmailService | None = None) -> FastAPI:
    settings = (settings or Settings.from_env()).validate()
    configure_logging(settings.log_level)
    spec = load_spec()
    container = build_container(settings, database, email)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        container.db.open()
        log.info("application started", extra={"event": "startup", "env": settings.app_env})
        try:
            yield
        finally:
            container.db.close()
            log.info("application stopped", extra={"event": "shutdown"})

    app = FastAPI(
        title=f"{spec['project']['name']} API", version="0.1.0", lifespan=lifespan,
        description=(spec["project"].get("description") or "") + "\n\nGenerated from the project graphs by the Backend Agent. "
        "Every route's `operationId` is its api.json endpoint id.",
        docs_url="/docs" if not settings.is_production else None, redoc_url=None,
    )
    app.state.container = container
    install_error_handlers(app)
    for router in ROUTERS:
        app.include_router(router)

    @app.get("/health", tags=["infrastructure"], summary="Liveness", operation_id="health.live", include_in_schema=True)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", tags=["infrastructure"], summary="Readiness (database reachable)", operation_id="health.ready",
             responses={503: {"model": ErrorResponse, "description": "Dependency unavailable"}})
    def ready(request: Request) -> Any:
        if container.db.ping():
            return {"status": "ready", "database": "ok"}
        request.state.error_code = "DATABASE_UNAVAILABLE"
        return JSONResponse({"error": {"code": "DATABASE_UNAVAILABLE", "message": "The database is currently unavailable.", "details": {}},
                             "message": "The database is currently unavailable.", "request_id": getattr(request.state, "request_id", None)}, status_code=503)

    app.add_middleware(RequestContextMiddleware, max_body_bytes=settings.max_body_bytes)
    app.add_middleware(
        CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID", "X-Total-Count"], max_age=600)
    return app
