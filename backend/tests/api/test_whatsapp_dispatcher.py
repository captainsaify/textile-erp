"""Dispatcher pipeline behaviour -- docs/08_WhatsApp.md §1-§3."""

from __future__ import annotations

import uuid
from typing import Any

from backend.api import whatsapp_commands
from backend.api.whatsapp_dispatcher import (
    UNSUPPORTED_MEDIA_REPLY,
    WhatsAppDispatcher,
)
from backend.models import User
from backend.models.enums import UserRole
from backend.schemas.whatsapp import WebhookPayload
from backend.tests.api.conftest import FakeSender, meta_payload, text_message


async def _process(dispatcher: WhatsAppDispatcher, payload: dict[str, Any]) -> None:
    await dispatcher.process_webhook(WebhookPayload.model_validate(payload))


async def test_known_sender_gets_help_reply(
    dispatcher: WhatsAppDispatcher, fake_sender: FakeSender, staff_user: User
) -> None:
    assert staff_user.whatsapp_number is not None
    await _process(dispatcher, meta_payload(text_message(staff_user.whatsapp_number, "help")))
    to, body = fake_sender.sent[0]
    assert to == staff_user.whatsapp_number
    assert "help" in body
    assert "Available commands" in body
    # the menu follows as a second message; FakeSender can't render
    # buttons, so it arrives as the text fallback (docs/19 §3)
    assert len(fake_sender.sent) == 2
    assert "What would you like to do?" in fake_sender.sent[1][1]


async def test_unknown_sender_gets_no_reply_at_all(
    dispatcher: WhatsAppDispatcher, fake_sender: FakeSender
) -> None:
    stranger = f"+9990{uuid.uuid4().hex[:8]}"
    await _process(dispatcher, meta_payload(text_message(stranger, "help")))
    assert fake_sender.sent == []


async def test_duplicate_delivery_processed_once(
    dispatcher: WhatsAppDispatcher, fake_sender: FakeSender, staff_user: User
) -> None:
    assert staff_user.whatsapp_number is not None
    message = text_message(staff_user.whatsapp_number, "help")
    await _process(dispatcher, meta_payload(message))
    after_first = len(fake_sender.sent)
    await _process(dispatcher, meta_payload(message))
    # counting sends rather than asserting exactly one: `help` also
    # sends its menu, and what this test is about is that a redelivered
    # message adds nothing at all
    assert len(fake_sender.sent) == after_first


async def test_unknown_command_gets_suggestion(
    dispatcher: WhatsAppDispatcher, fake_sender: FakeSender, staff_user: User
) -> None:
    assert staff_user.whatsapp_number is not None
    await _process(dispatcher, meta_payload(text_message(staff_user.whatsapp_number, "hlep")))
    assert len(fake_sender.sent) == 1
    assert "Did you mean 'help'?" in fake_sender.sent[0][1]


async def test_non_text_message_politely_rejected(
    dispatcher: WhatsAppDispatcher, fake_sender: FakeSender, staff_user: User
) -> None:
    assert staff_user.whatsapp_number is not None
    audio = {
        "id": f"wamid.{uuid.uuid4().hex}",
        "from": staff_user.whatsapp_number.lstrip("+"),
        "timestamp": "1753500000",
        "type": "audio",
    }
    await _process(dispatcher, meta_payload(audio))
    assert fake_sender.sent == [(staff_user.whatsapp_number, UNSUPPORTED_MEDIA_REPLY)]


async def test_command_above_role_is_denied_and_hidden_from_help(
    dispatcher: WhatsAppDispatcher, fake_sender: FakeSender, staff_user: User
) -> None:
    assert staff_user.whatsapp_number is not None

    async def owner_only(
        args: str, ctx: whatsapp_commands.RequestContext
    ) -> whatsapp_commands.CommandResult:
        return whatsapp_commands.CommandResult(reply="secret")

    spec = whatsapp_commands.CommandSpec(
        name="settings",
        syntax="settings <key> <value>",
        min_role=UserRole.OWNER,
        handler=owner_only,
        help_text="Owner-only settings.",
    )
    whatsapp_commands.COMMAND_REGISTRY["settings"] = spec
    try:
        await _process(
            dispatcher, meta_payload(text_message(staff_user.whatsapp_number, "settings x 1"))
        )
        assert "don't have permission" in fake_sender.sent[-1][1]

        await _process(dispatcher, meta_payload(text_message(staff_user.whatsapp_number, "help")))
        assert "settings" not in fake_sender.sent[-1][1]
    finally:
        del whatsapp_commands.COMMAND_REGISTRY["settings"]


async def test_help_on_specific_command(
    dispatcher: WhatsAppDispatcher, fake_sender: FakeSender, staff_user: User
) -> None:
    assert staff_user.whatsapp_number is not None
    await _process(dispatcher, meta_payload(text_message(staff_user.whatsapp_number, "help help")))
    assert "Syntax: help [command]" in fake_sender.sent[0][1]


async def test_delivery_statuses_are_ignored(
    dispatcher: WhatsAppDispatcher, fake_sender: FakeSender
) -> None:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "123456",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "statuses": [{"id": "wamid.x", "status": "delivered"}],
                        },
                    }
                ],
            }
        ],
    }
    await _process(dispatcher, payload)
    assert fake_sender.sent == []
