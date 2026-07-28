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

import dataclasses
import time
import uuid
from dataclasses import dataclass

import redis.asyncio as aioredis
from redis.exceptions import LockError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.commands.purchase_commands import handle_purchase_session_reply
from backend.api.interactive import as_text
from backend.api.whatsapp_commands import (
    COMMAND_REGISTRY,
    CommandResult,
    CommandSpec,
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
from backend.schemas.whatsapp import WebhookMedia, WebhookMessage, WebhookPayload
from backend.services.whatsapp_bridge_client import get_bridge_sender
from backend.services.whatsapp_client import SupportsSendText, get_whatsapp_client

logger = get_logger(__name__)

_DEDUP_TTL_SECONDS = 24 * 60 * 60  # docs/08_WhatsApp.md §3, transport layer
_RATE_WINDOW_SECONDS = 60
_LOCK_TIMEOUT_SECONDS = 30
_LOCK_WAIT_SECONDS = 5

#: Commands that *continue* an intake wizard instead of abandoning it:
#: `details ...` fills every remaining slot in one message, which is the
#: same wizard reaching the same draft (docs/20 §5, §12).
_WIZARD_CONTINUATIONS = frozenset({"details"})

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
                    media = message.media
                    if media is not None:
                        await self._process_meta_media(message, media)
                        continue
                    await self.process_inbound(_from_meta(message))

    async def _process_meta_media(self, message: WebhookMessage, media: WebhookMedia) -> None:
        """Meta carries media by reference: fetch the bytes, then hand to
        the same OCR path the bridge transport uses."""
        sender = normalize_whatsapp_number(message.from_number)
        fetcher = getattr(self._client, "fetch_media", None)
        if fetcher is None:
            await self.process_inbound(_from_meta(message))
            return

        fetched = await fetcher(media.id)
        if fetched is None:
            logger.error("media_fetch_failed", media_id=media.id, message_id=message.id)
            await self._client.send_text(
                sender, "❌ I couldn't download that file from WhatsApp — please resend it."
            )
            return

        data, mime_type = fetched
        await self.process_media(
            InboundMedia(
                message_id=message.id,
                sender_number=sender,
                reply_to=sender,
                mime_type=media.mime_type or mime_type,
                data=data,
            )
        )

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
            await self._deliver(message.reply_to, reply)
            await self._notify(reply)

    async def _deliver(self, to_number: str, result: CommandResult) -> None:
        """`reply` always goes out; the interactive payload follows only
        if the transport can render it. Two messages, not one: a button
        message's body caps at 1024 chars, which several replies exceed
        (docs/19 §5), and truncating would hide the very line items the
        user is being asked to check."""
        if result.reply.strip():
            await self._client.send_text(to_number, result.reply)
        if result.interactive is None:
            return
        sender = getattr(self._client, "send_interactive", None)
        if sender is None:
            # bridge transport: the options are already listed as text
            await self._client.send_text(to_number, as_text(result.interactive))
            return
        if not await sender(to_number, result.interactive):
            await self._client.send_text(to_number, as_text(result.interactive))

    async def _notify(self, result: CommandResult) -> None:
        """Fan out to third parties (the dual-approval request in
        docs/06_Accounting.md §8). The transaction is already committed
        by now, so a send failure is logged, never raised -- an
        unreachable partner must not look like a failed withdrawal."""
        for note in result.notifications:
            try:
                await self._deliver(
                    note.to_number,
                    CommandResult(reply=note.body, interactive=note.interactive),
                )
            except Exception as exc:  # noqa: BLE001 -- best-effort fan-out
                logger.error("notification_send_failed", to=note.to_number, error=str(exc))

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
            user=user,
            session_factory=self._session_factory,
            message_id=media.message_id,
            ack=lambda body: self._client.send_text(media.reply_to, body),
        )
        logger.info(
            "whatsapp_media",
            user_id=str(user.id),
            org_id=str(user.org_id),
            mime_type=media.mime_type,
            bytes=len(media.data),
        )
        result = await process_purchase_photo(
            media.data, media.mime_type, media.message_id, context
        )
        await self._deliver(media.reply_to, result)

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
            ack=lambda body: self._client.send_text(message.reply_to, body),
        )

        from backend.api.commands import wizards
        from backend.api.commands.intake_commands import handle_intent_reply, handle_slot_reply
        from backend.api.commands.share_commands import handle_share_reply
        from backend.services.session_service import (
            AWAITING_COMMAND_SLOT,
            AWAITING_INTENT,
            AWAITING_PURCHASE_CONFIRMATION,
            AWAITING_RETURN_REFUND_CHOICE,
            AWAITING_SALE_CONFIRMATION,
            AWAITING_SETTLEMENT_CONFIRMATION,
            AWAITING_SHARE_CHOICE,
            AWAITING_SLOT,
            IDLE,
            SessionService,
        )

        sessions = SessionService(self._session_factory, self._redis)
        session_state = await sessions.get(user.org_id, user.id)

        # An intake wizard answers first, because most answers are free
        # text -- but a *recognised* command still wins, so nobody is
        # trapped in a mode (docs/20_ConversationalIntake.md §5).
        if session_state.state == AWAITING_INTENT and spec is None:
            return await handle_intent_reply(text, context, session_state)
        if session_state.state == AWAITING_SLOT and (
            spec is None or keyword in _WIZARD_CONTINUATIONS
        ):
            return await handle_slot_reply(text, context, session_state)
        if session_state.state == AWAITING_COMMAND_SLOT and spec is None:
            return await wizards.handle_reply(text, context, session_state)
        if session_state.state == AWAITING_SHARE_CHOICE and spec is None:
            return await handle_share_reply(text, context, session_state)

        if spec is None:
            # not a command: an active session interprets it as a reply in
            # the current flow -- docs/08_WhatsApp.md §5
            from backend.api.commands.return_commands import handle_return_session_reply
            from backend.api.commands.sale_commands import handle_sale_session_reply
            from backend.api.commands.settlement_commands import (
                handle_settlement_session_reply,
            )

            if session_state.state == AWAITING_PURCHASE_CONFIRMATION:
                return await handle_purchase_session_reply(text, context, session_state)
            if session_state.state == AWAITING_SALE_CONFIRMATION:
                return await handle_sale_session_reply(text, context, session_state)
            if session_state.state == AWAITING_SETTLEMENT_CONFIRMATION:
                return await handle_settlement_session_reply(text, context, session_state)
            if session_state.state == AWAITING_RETURN_REFUND_CHOICE:
                return await handle_return_session_reply(text, context, session_state)
            suggestion = closest_command(keyword, user.role)
            hint = f" Did you mean '{suggestion}'?" if suggestion else ""
            return CommandResult(
                reply=f"I don't recognize '{keyword}'.{hint} Send 'help' to see commands."
            )
        if not role_at_least(user.role, spec.min_role):
            return CommandResult(reply=f"You don't have permission to use '{spec.name}'.")

        if session_state.state in {AWAITING_INTENT, AWAITING_SLOT, AWAITING_COMMAND_SLOT}:
            # abandon, and say so -- silently dropping a half-answered
            # entry would look like the answers were saved
            abandoning = (
                "purchase"
                if session_state.state != AWAITING_COMMAND_SLOT
                else str(session_state.context.get("command", "entry"))
            )
            await sessions.set(user.org_id, user.id, IDLE, {})
            logger.info(
                "whatsapp_command",
                command=spec.name,
                user_id=str(user.id),
                org_id=str(user.org_id),
                message_id=message.message_id,
            )
            abandoned = await self._run(spec, args, context)
            return dataclasses.replace(
                abandoned,
                reply=f"{abandoned.reply}\n\n_(I've dropped the half-finished "
                f"{abandoning} — start it again when you're ready.)_",
            )

        logger.info(
            "whatsapp_command",
            command=spec.name,
            user_id=str(user.id),
            org_id=str(user.org_id),
            message_id=message.message_id,
        )
        result = await self._run(spec, args, context)
        return await self._offer_sharing(spec, result, context)

    @staticmethod
    async def _offer_sharing(
        spec: CommandSpec, result: CommandResult, context: RequestContext
    ) -> CommandResult:
        """A result worth sharing ends with one button
        (docs/22_GroupBroadcast.md §4). The text is parked in the session
        so tapping it shares what was shown, not a fresh query."""
        from backend.api.commands import share_commands
        from backend.services.group_relay import get_group_relay

        if not spec.shareable or result.interactive is not None:
            return result
        if not get_group_relay().enabled:
            # no relay configured: don't offer something that can't work
            return result
        await share_commands.remember(result, context)
        return dataclasses.replace(result, interactive=share_commands.offer(result))

    @staticmethod
    async def _run(spec: CommandSpec, args: str, context: RequestContext) -> CommandResult:
        """A partial command is a question, not an error
        (docs/20_ConversationalIntake.md §7). A *complete* one still runs
        in one shot, untouched -- for someone fluent that is one round
        trip instead of four."""
        from backend.api.commands import wizards

        wizard = wizards.WIZARDS.get(spec.name)
        if wizard is not None:
            started = await wizards.start(wizard, args.strip(), context)
            if started is not None:
                return started
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
    """A tapped button or picked list row is carried as *text* -- its id
    is the string the user would have typed (docs/19 §7). Every handler
    and every existing test therefore sees one input shape, and the
    tapped and typed paths cannot drift apart."""
    sender = normalize_whatsapp_number(message.from_number)
    choice = message.choice_id
    body = choice if choice is not None else (message.text.body if message.text else None)
    return InboundMessage(
        message_id=message.id,
        sender_number=sender,
        reply_to=sender,
        kind="text" if body is not None else message.type,
        text=body,
    )


def get_dispatcher() -> WhatsAppDispatcher:
    """FastAPI dependency; overridden in tests."""
    return WhatsAppDispatcher()
