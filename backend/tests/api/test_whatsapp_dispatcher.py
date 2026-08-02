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


async def test_recognised_command_mid_wizard_runs_and_abandons_the_draft(
    dispatcher: WhatsAppDispatcher,
    fake_sender: FakeSender,
    staff_user: User,
    session_factory: Any,
    redis_client: Any,
) -> None:
    """Someone who types `stock` mid-wizard wants stock, not to name it
    as their supplier (docs/20_ConversationalIntake.md §5). The wizard is
    dropped and says so -- silence would look like the answers were kept.
    """
    from backend.services.session_service import AWAITING_SLOT, IDLE, SessionService

    assert staff_user.whatsapp_number is not None
    sessions = SessionService(session_factory, redis_client)
    await sessions.set(
        staff_user.org_id,
        staff_user.id,
        AWAITING_SLOT,
        {"draft": {}, "queue": ["supplier"], "filled": {}},
    )

    await _process(dispatcher, meta_payload(text_message(staff_user.whatsapp_number, "stock")))

    reply = fake_sender.sent[0][1]
    assert "Which supplier" not in reply
    assert "dropped the half-finished purchase" in reply
    state = await sessions.get(staff_user.org_id, staff_user.id)
    assert state.state == IDLE


async def test_free_text_mid_wizard_is_an_answer_not_an_unknown_command(
    dispatcher: WhatsAppDispatcher,
    fake_sender: FakeSender,
    staff_user: User,
    session_factory: Any,
    redis_client: Any,
) -> None:
    from backend.services.session_service import AWAITING_SLOT, SessionService
    from backend.tests.api.test_conversational_intake import make_draft

    assert staff_user.whatsapp_number is not None
    sessions = SessionService(session_factory, redis_client)
    await sessions.set(
        staff_user.org_id,
        staff_user.id,
        AWAITING_SLOT,
        {
            "draft": make_draft().to_context(),
            "queue": ["supplier", "invoice_no"],
            "filled": {},
        },
    )

    await _process(
        dispatcher, meta_payload(text_message(staff_user.whatsapp_number, "Wagdia Textiles"))
    )

    reply = fake_sender.sent[0][1]
    assert "I don't recognize" not in reply
    assert "What's the invoice number?" in reply
    state = await sessions.get(staff_user.org_id, staff_user.id)
    assert state.context["filled"] == {"supplier": "Wagdia Textiles"}


# --------------------------------------------------------------------
# delivery receipts
# --------------------------------------------------------------------


def _status_payload(status: str, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": "wamid.sent-earlier",
        "recipient_id": "917000087329",
        "status": status,
        **extra,
    }
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "0",
                "changes": [
                    {
                        "field": "messages",
                        "value": {"messaging_product": "whatsapp", "statuses": [entry]},
                    }
                ],
            }
        ],
    }


async def test_a_failed_delivery_is_logged_not_swallowed(
    dispatcher: WhatsAppDispatcher, capsys: Any
) -> None:
    """Meta returns a message id for a send it merely *accepted*. Whether
    it arrived comes back minutes later as a receipt, and with those
    unread an undelivered partner notification looked exactly like a
    delivered one -- which is how three "he never got it" reports had
    nothing to investigate."""
    await _process(
        dispatcher,
        _status_payload(
            "failed",
            errors=[
                {
                    "code": 131047,
                    "title": "Re-engagement message",
                    "details": "Message failed to send because more than 24 hours "
                    "have passed since the customer last replied.",
                }
            ],
        ),
    )

    logged = capsys.readouterr().out
    assert "whatsapp_delivery_failed" in logged
    assert "131047" in logged
    assert "917000087329" in logged


async def test_ordinary_receipts_stay_quiet(dispatcher: WhatsAppDispatcher, capsys: Any) -> None:
    """Every message produces sent/delivered/read. Logging those would
    bury the one line worth reading."""
    for status in ("sent", "delivered", "read"):
        await _process(dispatcher, _status_payload(status))

    assert "whatsapp_delivery_failed" not in capsys.readouterr().out


async def test_a_receipt_never_looks_like_an_inbound_message(
    dispatcher: WhatsAppDispatcher, fake_sender: FakeSender
) -> None:
    """A status callback carries no `messages`, so nothing is replied to."""
    await _process(dispatcher, _status_payload("delivered"))
    assert fake_sender.sent == []
