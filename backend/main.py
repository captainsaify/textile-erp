"""FastAPI application entrypoint.

Run locally:  uvicorn backend.main:app --reload
Production:   gunicorn -k uvicorn.workers.UvicornWorker backend.main:app
(docs/16_Deployment.md)
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.api import bridge, webhooks
from backend.api.errors import register_error_handlers
from backend.api.routers import auth, catalog, reconciliation, reporting
from backend.core.db import check_db_connection
from backend.core.lifecycle import release_all
from backend.core.logging import configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging()
    logger.info("app_startup")
    yield
    # the same registry the workers use, so shutdown can't drift from
    # task cleanup (backend/core/lifecycle.py)
    await release_all()
    logger.info("app_shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="WhatsApp Trading ERP",
        version="1.0.0",
        lifespan=lifespan,
        # docs/10_API.md §8: the schema is generated, never hand-written
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs",
    )
    register_error_handlers(app)
    app.include_router(webhooks.router)
    app.include_router(bridge.router)
    app.include_router(auth.router)
    app.include_router(reporting.router)
    app.include_router(catalog.router)
    app.include_router(reconciliation.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"status": "ok", "database": await check_db_connection()}

    return app


app = create_app()
