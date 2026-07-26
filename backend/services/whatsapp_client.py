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


class SupportsFetchMedia(Protocol):
    """Transports that carry media by reference rather than by value."""

    async def fetch_media(self, media_id: str) -> tuple[bytes, str] | None: ...


class WhatsAppClient:
    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self._phone_number_id = settings.whatsapp_phone_number_id
        self._access_token = settings.whatsapp_access_token
        self._http = http or httpx.AsyncClient(
            base_url=f"https://graph.facebook.com/{settings.whatsapp_api_version}",
            headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
            # media downloads are larger than a text send
            timeout=60.0,
        )

    async def fetch_media(self, media_id: str) -> tuple[bytes, str] | None:
        return await _fetch_media_impl(self._http, self._access_token, media_id)

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


async def _fetch_media_impl(
    http: httpx.AsyncClient, access_token: str, media_id: str
) -> tuple[bytes, str] | None:
    """Two-step per Meta's API: resolve the media id to a short-lived
    lookaside URL, then download it with the same bearer token. Returns
    (bytes, mime_type), or None on any failure -- the caller turns that
    into a user-facing 'couldn't download' message."""
    try:
        meta_response = await http.get(f"/{media_id}")
        if meta_response.status_code >= 300:
            logger.error(
                "media_lookup_failed",
                media_id=media_id,
                status=meta_response.status_code,
                body=meta_response.text[:300],
            )
            return None
        payload = meta_response.json()
        url = payload.get("url")
        mime_type = payload.get("mime_type") or "application/octet-stream"
        if not url:
            logger.error("media_lookup_no_url", media_id=media_id)
            return None

        # The lookaside URL is absolute and outside the client's base_url,
        # and still requires the bearer token.
        download = await http.get(
            url, headers={"Authorization": f"Bearer {access_token}"}, follow_redirects=True
        )
        if download.status_code >= 300:
            logger.error("media_download_failed", media_id=media_id, status=download.status_code)
            return None
        return download.content, str(mime_type)
    except httpx.HTTPError as exc:
        logger.error("media_transport_error", media_id=media_id, error=str(exc))
        return None


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
