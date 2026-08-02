"""`received-short` / `receive` -- correcting what actually arrived.

docs/23_ReceiptCorrections.md. The command speaks in bales because that
is what gets counted off a truck; the kilograms follow from the line's
own per-bale weight, so the correction can never disagree with the
arithmetic on the original sheet.
"""

from __future__ import annotations

import decimal

from backend.api.command_types import CommandResult, RequestContext
from backend.api.commands.documents import attach_document
from backend.api.formatting import fmt_money, fmt_qty
from backend.core.exceptions import DomainError, ValidationError
from backend.services.receipt_correction_service import CorrectionResult, ReceiptCorrectionService

USAGE = (
    "Usage: receive <invoice> <CODE> <bales actually received> [<CODE> <bales> ...]\n"
    "e.g. receive 001 35A 9        (billed 10, only 9 arrived)\n"
    "e.g. receive 001 35A 9 22D 4  (two lines short on the same bill)"
)


def parse_receive(args: str) -> tuple[str, list[tuple[str, decimal.Decimal]]]:
    """One bill, and every line of it that came in wrong.

    A truck is unloaded once, so several lines are usually short
    together. Corrections were one command each, which meant retyping
    the invoice number and re-reading the same reply -- and made it easy
    to stop after the first.
    """
    tokens = args.split()
    if len(tokens) < 3:
        raise ValidationError(USAGE)
    invoice_no, rest = tokens[0], tokens[1:]
    if len(rest) % 2:
        raise ValidationError(
            f"'{rest[-1]}' has no count after it — every code needs the bales that "
            f"actually arrived.\n{USAGE}"
        )
    corrections: list[tuple[str, decimal.Decimal]] = []
    for index in range(0, len(rest), 2):
        code, raw = rest[index], rest[index + 1]
        try:
            pieces = decimal.Decimal(raw.replace(",", ""))
        except decimal.InvalidOperation:
            raise ValidationError(f"'{raw}' isn't a number of bales I can read. {USAGE}") from None
        corrections.append((code, pieces))
    seen = [code.upper() for code, _ in corrections]
    duplicated = {code for code in seen if seen.count(code) > 1}
    if duplicated:
        # Two counts for one line is two different claims about what
        # arrived; applying both would leave the last one silently
        # winning over a number the user also meant.
        raise ValidationError(
            f"{', '.join(sorted(duplicated))} is listed twice — give one count per code."
        )
    return invoice_no, corrections


def _warnings(results: list[CorrectionResult]) -> list[str]:
    """Said once for the whole correction, however many lines it covered
    -- three identical freight notices read as three different facts."""
    notes: list[str] = []
    # Read off the last result only: after the first of three lines the
    # bill may look overpaid and not be once the rest are applied.
    if results[-1].now_overpaid:
        notes.append(
            "⚠️ You've already paid more than the corrected bill — the excess sits as "
            "an advance with this supplier."
        )
    approximated = [result.code for result in results if result.cost_approximated]
    if approximated:
        notes.append(
            f"⚠️ Most of {', '.join(approximated)} has already been sold, so the average "
            "cost couldn't be unwound exactly — worth checking."
        )
    if any(result.freight_reallocated for result in results):
        notes.append(
            "ℹ️ Freight was re-split across the invoice. Other products' *historical* "
            "average cost is unchanged — only this one moved."
        )
    return notes


def _movement(result: CorrectionResult) -> str:
    return (
        f"{fmt_qty(result.old_pieces)} → {fmt_qty(result.new_pieces)} bales "
        f"× {fmt_qty(result.weight_per_piece)} = {fmt_qty(result.new_qty)} "
        f"(was {fmt_qty(result.old_qty)})"
    )


def render(result: CorrectionResult) -> str:
    direction = "short" if result.new_qty < result.old_qty else "extra"
    lines = [
        f"✅ {result.code} on {result.invoice_no} corrected — came in {direction}.",
        _movement(result),
        f"Invoice total: {fmt_money(result.old_grand_total)} → {fmt_money(result.new_grand_total)}",
        f"Still owed to {result.supplier_name}: {fmt_money(result.payable_after)}",
        f"Stock now {fmt_qty(result.resulting_qty_on_hand)} "
        f"@ {fmt_money(result.resulting_avg_cost)} avg",
    ]
    lines.extend(_warnings([result]))
    return "\n".join(lines)


def render_all(results: list[CorrectionResult]) -> str:
    """Several lines off one truck.

    The bill's total and the payable are stated once, at the end, from
    the state after every line was applied -- quoting them per line
    would show four different "still owed" figures for one bill, three
    of which were true only mid-correction.
    """
    if len(results) == 1:
        return render(results[0])
    first, last = results[0], results[-1]
    lines = [f"✅ {len(results)} lines corrected on {last.invoice_no}."]
    for result in results:
        direction = "short" if result.new_qty < result.old_qty else "extra"
        lines.append(f"• {result.code} — came in {direction}: {_movement(result)}")
        lines.append(
            f"  stock now {fmt_qty(result.resulting_qty_on_hand)} "
            f"@ {fmt_money(result.resulting_avg_cost)} avg"
        )
    lines.append(
        f"Invoice total: {fmt_money(first.old_grand_total)} → {fmt_money(last.new_grand_total)}"
    )
    lines.append(f"Still owed to {last.supplier_name}: {fmt_money(last.payable_after)}")
    lines.extend(_warnings(results))
    return "\n".join(lines)


async def handle_receive(args: str, ctx: RequestContext) -> CommandResult:
    try:
        invoice_no, corrections = parse_receive(args)
    except DomainError as exc:
        return CommandResult(reply=exc.message)

    try:
        # One transaction for the whole truck: several lines short on one
        # bill is one event, and half of it applied would leave the bill
        # disagreeing with the stock behind it.
        async with ctx.session_factory() as session, session.begin():
            service = ReceiptCorrectionService(session)
            results = [
                await service.correct(
                    ctx.user,
                    invoice_no=invoice_no,
                    code=code,
                    received_pieces=pieces,
                    whatsapp_message_id=ctx.message_id,
                )
                for code, pieces in corrections
            ]
    except DomainError as exc:
        return CommandResult(reply=exc.message)
    # The bill changed, so its sheet did too -- sent with the change
    # rather than left for someone to ask for, since a corrected bill
    # whose old copy is still circulating is the problem this solves.
    return await attach_document(
        CommandResult(reply=render_all(results)),
        ctx,
        kind="purchase",
        reference=str(results[-1].header_id),
    )
