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
from backend.core.db import check_db_connection, dispose_engine
from backend.core.logging import configure_logging, get_logger
from backend.core.redis import close_redis
from backend.services.whatsapp_bridge_client import close_bridge_sender
from backend.services.whatsapp_client import close_whatsapp_client

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging()
    logger.info("app_startup")
    yield
    await close_whatsapp_client()
    await close_bridge_sender()
    await close_redis()
    await dispose_engine()
    logger.info("app_shutdown")


def create_app() -> FastAPI:
    app = FastAPI(title="WhatsApp Trading ERP", lifespan=lifespan)
    app.include_router(webhooks.router)
    app.include_router(bridge.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"status": "ok", "database": await check_db_connection()}

    return app


app = create_app()
