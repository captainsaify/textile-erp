"""`undo expense <ref>` -- reversing an expense.

docs/23_ReceiptCorrections.md's sibling: money that already moved is put
back with compensating entries, never removed by deleting a row. An
expense paid personally by a partner reverses against their capital
rather than the till, because the business cash was never touched.
"""

from __future__ import annotations

from backend.api.command_types import CommandResult, RequestContext
from backend.api.formatting import fmt_money
from backend.core.exceptions import DomainError
from backend.services.money_service import MoneyService


async def handle_undo_expense(reference: str, ctx: RequestContext) -> CommandResult:
    if not reference.strip():
        return CommandResult(reply="Which expense? Use its reference — 'expenses' lists them.")
    try:
        async with ctx.session_factory() as session:
            result = await MoneyService(session).reverse_expense(
                ctx.user,
                reference=reference.strip(),
                whatsapp_message_id=ctx.message_id,
            )
    except DomainError as exc:
        return CommandResult(reply=exc.message)

    where = "the partner's capital" if result.paid_by_partner else f"{result.via} balance"
    return CommandResult(
        reply=(
            f"↩️ Expense reversed — {result.category} {fmt_money(result.amount)}.\n"
            f"Put back to {where}, now {fmt_money(result.balance_after)}.\n"
            "It stays in your history, marked reversed."
        )
    )
