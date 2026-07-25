"""WhatsApp command dispatcher -- the API-layer pipeline from
docs/08_WhatsApp.md §1-§3: transport dedup, sender resolution, rate
limiting, per-user serialization, then registry dispatch. No domain
decisions happen here (docs/17_CodingStandards.md §5); handlers call
services.

Transport-neutral: Meta Cloud API webhooks and the whatsapp-web.js
bridge both map into InboundMessage. `reply_to` is whatever chat the
answer belongs in -- the sender's own number for 1:1, the group chat id
for group messages -- while `sender_number` is always the individual
person, so RBAC and audit attribution are per-user even in a group.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import redis.asyncio as aioredis
from redis.exceptions import LockError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.commands.purchase_commands import handle_purchase_session_reply
from backend.api.whatsapp_commands import (
    COMMAND_REGISTRY,
    CommandResult,
    RequestContext,
    closest_command,
)
from backend.core.config import get_settings
from backend.core.db import get_session_factory
from backend.core.logging import get_logger
from backend.core.redis import get_redis
from backend.core.security import normalize_whatsapp_number, role_at_least
from backend.models import User
from backend.repositories.user_repository import UserRepository
from backend.schemas.whatsapp import WebhookMessage, WebhookPayload
from backend.services.whatsapp_bridge_client import get_bridge_sender
from backend.services.whatsapp_client import SupportsSendText, get_whatsapp_client

logger = get_logger(__name__)

_DEDUP_TTL_SECONDS = 24 * 60 * 60  # docs/08_WhatsApp.md §3, transport layer
_RATE_WINDOW_SECONDS = 60
_LOCK_TIMEOUT_SECONDS = 30
_LOCK_WAIT_SECONDS = 5

UNSUPPORTED_MEDIA_REPLY = "I can only read text commands for now. Send 'help' to see what I can do."
BUSY_REPLY = "⏳ Still working on your previous message — try again in a moment."
THROTTLE_REPLY = "Sending a lot of messages at once — please slow down a little."


@dataclass(frozen=True)
class InboundMessage:
    message_id: str
    sender_number: str  # E.164 of the individual person who wrote it
    reply_to: str  # transport-specific chat address replies go to
    kind: str  # "text" or a media/other type name
    text: str | None


@dataclass(frozen=True)
class InboundMedia:
    """A photo/PDF: the OCR purchase path (docs/07_OCR.md)."""

    message_id: str
    sender_number: str
    reply_to: str
    mime_type: str
    data: bytes


class WhatsAppDispatcher:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        redis: aioredis.Redis | None = None,
        client: SupportsSendText | None = None,
    ) -> None:
        self._session_factory = session_factory or get_session_factory()
        self._redis = redis or get_redis()
        self._client = client or _default_sender()

    async def process_webhook(self, payload: WebhookPayload) -> None:
        """Meta Cloud API entrypoint; 1:1 only, so replies go to the sender."""
        for entry in payload.entry:
            for change in entry.changes:
                for message in change.value.messages:
                    await self.process_inbound(_from_meta(message))

    async def process_inbound(self, message: InboundMessage) -> None:
        if not await self._first_delivery(message.message_id):
            logger.info("whatsapp_duplicate_delivery", message_id=message.message_id)
            return

        sender = message.sender_number
        async with self._session_factory() as session:
            user = await UserRepository(session).get_active_by_whatsapp_number(sender)

        if user is None:
            # deliberate silence -- docs/08_WhatsApp.md §2: strangers get no
            # reply at all, only a log line for security review
            logger.warning(
                "unauthorized_sender", whatsapp_number=sender, message_id=message.message_id
            )
            return

        if await self._over_rate_limit(sender):
            if await self._first_throttle_notice(sender):
                await self._client.send_text(message.reply_to, THROTTLE_REPLY)
            return

        lock = self._redis.lock(
            f"wa:lock:{user.org_id}:{user.id}",
            timeout=_LOCK_TIMEOUT_SECONDS,
            blocking_timeout=_LOCK_WAIT_SECONDS,
        )
        try:
            async with lock:
                reply = await self._handle(message, user)
        except LockError:
            logger.warning("whatsapp_user_lock_contention", user_id=str(user.id))
            await self._client.send_text(message.reply_to, BUSY_REPLY)
            return
        if reply is not None:
            await self._client.send_text(message.reply_to, reply.reply)

    async def process_media(self, media: InboundMedia) -> None:
        if not await self._first_delivery(media.message_id):
            logger.info("whatsapp_duplicate_delivery", message_id=media.message_id)
            return

        async with self._session_factory() as session:
            user = await UserRepository(session).get_active_by_whatsapp_number(media.sender_number)
        if user is None:
            logger.warning(
                "unauthorized_sender",
                whatsapp_number=media.sender_number,
                message_id=media.message_id,
            )
            return

        from backend.api.commands.ocr_commands import process_purchase_photo

        context = RequestContext(
            user=user, session_factory=self._session_factory, message_id=media.message_id
        )
        logger.info(
            "whatsapp_media",
            user_id=str(user.id),
            org_id=str(user.org_id),
            mime_type=media.mime_type,
            bytes=len(media.data),
        )
        await self._client.send_text(media.reply_to, "📸 Reading your sheet, one moment…")
        result = await process_purchase_photo(
            media.data, media.mime_type, media.message_id, context
        )
        await self._client.send_text(media.reply_to, result.reply)

    async def _handle(self, message: InboundMessage, user: User) -> CommandResult | None:
        if message.kind != "text" or message.text is None:
            return CommandResult(reply=UNSUPPORTED_MEDIA_REPLY)

        text = message.text.strip()
        keyword, _, args = text.partition(" ")
        keyword = keyword.lower()
        spec = COMMAND_REGISTRY.get(keyword)
        context = RequestContext(
            user=user,
            session_factory=self._session_factory,
            message_id=message.message_id,
        )

        if spec is None:
            # not a command: an active session interprets it as a reply in
            # the current flow -- docs/08_WhatsApp.md §5
            from backend.services.session_service import (
                AWAITING_PURCHASE_CONFIRMATION,
                SessionService,
            )

            session_state = await SessionService(self._session_factory, self._redis).get(
                user.org_id, user.id
            )
            if session_state.state == AWAITING_PURCHASE_CONFIRMATION:
                return await handle_purchase_session_reply(text, context, session_state)
            suggestion = closest_command(keyword, user.role)
            hint = f" Did you mean '{suggestion}'?" if suggestion else ""
            return CommandResult(
                reply=f"I don't recognize '{keyword}'.{hint} Send 'help' to see commands."
            )
        if not role_at_least(user.role, spec.min_role):
            return CommandResult(reply=f"You don't have permission to use '{spec.name}'.")

        logger.info(
            "whatsapp_command",
            command=spec.name,
            user_id=str(user.id),
            org_id=str(user.org_id),
            message_id=message.message_id,
        )
        return await spec.handler(args.strip(), context)

    async def _first_delivery(self, message_id: str) -> bool:
        return bool(
            await self._redis.set(f"wa:msg:{message_id}", "1", nx=True, ex=_DEDUP_TTL_SECONDS)
        )

    async def _over_rate_limit(self, sender: str) -> bool:
        limit = get_settings().whatsapp_rate_limit_per_minute
        key = f"wa:rl:{sender}"
        now = time.time()
        pipe = self._redis.pipeline()
        pipe.zremrangebyscore(key, 0, now - _RATE_WINDOW_SECONDS)
        pipe.zadd(key, {f"{now}:{uuid.uuid4().hex}": now})
        pipe.zcard(key)
        pipe.expire(key, _RATE_WINDOW_SECONDS * 2)
        _, _, count, _ = await pipe.execute()
        return int(count) > limit

    async def _first_throttle_notice(self, sender: str) -> bool:
        return bool(
            await self._redis.set(f"wa:rl:notice:{sender}", "1", nx=True, ex=_RATE_WINDOW_SECONDS)
        )


def _default_sender() -> SupportsSendText:
    if get_settings().whatsapp_transport == "webjs":
        return get_bridge_sender()
    return get_whatsapp_client()


def _from_meta(message: WebhookMessage) -> InboundMessage:
    sender = normalize_whatsapp_number(message.from_number)
    return InboundMessage(
        message_id=message.id,
        sender_number=sender,
        reply_to=sender,
        kind=message.type,
        text=message.text.body if message.text is not None else None,
    )


def get_dispatcher() -> WhatsAppDispatcher:
    """FastAPI dependency; overridden in tests."""
    return WhatsAppDispatcher()
