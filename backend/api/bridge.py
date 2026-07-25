"""Inbound endpoint for the whatsapp-web.js bridge (whatsapp-bridge/).

The bridge is a trusted local relay, authenticated with a shared secret
-- not Meta's HMAC scheme, which only Meta-signed payloads can use. The
message then flows through the exact same dispatcher pipeline (dedup,
sender resolution, rate limit, registry) as Cloud API webhooks.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header
from fastapi.responses import JSONResponse

from backend.api.whatsapp_dispatcher import (
    InboundMedia,
    InboundMessage,
    WhatsAppDispatcher,
    get_dispatcher,
)
from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.core.security import jid_to_e164, verify_shared_secret
from backend.schemas.whatsapp import BridgeInboundMedia, BridgeInboundMessage

logger = get_logger(__name__)

router = APIRouter(prefix="/internal/whatsapp-bridge", tags=["bridge"])


@router.post("/messages")
async def receive_bridge_message(
    message: BridgeInboundMessage,
    background: BackgroundTasks,
    dispatcher: Annotated[WhatsAppDispatcher, Depends(get_dispatcher)],
    x_bridge_secret: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    if not verify_shared_secret(get_settings().bridge_shared_secret, x_bridge_secret):
        logger.warning("bridge_secret_invalid")
        return JSONResponse(
            status_code=401,
            content={"error": {"code": "invalid_secret", "message": "Secret check failed."}},
        )

    inbound = InboundMessage(
        message_id=message.message_id,
        sender_number=jid_to_e164(message.sender),
        reply_to=message.chat_id,
        kind="text" if message.kind == "chat" else message.kind,
        text=message.body,
    )
    background.add_task(dispatcher.process_inbound, inbound)
    return JSONResponse(content={"status": "received"})


@router.post("/media")
async def receive_bridge_media(
    media: BridgeInboundMedia,
    background: BackgroundTasks,
    dispatcher: Annotated[WhatsAppDispatcher, Depends(get_dispatcher)],
    x_bridge_secret: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Photos/PDFs take the OCR path -- acked immediately, answered when
    the sheet is parsed (docs/07_OCR.md §13: ~10s budget)."""
    if not verify_shared_secret(get_settings().bridge_shared_secret, x_bridge_secret):
        logger.warning("bridge_secret_invalid")
        return JSONResponse(
            status_code=401,
            content={"error": {"code": "invalid_secret", "message": "Secret check failed."}},
        )

    import base64
    import binascii

    try:
        data = base64.b64decode(media.data_base64, validate=True)
    except (binascii.Error, ValueError):
        logger.warning("bridge_media_undecodable", message_id=media.message_id)
        return JSONResponse(content={"status": "ignored"})

    max_bytes = get_settings().max_attachment_size_mb * 1024 * 1024
    if len(data) > max_bytes:
        return JSONResponse(content={"status": "too_large"})

    inbound = InboundMedia(
        message_id=media.message_id,
        sender_number=jid_to_e164(media.sender),
        reply_to=media.chat_id,
        mime_type=media.mime_type,
        data=data,
    )
    background.add_task(dispatcher.process_media, inbound)
    return JSONResponse(content={"status": "received"})
