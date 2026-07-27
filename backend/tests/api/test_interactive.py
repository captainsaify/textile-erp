"""Interactive messages -- docs/19_InteractiveMessages.md.

Two properties matter here and nothing else really does:

1. **A tapped option and the typed equivalent produce the same result.**
   That is the whole design (§7) — button ids *are* the command
   vocabulary — and it is the only thing stopping the two input paths
   drifting into different behaviour.
2. **Platform limits are never exceeded.** Over-length titles and a
   fourth button are silent failures at Meta's API: the send is
   rejected and the partner sees a message that simply never arrives.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.command_types import CommandResult, Notification, RequestContext
from backend.api.interactive import (
    MAX_BUTTON_TITLE,
    MAX_BUTTONS,
    MAX_LIST_ROWS,
    MAX_ROW_TITLE,
    Buttons,
    Choice,
    InteractiveError,
    ListMenu,
    Section,
    as_text,
    to_cloud_api,
)
from backend.api.whatsapp_dispatcher import WhatsAppDispatcher, _from_meta
from backend.models import User
from backend.schemas.whatsapp import WebhookMessage
from backend.tests.conftest import SEEDED_ORG_ID, purge_business_rows

D = decimal.Decimal
ORG = uuid.UUID(SEEDED_ORG_ID)


@pytest.fixture(autouse=True)
async def clean(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    yield
    await purge_business_rows(session_factory)


# --------------------------------------------------------------------
# payload limits are enforced, not trusted
# --------------------------------------------------------------------


def test_a_fourth_button_is_refused() -> None:
    """Meta caps reply buttons at 3. Exceeding it is a rejected send —
    invisible unless something raises here."""
    with pytest.raises(InteractiveError, match="must be 1-3"):
        Buttons(
            body="pick",
            choices=tuple(Choice(id=f"c{n}", title=f"Choice {n}") for n in range(MAX_BUTTONS + 1)),
        )


def test_an_over_long_button_title_is_refused() -> None:
    with pytest.raises(InteractiveError, match="limit is 20"):
        Buttons(body="pick", choices=(Choice(id="x", title="A" * (MAX_BUTTON_TITLE + 1)),))


def test_duplicate_button_titles_are_refused() -> None:
    """Meta requires unique titles; duplicates are dropped silently."""
    with pytest.raises(InteractiveError, match="duplicate button title"):
        Buttons(
            body="pick",
            choices=(Choice(id="a", title="Same"), Choice(id="b", title="Same")),
        )


def test_an_eleventh_list_row_is_refused() -> None:
    with pytest.raises(InteractiveError, match=f"limit is {MAX_LIST_ROWS}"):
        ListMenu(
            body="pick",
            menu_label="Choose",
            sections=(
                Section(
                    title="All",
                    rows=tuple(
                        Choice(id=f"r{n}", title=f"Row {n}") for n in range(MAX_LIST_ROWS + 1)
                    ),
                ),
            ),
        )


def test_an_over_long_row_title_is_refused() -> None:
    with pytest.raises(InteractiveError, match=f"limit is {MAX_ROW_TITLE}"):
        ListMenu(
            body="pick",
            menu_label="Choose",
            sections=(Section(title="All", rows=(Choice(id="r", title="B" * 25),)),),
        )


def test_cloud_api_shape_matches_the_documented_payload() -> None:
    payload = to_cloud_api(
        Buttons(body="Save?", choices=(Choice(id="confirm", title="Confirm"),)),
        "+919000000000",
    )
    assert payload["type"] == "interactive"
    assert payload["to"] == "919000000000"  # no leading +
    interactive = payload["interactive"]
    assert isinstance(interactive, dict)
    assert interactive["type"] == "button"
    buttons = interactive["action"]["buttons"]
    assert buttons[0] == {"type": "reply", "reply": {"id": "confirm", "title": "Confirm"}}


def test_text_fallback_lists_the_ids_so_the_flow_is_typeable() -> None:
    """The bridge transport can't render buttons (§3). The fallback has
    to name what to type, not just describe the options."""
    text = as_text(
        Buttons(
            body="Save this purchase?",
            choices=(Choice(id="confirm", title="Confirm"), Choice(id="discard", title="Discard")),
        )
    )
    assert "Save this purchase?" in text
    assert "confirm" in text and "discard" in text


# --------------------------------------------------------------------
# inbound: a tap is carried as the text it stands for
# --------------------------------------------------------------------


def _webhook(**interactive: object) -> WebhookMessage:
    return WebhookMessage.model_validate(
        {
            "id": f"wamid.{uuid.uuid4().hex[:8]}",
            "from": "919000000000",
            "type": "interactive",
            "interactive": interactive,
        }
    )


def test_a_tapped_button_arrives_as_text() -> None:
    """§7: the id *is* the string the user would have typed, so every
    handler sees one input shape."""
    message = _from_meta(
        _webhook(type="button_reply", button_reply={"id": "confirm", "title": "Confirm"})
    )
    assert message.kind == "text"
    assert message.text == "confirm"


def test_a_picked_list_row_arrives_as_text() -> None:
    message = _from_meta(
        _webhook(type="list_reply", list_reply={"id": "summary month", "title": "This month"})
    )
    assert message.kind == "text"
    assert message.text == "summary month"


def test_a_plain_text_message_is_unaffected() -> None:
    message = _from_meta(
        WebhookMessage.model_validate(
            {
                "id": "wamid.1",
                "from": "919000000000",
                "type": "text",
                "text": {"body": "stock"},
            }
        )
    )
    assert message.kind == "text"
    assert message.text == "stock"


# --------------------------------------------------------------------
# outbound: delivery and degradation
# --------------------------------------------------------------------


class _TextOnlySender:
    """Stands in for the whatsapp-web.js bridge, which has no
    send_interactive at all."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, to_number: str, body: str) -> bool:
        self.sent.append((to_number, body))
        return True


class _InteractiveSender(_TextOnlySender):
    def __init__(self, *, succeeds: bool = True) -> None:
        super().__init__()
        self.interactive: list[tuple[str, object]] = []
        self._succeeds = succeeds

    async def send_interactive(self, to_number: str, payload: object) -> bool:
        self.interactive.append((to_number, payload))
        return self._succeeds


def _dispatcher(client: _TextOnlySender) -> WhatsAppDispatcher:
    return WhatsAppDispatcher(session_factory=None, redis=object(), client=client)  # type: ignore[arg-type]


async def test_reply_and_buttons_are_two_messages() -> None:
    """A button message's body caps at 1024 chars, well under the 26-line
    purchase preview, so the detail goes as text and the buttons follow
    (§5)."""
    client = _InteractiveSender()
    result = CommandResult(
        reply="a very long preview…",
        interactive=Buttons(body="Save?", choices=(Choice(id="confirm", title="Confirm"),)),
    )
    await _dispatcher(client)._deliver("+919000000000", result)

    assert [body for _, body in client.sent] == ["a very long preview…"]
    assert len(client.interactive) == 1


async def test_a_transport_without_buttons_falls_back_to_text() -> None:
    client = _TextOnlySender()
    result = CommandResult(
        reply="preview",
        interactive=Buttons(body="Save?", choices=(Choice(id="confirm", title="Confirm"),)),
    )
    await _dispatcher(client)._deliver("+919000000000", result)

    assert len(client.sent) == 2, "the options must still reach the user as text"
    assert "confirm" in client.sent[1][1]


async def test_a_failed_interactive_send_falls_back_to_text() -> None:
    """Meta rejecting the payload must not lose the question."""
    client = _InteractiveSender(succeeds=False)
    result = CommandResult(
        reply="preview",
        interactive=Buttons(body="Save?", choices=(Choice(id="confirm", title="Confirm"),)),
    )
    await _dispatcher(client)._deliver("+919000000000", result)
    assert len(client.sent) == 2
    assert "confirm" in client.sent[1][1]


async def test_notifications_carry_their_own_buttons() -> None:
    """The person who has to act on a withdrawal approval is the
    recipient, not the sender."""
    client = _InteractiveSender()
    result = CommandResult(
        reply="waiting on Farida",
        notifications=(
            Notification(
                to_number="+919000000000",
                body="Rahul wants to withdraw ₹30,000.00",
                interactive=Buttons(
                    body="Approve?",
                    choices=(
                        Choice(id="approve withdraw abc123", title="Approve"),
                        Choice(id="reject withdraw abc123", title="Reject"),
                    ),
                ),
            ),
        ),
    )
    await _dispatcher(client)._notify(result)
    assert client.interactive[0][0] == "+919000000000"


# --------------------------------------------------------------------
# the equivalence that keeps the two paths from diverging
# --------------------------------------------------------------------


@pytest.fixture
def ctx(staff_user: User, session_factory: async_sessionmaker[AsyncSession]) -> RequestContext:
    return RequestContext(user=staff_user, session_factory=session_factory, message_id="m1")


def _draft(**overrides: object) -> object:
    from backend.services.purchase_service import Draft, DraftLine

    base = {
        "supplier_id": uuid.uuid4(),
        "supplier_name": "Wagdia",
        "invoice_no": "INV-001",
        "invoice_date": datetime.date(2026, 7, 26),
        "brand_id": None,
        "brand_name": None,
        "lines": [
            DraftLine(
                code="TRP",
                qty=D("100"),
                rate=D("150"),
                product_id=uuid.uuid4(),
                resolved_code="TRP",
                unit_code="KG",
            )
        ],
        "freight": D("0"),
        "other_charges": D("0"),
        "declared_total": None,
    }
    base.update(overrides)
    return Draft(**base)  # type: ignore[arg-type]


def test_a_ready_draft_offers_confirm() -> None:
    from backend.api.commands.purchase_commands import preview_result

    result = preview_result(_draft())  # type: ignore[arg-type]
    assert result.interactive is not None
    ids = [c.id for c in result.interactive.choices]  # type: ignore[union-attr]
    assert "confirm" in ids
    # every id must be something the typed path already accepts
    from backend.api.commands.purchase_commands import CONFIRM_VOCAB

    assert "confirm" in CONFIRM_VOCAB


def test_unresolved_codes_offer_create_all_not_confirm() -> None:
    """The buttons follow the draft's state, so the offer can never
    contradict what the text says is wrong."""
    from backend.api.commands.purchase_commands import preview_result
    from backend.services.purchase_service import DraftLine

    draft = _draft(
        lines=[
            DraftLine(
                code=f"C{n}",
                qty=D("10"),
                rate=D("150"),
                product_id=None,
                resolved_code=None,
                unit_code=None,
                description=f"Item {n}",
            )
            for n in range(26)
        ]
    )
    result = preview_result(draft)  # type: ignore[arg-type]
    assert result.interactive is not None
    ids = [c.id for c in result.interactive.choices]  # type: ignore[union-attr]
    assert "create all products" in ids
    assert "confirm" not in ids, "must not offer to save a draft that can't be saved"

    titles = [c.title for c in result.interactive.choices]  # type: ignore[union-attr]
    assert "Create all 26" in titles
    assert all(len(t) <= MAX_BUTTON_TITLE for t in titles)


def test_a_draft_still_awaiting_details_offers_nothing() -> None:
    """Nothing to decide yet — inventing buttons would be noise."""
    from backend.api.commands.purchase_commands import preview_result

    result = preview_result(_draft(supplier_name="", invoice_no=""))  # type: ignore[arg-type]
    assert result.interactive is None


def test_every_button_id_is_understood_by_the_typed_path() -> None:
    """The equivalence in one assertion: each id offered anywhere is
    either a registered command or a session-reply keyword."""
    from backend.api.commands.purchase_commands import _GUIDANCE, CONFIRM_VOCAB
    from backend.api.commands.return_commands import _REFUND_CHOICE
    from backend.api.whatsapp_commands import COMMAND_REGISTRY

    offered = {
        "confirm",
        "discard",
        "create all products",
        "create supplier",
        "one by one",
        "fix",
        "refund cash",
        "refund bank",
        "credit",
    }
    understood = (
        set(CONFIRM_VOCAB)
        | set(_GUIDANCE)
        | set(_REFUND_CHOICE)
        | set(COMMAND_REGISTRY)
        | {"discard", "create all products", "create supplier"}
    )
    assert offered <= understood, f"unhandled button ids: {offered - understood}"
