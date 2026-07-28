"""Posting to the partners' WhatsApp group -- docs/22_GroupBroadcast.md.

**Meta's Cloud API cannot send to a group.** Not a gap in this code: the
API has no group messaging at all. So the group is reached through a
second number running whatsapp-web.js, and that number does exactly one
thing -- it posts what it is given.

The split matters more than it looks:

- **All interaction stays on Meta.** Commands, wizards, confirmations,
  approvals, media intake -- the official, supported, rate-limited path.
- **The relay is outbound only.** It never reads a message, never holds
  session state, never runs a command. There is nothing to exploit and
  nothing to get out of sync.

That isolation is the point. whatsapp-web.js is an unofficial client
and the number running it can be banned; the project already accepted
that risk knowingly. What this design buys is that the *consequence* of
a ban is losing group posts, not losing the ERP. Every record, every
reply and every approval keeps working on Meta.

A relay failure is therefore never allowed to fail the thing that
triggered it. A purchase that was confirmed stays confirmed even if
nobody could be told about it in the group.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import httpx

from backend.core.config import get_settings
from backend.core.lifecycle import on_release
from backend.core.logging import get_logger

logger = get_logger(__name__)

_RETRY_DELAYS_SECONDS = (1, 3)


@dataclasses.dataclass(frozen=True)
class RelayResult:
    delivered: bool
    #: Why not, in words a partner can act on -- shown to whoever asked
    #: to share, never swallowed into a log only they can't read.
    reason: str = ""


class GroupRelay:
    """Sends text and files to one configured group chat."""

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self._chat_id = settings.group_chat_id
        self._enabled = bool(settings.group_broadcast_enabled and settings.group_chat_id)
        self._http = http or httpx.AsyncClient(
            base_url=settings.bridge_url,
            headers={"X-Bridge-Secret": settings.bridge_shared_secret},
            # a media upload through the bridge is slower than a text
            timeout=60.0,
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def aclose(self) -> None:
        await self._http.aclose()

    async def send_text(self, body: str) -> RelayResult:
        if not self._enabled:
            return RelayResult(False, "Group sharing isn't switched on yet.")
        return await self._post("/send", {"chat_id": self._chat_id, "body": body})

    async def send_file(self, path: Path, *, caption: str) -> RelayResult:
        """Files go as base64 through the same endpoint rather than a
        second transport -- one thing to keep alive, not two."""
        import base64

        if not self._enabled:
            return RelayResult(False, "Group sharing isn't switched on yet.")
        try:
            data = path.read_bytes()
        except OSError as exc:
            logger.error("group_relay_unreadable_file", path=str(path), error=str(exc))
            return RelayResult(False, "I couldn't read that file to share it.")
        return await self._post(
            "/send",
            {
                "chat_id": self._chat_id,
                "body": caption,
                "media": {
                    "filename": path.name,
                    "data": base64.standard_b64encode(data).decode(),
                },
            },
        )

    async def _post(self, path: str, payload: dict[str, object]) -> RelayResult:
        """Retries transport failures, never 4xx.

        A 4xx from the bridge means the session is gone or the chat id is
        wrong -- retrying that just delays telling someone.
        """
        for attempt, delay in enumerate((*_RETRY_DELAYS_SECONDS, None)):
            try:
                response = await self._http.post(path, json=payload)
                if response.status_code < 300:
                    return RelayResult(True)
                if response.status_code < 500:
                    logger.error(
                        "group_relay_rejected",
                        status=response.status_code,
                        body=response.text[:300],
                    )
                    return RelayResult(
                        False,
                        "The group relay rejected that — its WhatsApp session may need "
                        "scanning again.",
                    )
                logger.warning("group_relay_5xx", status=response.status_code, attempt=attempt)
            except httpx.HTTPError as exc:
                logger.warning("group_relay_transport_error", error=str(exc), attempt=attempt)
            if delay is None:
                break
            import asyncio

            await asyncio.sleep(delay)
        logger.error("group_relay_failed")
        return RelayResult(False, "The group relay isn't reachable right now.")


_relay: GroupRelay | None = None


def get_group_relay() -> GroupRelay:
    global _relay
    if _relay is None:
        _relay = GroupRelay()
    return _relay


@on_release
async def close_group_relay() -> None:
    """Registered so a Celery task never inherits a client bound to a
    dead event loop -- see backend/core/lifecycle.py."""
    global _relay
    relay, _relay = _relay, None
    if relay is not None:
        await relay.aclose()
