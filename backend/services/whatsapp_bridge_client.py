"""Outbound sender for the whatsapp-web.js bridge transport.

Same contract as the Meta client (SupportsSendText): retried on
transient failure, never raises into command handling. `to_number` is a
web.js chat id (`...@c.us` / group `...@g.us`); a plain E.164 value is
accepted too and translated by the bridge.
"""

from __future__ import annotations

import asyncio

import httpx

from backend.core.config import get_settings
from backend.core.logging import get_logger

logger = get_logger(__name__)

_RETRY_DELAYS_SECONDS = (0.5, 1.0, 2.0)


class WhatsAppBridgeSender:
    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self._http = http or httpx.AsyncClient(
            base_url=settings.bridge_url,
            headers={"X-Bridge-Secret": settings.bridge_shared_secret},
            timeout=15.0,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def send_text(self, to_number: str, body: str) -> bool:
        payload = {"chat_id": to_number, "body": body}
        for attempt, delay in enumerate((*_RETRY_DELAYS_SECONDS, None)):
            try:
                response = await self._http.post("/send", json=payload)
                if response.status_code < 300:
                    return True
                if response.status_code < 500:
                    logger.error(
                        "bridge_send_rejected",
                        status=response.status_code,
                        body=response.text[:500],
                    )
                    return False
                logger.warning("bridge_send_5xx", status=response.status_code, attempt=attempt)
            except httpx.HTTPError as exc:
                logger.warning("bridge_send_transport_error", error=str(exc), attempt=attempt)
            if delay is None:
                break
            await asyncio.sleep(delay)
        logger.error("bridge_send_failed", chat_id=to_number)
        return False


_sender: WhatsAppBridgeSender | None = None


def get_bridge_sender() -> WhatsAppBridgeSender:
    global _sender
    if _sender is None:
        _sender = WhatsAppBridgeSender()
    return _sender


async def close_bridge_sender() -> None:
    global _sender
    if _sender is not None:
        await _sender.aclose()
    _sender = None
