"""`undo payment <ref>` -- taking a payment back.

docs/25_PaymentReversals.md. A settlement moves money *and* marks bills
as settled. Reversing only the money would leave bills showing paid that
nobody paid -- the payable understated, which is the direction that
loses money quietly. Both are unwound together.
"""

from __future__ import annotations

from backend.api.command_types import CommandResult, RequestContext
from backend.api.formatting import fmt_money
from backend.core.exceptions import DomainError
from backend.services.settlement_service import PaymentReversalService


async def handle_undo_payment(reference: str, ctx: RequestContext) -> CommandResult:
    if not reference.strip():
        return CommandResult(
            reply="Which payment? Pick it from 'delete' → Payment, or give its reference."
        )
    try:
        async with ctx.session_factory() as session:
            result = await PaymentReversalService(session).reverse(
                ctx.user, reference=reference.strip(), whatsapp_message_id=ctx.message_id
            )
    except DomainError as exc:
        return CommandResult(reply=exc.message)

    direction = "to" if result.kind == "paid" else "from"
    owes = "payable" if result.kind == "paid" else "outstanding"
    lines = [
        f"↩️ Payment reversed — {fmt_money(result.amount)} {direction} {result.party_name} "
        f"({result.via}).",
        f"{result.via.capitalize()} balance now {fmt_money(result.ledger_balance)}",
        f"{result.party_name}'s {owes} now {fmt_money(result.outstanding_after)}",
    ]
    if result.unapplied:
        lines.append(f"Taken back off: {', '.join(result.unapplied)}")
    return CommandResult(reply="\n".join(lines))
