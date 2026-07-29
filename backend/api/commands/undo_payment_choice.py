"""Undoing a bill that has money sitting on it -- docs/25 §4.

A sale can be reversed while a customer's payment is still applied to
it. Two silent answers are both wrong: reversing the payment too takes
back money the customer really did send, and leaving it orphans a
receipt against a sale that no longer exists.

So it asks. The choice is the partner's because only they know whether
the money is staying (an advance against the next sale) or going back.
"""

from __future__ import annotations

import decimal

from backend.api.command_types import CommandResult, RequestContext
from backend.api.formatting import fmt_money
from backend.api.interactive import Buttons, Choice
from backend.services.session_service import (
    AWAITING_UNDO_PAYMENT_CHOICE,
    IDLE,
    SessionService,
    SessionState,
)

BOTH = "undo both"
BILL_ONLY = "undo bill-only"


def offer(
    entity: str,
    reference: str,
    payments: list[tuple[str, str, decimal.Decimal]],
) -> CommandResult:
    total = sum((applied for _, _, applied in payments), decimal.Decimal("0"))
    verb = "received against" if entity == "sale" else "paid against"
    detail = ", ".join(f"{fmt_money(applied)} {via}" for _, via, applied in payments)
    return CommandResult(
        reply=(
            f"⚠️ {fmt_money(total)} has been {verb} {reference} ({detail}).\n"
            "Reversing the bill alone would leave that money attached to something "
            "that no longer exists."
        ),
        interactive=Buttons(
            body=f"What should happen to the {fmt_money(total)}?",
            choices=(
                Choice(id=BOTH, title="Reverse both"),
                Choice(id=BILL_ONLY, title="Keep the money"),
                Choice(id="undo cancel", title="Cancel"),
            ),
            footer="Keeping it leaves it as an advance with the party.",
        ),
    )


async def remember(
    entity: str,
    reference: str,
    payments: list[tuple[str, str, decimal.Decimal]],
    ctx: RequestContext,
) -> None:
    await SessionService(ctx.session_factory).set(
        ctx.user.org_id,
        ctx.user.id,
        AWAITING_UNDO_PAYMENT_CHOICE,
        {
            "entity": entity,
            "reference": reference,
            "payments": [ref for ref, _, _ in payments],
        },
    )


async def handle_choice(text: str, ctx: RequestContext, state: SessionState) -> CommandResult:
    from backend.api.commands.correction_commands import run_undo
    from backend.api.commands.payment_reversal import handle_undo_payment

    sessions = SessionService(ctx.session_factory)
    choice = text.strip().lower().removeprefix("undo ").strip()
    entity = str(state.context.get("entity", ""))
    reference = str(state.context.get("reference", ""))
    payments = [str(ref) for ref in state.context.get("payments", [])]

    await sessions.set(ctx.user.org_id, ctx.user.id, IDLE, {})

    if choice in {"cancel", "discard", "stop"}:
        return CommandResult(reply="Left everything as it was.")

    replies: list[str] = []
    if choice in {"both", "reverse both"}:
        # payments first: reversing them re-opens the bill, and undoing
        # the bill first would leave nothing for them to unapply against
        for payment_ref in payments:
            replies.append((await handle_undo_payment(payment_ref, ctx)).reply)

    outcome = await run_undo(entity, reference, ctx)
    replies.append(outcome.reply)
    if choice not in {"both", "reverse both"}:
        replies.append(
            "The money stays as an advance with the party — settle it against their "
            "next bill, or reverse it separately."
        )
    return CommandResult(reply="\n\n".join(replies))
