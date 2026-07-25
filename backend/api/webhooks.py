"""Meta webhook endpoints -- docs/08_WhatsApp.md §1. HTTP concerns only:
handshake, signature check, fast 200 ack; processing happens after the
response via background task (Celery takes over the slow paths in the
workers phase)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError

from backend.api.whatsapp_dispatcher import WhatsAppDispatcher, get_dispatcher
from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.core.security import verify_webhook_signature
from backend.schemas.whatsapp import WebhookPayload

logger = get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.get("/whatsapp")
async def verify_subscription(
    hub_mode: Annotated[str, Query(alias="hub.mode")] = "",
    hub_verify_token: Annotated[str, Query(alias="hub.verify_token")] = "",
    hub_challenge: Annotated[str, Query(alias="hub.challenge")] = "",
) -> PlainTextResponse:
    """Meta's one-time challenge/response handshake at webhook setup."""
    settings = get_settings()
    if (
        hub_mode == "subscribe"
        and settings.whatsapp_verify_token
        and hub_verify_token == settings.whatsapp_verify_token
    ):
        return PlainTextResponse(hub_challenge)
    logger.warning("webhook_verification_rejected", mode=hub_mode)
    return PlainTextResponse("verification failed", status_code=403)


@router.post("/whatsapp")
async def receive_webhook(
    request: Request,
    background: BackgroundTasks,
    dispatcher: Annotated[WhatsAppDispatcher, Depends(get_dispatcher)],
) -> JSONResponse:
    raw_body = await request.body()
    if not verify_webhook_signature(
        get_settings().whatsapp_app_secret,
        raw_body,
        request.headers.get("X-Hub-Signature-256"),
    ):
        logger.warning("webhook_signature_invalid")
        return JSONResponse(
            status_code=401,
            content={"error": {"code": "invalid_signature", "message": "Signature check failed."}},
        )

    try:
        payload = WebhookPayload.model_validate_json(raw_body)
    except ValidationError:
        # signed-but-unparseable: log for investigation, 200 so Meta
        # doesn't retry a payload that will never parse
        logger.error("webhook_payload_unparseable", body=raw_body[:1000].decode(errors="replace"))
        return JSONResponse(content={"status": "ignored"})

    background.add_task(dispatcher.process_webhook, payload)
    return JSONResponse(content={"status": "received"})
