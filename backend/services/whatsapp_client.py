"""Outbound WhatsApp Cloud API client (Meta Graph API).

Sends are retried with backoff on transient failures and never raise
into command handling -- a reply that can't be delivered is logged, and
per docs/08_WhatsApp.md §9 the underlying transaction is never rolled
back because of it. When the Celery phase lands, failed sends move to a
queued retry task per docs/11_BackgroundWorkers.md; the direct-send
contract here stays the same.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

import httpx

from backend.core.config import get_settings
from backend.core.logging import get_logger

logger = get_logger(__name__)

_RETRY_DELAYS_SECONDS = (0.5, 1.0, 2.0)


class SupportsSendText(Protocol):
    """What message-sending consumers (the dispatcher) actually need --
    lets tests substitute a recorder without a network client."""

    async def send_text(self, to_number: str, body: str) -> bool: ...


class WhatsAppClient:
    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self._phone_number_id = settings.whatsapp_phone_number_id
        self._http = http or httpx.AsyncClient(
            base_url=f"https://graph.facebook.com/{settings.whatsapp_api_version}",
            headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
            timeout=10.0,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def send_text(self, to_number: str, body: str) -> bool:
        """Send a plain text message. Returns delivery-accepted, never raises."""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_number.lstrip("+"),
            "type": "text",
            "text": {"preview_url": False, "body": body},
        }
        url = f"/{self._phone_number_id}/messages"
        for attempt, delay in enumerate((*_RETRY_DELAYS_SECONDS, None)):
            try:
                response = await self._http.post(url, json=payload)
                if response.status_code < 300:
                    return True
                # 4xx = our bug or config problem; retrying won't help
                if response.status_code < 500:
                    logger.error(
                        "whatsapp_send_rejected",
                        status=response.status_code,
                        body=response.text[:500],
                    )
                    return False
                logger.warning("whatsapp_send_5xx", status=response.status_code, attempt=attempt)
            except httpx.HTTPError as exc:
                logger.warning("whatsapp_send_transport_error", error=str(exc), attempt=attempt)
            if delay is None:
                break
            await asyncio.sleep(delay)
        logger.error("whatsapp_send_failed", to=to_number)
        return False


_client: WhatsAppClient | None = None


def get_whatsapp_client() -> WhatsAppClient:
    global _client
    if _client is None:
        _client = WhatsAppClient()
    return _client


async def close_whatsapp_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None
