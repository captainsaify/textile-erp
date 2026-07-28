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
from pathlib import Path
from typing import Protocol

import httpx

from backend.api.interactive import Interactive, to_cloud_api
from backend.core.config import get_settings
from backend.core.lifecycle import on_release
from backend.core.logging import get_logger

logger = get_logger(__name__)

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_RETRY_DELAYS_SECONDS = (0.5, 1.0, 2.0)


class SupportsSendText(Protocol):
    """What message-sending consumers (the dispatcher) actually need --
    lets tests substitute a recorder without a network client."""

    async def send_text(self, to_number: str, body: str) -> bool: ...


class SupportsSendInteractive(Protocol):
    """Cloud API only. The dispatcher checks for this at runtime and
    falls back to send_text, so the bridge transport needs no changes
    (docs/19_InteractiveMessages.md §3)."""

    async def send_interactive(self, to_number: str, payload: Interactive) -> bool: ...


class SupportsSendDocument(Protocol):
    """Cloud API only. Checked at runtime, like send_interactive, so the
    bridge transport needs no changes."""

    async def send_document(
        self, to_number: str, path: Path, *, filename: str, caption: str
    ) -> bool: ...


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

    async def send_interactive(self, to_number: str, payload: Interactive) -> bool:
        """Send buttons or a list menu. Same retry semantics as text."""
        return await self._post(to_cloud_api(payload, to_number))

    async def send_document(
        self, to_number: str, path: Path, *, filename: str, caption: str
    ) -> bool:
        """Deliver a file into the chat. Two steps, per Meta's API: upload
        the bytes to get a media id, then send a message referencing it.

        The alternative -- a public `link` -- would mean exposing report
        data on an unauthenticated URL, which is not a trade worth making
        for a file the recipient can already be handed directly.
        """
        media_id = await self._upload(path, filename)
        if media_id is None:
            return False
        return await self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to_number.lstrip("+"),
                "type": "document",
                "document": {"id": media_id, "filename": filename, "caption": caption[:1024]},
            }
        )

    async def _upload(self, path: Path, filename: str) -> str | None:
        """Returns Meta's media id, or None -- the caller falls back to
        telling the user in text rather than failing the whole job."""
        try:
            data = path.read_bytes()
        except OSError as exc:
            logger.error("media_upload_unreadable", path=str(path), error=str(exc))
            return None
        try:
            response = await self._http.post(
                f"/{self._phone_number_id}/media",
                data={"messaging_product": "whatsapp", "type": _XLSX_MIME},
                files={"file": (filename, data, _XLSX_MIME)},
            )
        except httpx.HTTPError as exc:
            logger.error("media_upload_transport_error", error=str(exc))
            return None
        if response.status_code >= 300:
            logger.error(
                "media_upload_rejected", status=response.status_code, body=response.text[:500]
            )
            return None
        media_id = response.json().get("id")
        return str(media_id) if media_id else None

    async def send_text(self, to_number: str, body: str) -> bool:
        """Send a plain text message. Returns delivery-accepted, never raises."""
        return await self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to_number.lstrip("+"),
                "type": "text",
                "text": {"preview_url": False, "body": body},
            }
        )

    async def _post(self, payload: dict[str, object]) -> bool:
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
        logger.error("whatsapp_send_failed", to=payload.get("to"))
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


@on_release
async def close_whatsapp_client() -> None:
    """Cleared even if closing fails: a client bound to a dead loop must
    not be handed to the next caller."""
    global _client
    client, _client = _client, None
    if client is not None:
        await client.aclose()
