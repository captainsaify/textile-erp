"""`received-short` / `receive` -- correcting what actually arrived.

docs/23_ReceiptCorrections.md. The command speaks in bales because that
is what gets counted off a truck; the kilograms follow from the line's
own per-bale weight, so the correction can never disagree with the
arithmetic on the original sheet.
"""

from __future__ import annotations

import decimal

from backend.api.command_types import CommandResult, RequestContext
from backend.api.formatting import fmt_money, fmt_qty
from backend.core.exceptions import DomainError, ValidationError
from backend.services.receipt_correction_service import CorrectionResult, ReceiptCorrectionService

USAGE = (
    "Usage: receive <invoice> <CODE> <bales actually received>\n"
    "e.g. receive 001 35A 9   (billed 10, only 9 arrived)"
)


def parse_receive(args: str) -> tuple[str, str, decimal.Decimal]:
    tokens = args.split()
    if len(tokens) < 3:
        raise ValidationError(USAGE)
    invoice_no, code, raw = tokens[0], tokens[1], tokens[2]
    try:
        pieces = decimal.Decimal(raw.replace(",", ""))
    except decimal.InvalidOperation:
        raise ValidationError(f"'{raw}' isn't a number of bales I can read. {USAGE}") from None
    return invoice_no, code, pieces


def render(result: CorrectionResult) -> str:
    direction = "short" if result.new_qty < result.old_qty else "extra"
    lines = [
        f"✅ {result.code} on {result.invoice_no} corrected — came in {direction}.",
        f"{fmt_qty(result.old_pieces)} → {fmt_qty(result.new_pieces)} bales "
        f"× {fmt_qty(result.weight_per_piece)} = {fmt_qty(result.new_qty)} "
        f"(was {fmt_qty(result.old_qty)})",
        f"Invoice total: {fmt_money(result.old_grand_total)} → {fmt_money(result.new_grand_total)}",
        f"Still owed to {result.supplier_name}: {fmt_money(result.payable_after)}",
        f"Stock now {fmt_qty(result.resulting_qty_on_hand)} "
        f"@ {fmt_money(result.resulting_avg_cost)} avg",
    ]
    if result.now_overpaid:
        lines.append(
            "⚠️ You've already paid more than the corrected bill — the excess sits as "
            "an advance with this supplier."
        )
    if result.cost_approximated:
        lines.append(
            "⚠️ Most of that batch has already been sold, so the average cost couldn't be "
            "unwound exactly — worth checking."
        )
    if result.freight_reallocated:
        lines.append(
            "ℹ️ Freight was re-split across the invoice. Other products' *historical* "
            "average cost is unchanged — only this one moved."
        )
    return "\n".join(lines)


async def handle_receive(args: str, ctx: RequestContext) -> CommandResult:
    try:
        invoice_no, code, pieces = parse_receive(args)
    except DomainError as exc:
        return CommandResult(reply=exc.message)

    try:
        async with ctx.session_factory() as session, session.begin():
            result = await ReceiptCorrectionService(session).correct(
                ctx.user,
                invoice_no=invoice_no,
                code=code,
                received_pieces=pieces,
                whatsapp_message_id=ctx.message_id,
            )
    except DomainError as exc:
        return CommandResult(reply=exc.message)
    return CommandResult(reply=render(result))
