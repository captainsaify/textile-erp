"""`rate` -- correcting the price on a confirmed bill.

docs/26_RateChanges.md. The quantity was right and the price was not,
which is a different correction from a short delivery: nothing moves,
but the bill, the payable and what the stock cost all change.

    rate 001 145            every line on the bill
    rate 001 145 35A 22D    only those codes
"""

from __future__ import annotations

import decimal

from backend.api.command_types import CommandResult, RequestContext
from backend.api.formatting import fmt_money
from backend.core.exceptions import DomainError, ValidationError
from backend.services.receipt_correction_service import RateChange, RateChangeService

USAGE = (
    "Usage: rate <invoice> <new rate> [CODE CODE ...]\n"
    "e.g. rate 001 145        (every line)\n"
    "e.g. rate 001 145 35A 22D  (just those)"
)


def parse_rate(args: str) -> tuple[str, decimal.Decimal, list[str]]:
    tokens = args.split()
    if len(tokens) < 2:
        raise ValidationError(USAGE)
    invoice_no, raw = tokens[0], tokens[1]
    try:
        rate = decimal.Decimal(raw.replace(",", "").replace("₹", ""))
    except decimal.InvalidOperation:
        raise ValidationError(f"'{raw}' isn't a rate I can read. {USAGE}") from None
    # everything after the rate is a code; several is the normal case
    codes = [token.upper() for token in tokens[2:]]
    return invoice_no, rate, codes


def render(result: RateChange) -> str:
    scope = "every line" if len(result.codes) > 3 else ", ".join(result.codes)
    was = f"{fmt_money(result.old_rate)} → " if result.old_rate is not None else ""
    lines = [
        f"✅ Rate updated on {result.invoice_no} — {scope}.",
        f"{was}{fmt_money(result.new_rate)} per unit",
        f"Invoice total: {fmt_money(result.old_grand_total)} → {fmt_money(result.new_grand_total)}",
        f"Still owed to {result.supplier_name}: {fmt_money(result.payable_after)}",
    ]
    if result.now_overpaid:
        lines.append(
            "⚠️ You've already paid more than the corrected bill — the excess sits as "
            "an advance with this supplier."
        )
    if result.partly_sold:
        lines.append(
            f"ℹ️ Some of {', '.join(result.partly_sold)} has already been sold. The stock "
            "still on hand was revalued; what was sold keeps the cost it was sold at."
        )
    return "\n".join(lines)


async def handle_rate(args: str, ctx: RequestContext) -> CommandResult:
    try:
        invoice_no, rate, codes = parse_rate(args)
    except DomainError as exc:
        return CommandResult(reply=exc.message)

    try:
        async with ctx.session_factory() as session, session.begin():
            result = await RateChangeService(session).change(
                ctx.user,
                invoice_no=invoice_no,
                new_rate=rate,
                codes=codes or None,
                whatsapp_message_id=ctx.message_id,
            )
    except DomainError as exc:
        return CommandResult(reply=exc.message)
    return CommandResult(reply=render(result))
