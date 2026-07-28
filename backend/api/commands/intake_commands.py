"""Conversational intake -- docs/20_ConversationalIntake.md.

A photo becomes a posted purchase by answering questions, not by
learning a template. Two states drive it:

- `AWAITING_INTENT` — what is this photo? Asked *before* OCR runs, so a
  mis-sent picture never spends a vision call, and so the extraction
  can be told what it is reading (§2).
- `AWAITING_SLOT` — one question per missing field, in order.

The slot machine lives here rather than in a service because it is
conversational state, which is what this layer already owns (§10). What
it produces is exactly the `Draft` the `details` command produces, and
it hands off to the same preview and CONFIRM.
"""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Callable
from typing import Any

from backend.api.amounts import parse_amount
from backend.api.command_types import CommandResult, RequestContext
from backend.api.interactive import Buttons, Choice, ListMenu, Section
from backend.core.exceptions import DomainError, ValidationError
from backend.services.purchase_service import Draft
from backend.services.session_service import (
    AWAITING_PURCHASE_CONFIRMATION,
    AWAITING_SLOT,
    IDLE,
    SessionService,
    SessionState,
)

#: How many recent suppliers to offer before falling back to typing.
#: One row is reserved for "Someone new", and a list caps at 10.
SUPPLIER_ROWS = 9


# --------------------------------------------------------------------
# intent
# --------------------------------------------------------------------


def ask_intent() -> CommandResult:
    """The choices carry no attachment id: the photo being asked about is
    the one held in the session, and a stale button from an older photo
    must not silently read a different sheet."""
    return CommandResult(
        reply="Got your photo. What is it?",
        interactive=Buttons(
            body="What should I do with this photo?",
            choices=(
                Choice(id="intake purchase", title="A purchase"),
                Choice(id="intake sale", title="A sale"),
                Choice(id="intake cancel", title="Neither"),
            ),
        ),
    )


async def handle_intent_reply(text: str, ctx: RequestContext, state: SessionState) -> CommandResult:
    choice = text.strip().lower().removeprefix("intake ").strip()
    sessions = SessionService(ctx.session_factory)
    attachment_id = str(state.context.get("attachment_id", ""))

    if choice in {"cancel", "neither", "something else"}:
        await sessions.set(ctx.user.org_id, ctx.user.id, IDLE, {})
        return CommandResult(reply="No problem — I've left that photo alone.")

    if choice == "sale":
        # Honest rather than half-built: reading a *sales* sheet needs its
        # own column template and stock checks, which don't exist yet.
        await sessions.set(ctx.user.org_id, ctx.user.id, IDLE, {})
        return CommandResult(
            reply="I can't read a sales sheet yet — only purchase sheets.\n"
            "Record it with:\n*sale <customer> <CODE> <qty> <rate>*"
        )

    if choice != "purchase":
        return ask_intent()

    from backend.api.commands.ocr_commands import read_stored_sheet

    return await read_stored_sheet(attachment_id, ctx)


# --------------------------------------------------------------------
# slots
# --------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SlotSpec:
    """One missing field: how to ask for it, and how to apply the answer.

    `apply` mutates the draft rather than returning a new one, matching
    how every other correction path in the purchase flow already works.
    """

    name: str
    question: str
    apply: Callable[[Draft, str], None]
    #: free-text answers get an example; choice slots build theirs at
    #: ask time because the options come from the database
    example: str = ""


def _apply_supplier(draft: Draft, value: str) -> None:
    draft.supplier_name = value.strip()
    draft.supplier_id = None  # re-resolved against the catalogue later


def _apply_invoice_no(draft: Draft, value: str) -> None:
    draft.invoice_no = value.strip()


def _apply_invoice_date(draft: Draft, value: str) -> None:
    text = value.strip().lower()
    today = datetime.date.today()
    if text == "today":
        draft.invoice_date = today
        return
    if text == "yesterday":
        draft.invoice_date = today - datetime.timedelta(days=1)
        return
    try:
        draft.invoice_date = datetime.datetime.strptime(text, "%d-%m-%Y").date()
    except ValueError:
        raise ValidationError(
            f"'{value.strip()}' isn't a date I can read. Use DD-MM-YYYY, e.g. 26-07-2026."
        ) from None


def _apply_rate(draft: Draft, value: str) -> None:
    rate = parse_amount(value, field="Rate")
    for line in draft.lines:
        if line.rate == 0:
            line.rate = rate


SLOTS: dict[str, SlotSpec] = {
    "supplier": SlotSpec(
        name="supplier",
        question="Which supplier is this from?",
        apply=_apply_supplier,
        example="e.g. Wagdia",
    ),
    "invoice_no": SlotSpec(
        name="invoice_no",
        question="What's the invoice number?",
        apply=_apply_invoice_no,
        example="e.g. INV-001",
    ),
    "invoice_date": SlotSpec(
        name="invoice_date",
        question="What date is on the invoice?",
        apply=_apply_invoice_date,
        example="e.g. 26-07-2026",
    ),
    "purchase_rate": SlotSpec(
        name="purchase_rate",
        question="What rate per unit did you pay?",
        apply=_apply_rate,
        example="e.g. 150",
    ),
}

#: Asked in this order -- who, then which invoice, then when, then how
#: much. It follows how the question would be asked out loud.
SLOT_ORDER = ("supplier", "invoice_no", "invoice_date", "purchase_rate")


def missing_slots(draft: Draft, *, date_known: bool = True) -> list[str]:
    """Gap analysis (§3). The vision engine returns empty strings for
    fields a sheet didn't carry, so "what's missing" needs no new
    detection -- it's just what the draft still lacks.

    `date_known` is passed in because a Draft always *has* a date (it
    defaults to today); only the caller knows whether the sheet said so.
    """
    missing: list[str] = []
    if not draft.supplier_name.strip():
        missing.append("supplier")
    if not draft.invoice_no.strip():
        missing.append("invoice_no")
    if not date_known:
        missing.append("invoice_date")
    if any(line.rate == 0 for line in draft.lines):
        missing.append("purchase_rate")
    return [slot for slot in SLOT_ORDER if slot in missing]


def summarise_gaps(draft: Draft, queue: list[str]) -> str:
    """Say what was found and what is still needed, before asking
    anything -- so the partner knows how many questions are coming
    rather than being drip-fed with no visible end (§2)."""
    lines = [f"📸 Read {len(draft.lines)} item(s) from your sheet."]
    if not queue:
        return lines[0]
    labels = {
        "supplier": "supplier",
        "invoice_no": "invoice number",
        "invoice_date": "invoice date",
        "purchase_rate": "rate",
    }
    needed = ", ".join(labels[slot] for slot in queue)
    lines.append(f"The sheet doesn't show the {needed}, so I'll ask — {len(queue)} question(s).")
    return "\n".join(lines)


async def ask_slot(slot_name: str, ctx: RequestContext, *, draft: Draft) -> CommandResult:
    """Render the question, offering choices wherever they exist (§4)."""
    spec = SLOTS[slot_name]
    body = spec.question

    if slot_name == "supplier":
        from backend.repositories.party_repository import SupplierRepository

        async with ctx.session_factory() as session:
            recent = await SupplierRepository(session).search(
                ctx.user.org_id, "", limit=SUPPLIER_ROWS
            )
        rows = tuple(Choice(id=f"slot {s.name}", title=s.name[:24]) for s in recent[:SUPPLIER_ROWS])
        if rows:
            return CommandResult(
                reply="",
                interactive=ListMenu(
                    body=body,
                    menu_label="Pick supplier",
                    sections=(
                        Section(title="Recent", rows=rows),
                        Section(
                            title="Or",
                            rows=(
                                Choice(
                                    id="slot new",
                                    title="Someone new",
                                    description="You'll type the name",
                                ),
                            ),
                        ),
                    ),
                ),
            )

    if slot_name == "invoice_date":
        return CommandResult(
            reply="",
            interactive=Buttons(
                body=body,
                choices=(
                    Choice(id="slot today", title="Today"),
                    Choice(id="slot yesterday", title="Yesterday"),
                    Choice(id="slot other", title="Another date"),
                ),
            ),
        )

    return CommandResult(reply=f"{body}\n{spec.example}")


async def begin_slots(draft: Draft, queue: list[str], ctx: RequestContext) -> CommandResult:
    """Announce the gaps, then ask the first. Two messages, because the
    summary and the question are different jobs."""
    sessions = SessionService(ctx.session_factory)
    if not queue:
        from backend.api.commands.purchase_commands import preview_result

        await sessions.set(
            ctx.user.org_id, ctx.user.id, AWAITING_PURCHASE_CONFIRMATION, draft.to_context()
        )
        return preview_result(draft)

    await sessions.set(
        ctx.user.org_id,
        ctx.user.id,
        AWAITING_SLOT,
        {"draft": draft.to_context(), "queue": queue, "filled": {}},
    )
    question = await ask_slot(queue[0], ctx, draft=draft)
    return dataclasses.replace(
        question, reply=f"{summarise_gaps(draft, queue)}\n\n{question.reply}".strip()
    )


async def handle_slot_reply(text: str, ctx: RequestContext, state: SessionState) -> CommandResult:
    """One answer fills the head of the queue and advances.

    A *recognised command* never reaches here -- the dispatcher matches
    the registry first -- which is what stops the wizard becoming a mode
    the user is trapped inside (§5).
    """
    sessions = SessionService(ctx.session_factory)
    context: dict[str, Any] = dict(state.context)
    draft = Draft.from_context(context["draft"])
    queue: list[str] = list(context.get("queue", []))
    filled: dict[str, str] = dict(context.get("filled", {}))

    answer = text.strip().removeprefix("slot ").strip()
    lowered = answer.lower()

    if lowered == "skip":
        # no slot here is optional -- every one of them is needed for a
        # posted purchase, so say which, rather than silently defaulting
        return await _reask(
            queue, draft, ctx, prefix="I can't skip that one — a purchase needs it."
        )

    if lowered == "cancel":
        await sessions.set(ctx.user.org_id, ctx.user.id, IDLE, {})
        return CommandResult(reply="Cancelled — nothing was saved.")

    if not queue:  # defensive: an empty queue should have handed off already
        await sessions.set(ctx.user.org_id, ctx.user.id, IDLE, {})
        return CommandResult(reply="That draft is finished. Send the photo again to restart.")

    if lowered == "back":
        done = list(filled)
        if not done:
            return await _reask(queue, draft, ctx, prefix="That's the first question.")
        previous = done[-1]
        filled.pop(previous)
        queue.insert(0, previous)
        await _save(sessions, ctx, draft, queue, filled)
        return await _reask(queue, draft, ctx, prefix="Going back.")

    if lowered.startswith("details "):
        # one-shot: the whole wizard answered in a single message. Same
        # draft, same preview -- typing is never *required*, but it is
        # never taken away from someone who is fast at it (§12).
        from backend.api.commands.ocr_commands import apply_details

        try:
            draft = apply_details(draft, answer[len("details ") :])
        except DomainError as exc:
            return await _reask(queue, draft, ctx, prefix=exc.message)
        return await _finish(draft, ctx)

    current = queue[0]
    if current == "supplier" and lowered == "new":
        return await _reask(queue, draft, ctx, prefix="What's their name?")
    if current == "invoice_date" and lowered == "other":
        return await _reask(queue, draft, ctx, prefix="Which date? Use DD-MM-YYYY.")

    try:
        SLOTS[current].apply(draft, answer)
    except DomainError as exc:
        # re-ask naming what was expected; never accept and never loop
        return await _reask(queue, draft, ctx, prefix=exc.message)

    filled[current] = answer
    queue.pop(0)

    if queue:
        await _save(sessions, ctx, draft, queue, filled)
        return await ask_slot(queue[0], ctx, draft=draft)

    return await _finish(draft, ctx)


async def _finish(draft: Draft, ctx: RequestContext) -> CommandResult:
    """Queue empty: resolve against the catalogue and hand to the normal
    preview, exactly as `details` does."""
    from backend.api.commands.ocr_commands import resolve_after_details
    from backend.api.commands.purchase_commands import preview_result

    draft = await resolve_after_details(draft, ctx)
    await SessionService(ctx.session_factory).set(
        ctx.user.org_id, ctx.user.id, AWAITING_PURCHASE_CONFIRMATION, draft.to_context()
    )
    return preview_result(draft)


async def _save(
    sessions: SessionService,
    ctx: RequestContext,
    draft: Draft,
    queue: list[str],
    filled: dict[str, str],
) -> None:
    await sessions.set(
        ctx.user.org_id,
        ctx.user.id,
        AWAITING_SLOT,
        {"draft": draft.to_context(), "queue": queue, "filled": filled},
    )


async def _reask(
    queue: list[str], draft: Draft, ctx: RequestContext, *, prefix: str
) -> CommandResult:
    question = await ask_slot(queue[0], ctx, draft=draft)
    return dataclasses.replace(question, reply=f"{prefix}\n{question.reply}".strip())
