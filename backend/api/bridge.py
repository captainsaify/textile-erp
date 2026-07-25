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
    InboundMessage,
    WhatsAppDispatcher,
    get_dispatcher,
)
from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.core.security import jid_to_e164, verify_shared_secret
from backend.schemas.whatsapp import BridgeInboundMessage

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
