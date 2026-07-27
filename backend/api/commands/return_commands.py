"""`return` -- docs/08_WhatsApp.md #return, docs/05_Sales.md §6.

A return of goods sold for cash and already paid for cannot be posted
straight through: whether cash actually left the drawer is a fact only
the partner knows. That case parks in a session and asks; every other
case executes immediately.
"""

from __future__ import annotations

import decimal
import re

from backend.api.command_types import CommandResult, RequestContext
from backend.api.formatting import fmt_date, fmt_money, fmt_qty
from backend.api.interactive import Buttons, Choice
from backend.core.exceptions import DomainError, ValidationError
from backend.services.return_service import ReturnPreview, ReturnRecorded, ReturnService
from backend.services.session_service import (
    AWAITING_RETURN_REFUND_CHOICE,
    IDLE,
    SessionService,
    SessionState,
)

USAGE = (
    "Usage:\n"
    "return sale <customer|last> <CODE> <qty> [reason: <text>]\n"
    "return purchase <invoice-no|last> <CODE> <qty> [reason: <text>]"
)

_RETURN = re.compile(
    r"^(?P<kind>sale|purchase)\s+(?P<reference>.+?)\s+(?P<code>\S+)\s+(?P<qty>[\d.]+)"
    r"(?:\s+reason:\s*(?P<reason>.+))?$",
    re.IGNORECASE,
)

_REFUND_CHOICE = {
    "refund cash": "refund_cash",
    "refund bank": "refund_bank",
    "credit": "credit_note",
    "credit note": "credit_note",
}


def parse_return_command(args: str) -> tuple[str, str, str, decimal.Decimal, str | None]:
    """-> (kind, reference, code, qty, reason)."""
    match = _RETURN.match(args.strip())
    if match is None:
        raise ValidationError(USAGE)
    try:
        qty = decimal.Decimal(match["qty"])
    except decimal.InvalidOperation:
        raise ValidationError(f"'{match['qty']}' is not a number.\n{USAGE}") from None
    reason = match["reason"].strip() if match["reason"] else None
    return (
        match["kind"].lower(),
        match["reference"].strip(),
        match["code"].strip().upper(),
        qty,
        reason,
    )


def render_recorded(recorded: ReturnRecorded) -> str:
    source = "sale" if recorded.kind == "sale" else "purchase"
    lines = [
        f"✅ Return recorded — {fmt_qty(recorded.qty)} {recorded.unit_code} "
        f"{recorded.product_code} from {recorded.party_name}'s {source} "
        f"({fmt_date(recorded.transaction_date)})"
    ]
    if recorded.settlement == "receivable":
        lines.append(
            f"{recorded.party_name}'s outstanding reduced by {fmt_money(recorded.line_value)}"
            + (
                f" (now {fmt_money(recorded.outstanding_after)})"
                if recorded.outstanding_after is not None
                else ""
            )
        )
    elif recorded.settlement in {"refund_cash", "refund_bank"}:
        via = "cash" if recorded.settlement == "refund_cash" else "bank"
        lines.append(f"Refunded {fmt_money(recorded.line_value)} from {via}")
    elif recorded.settlement == "credit_note":
        lines.append(
            f"{fmt_money(recorded.line_value)} held as credit against "
            f"{recorded.party_name}'s next purchase"
        )
    elif recorded.settlement == "payable":
        lines.append(
            f"Owed to {recorded.party_name} reduced by {fmt_money(recorded.line_value)}"
            + (
                f" (now {fmt_money(recorded.outstanding_after)})"
                if recorded.outstanding_after is not None
                else ""
            )
        )
    lines.append(
        f"Stock after: {recorded.product_code} {fmt_qty(recorded.qty_on_hand_after)} "
        f"{recorded.unit_code}"
    )
    if recorded.cost_approximated:
        # docs/03_Inventory.md §4 -- documented, not hidden
        lines.append(
            "⚠️ Most of that batch had already been sold, so the average cost couldn't be "
            "unwound exactly. Stock value was reduced proportionally instead — worth a "
            "manual check."
        )
    return "\n".join(lines)


def _render_refund_question(preview: ReturnPreview) -> str:
    return (
        f"↩️ {fmt_qty(preview.qty)} {preview.unit_code} {preview.product_code} "
        f"from {preview.party_name}'s sale ({fmt_date(preview.transaction_date)}) — "
        f"that sale was already paid.\n"
        f"Refund {fmt_money(preview.line_value)} to {preview.party_name} now (reply "
        f'"refund cash" or "refund bank"), or hold it as credit against their next '
        f'purchase (reply "credit")?'
    )


async def handle_return(args: str, ctx: RequestContext) -> CommandResult:
    try:
        kind, reference, code, qty, reason = parse_return_command(args)
        # Preview and execute get their own sessions on purpose: the
        # preview is read-only and `execute` opens its own transaction,
        # which SQLAlchemy refuses on a session a read has already
        # autobegun. It also means execute re-validates independently,
        # which is what makes the parked refund case safe.
        async with ctx.session_factory() as session:
            preview = await ReturnService(session).preview(
                ctx.user, kind=kind, reference=reference, code=code, qty=qty, reason=reason
            )
        if not preview.needs_refund_choice:
            async with ctx.session_factory() as session:
                recorded = await ReturnService(session).execute(
                    ctx.user, preview, settlement="auto", whatsapp_message_id=ctx.message_id
                )
            return CommandResult(reply=render_recorded(recorded))
    except DomainError as exc:
        return CommandResult(reply=exc.message)

    await SessionService(ctx.session_factory).set(
        ctx.user.org_id, ctx.user.id, AWAITING_RETURN_REFUND_CHOICE, preview.to_context()
    )
    return CommandResult(
        reply=_render_refund_question(preview),
        interactive=Buttons(
            body=(
                f"That sale was already paid. Return {fmt_money(preview.line_value)} "
                f"to {preview.party_name}, or hold it as credit?"
            ),
            choices=(
                Choice(id="refund cash", title="Refund cash"),
                Choice(id="refund bank", title="Refund bank"),
                Choice(id="credit", title="Credit note"),
            ),
        ),
    )


async def handle_return_session_reply(
    text: str, ctx: RequestContext, state: SessionState
) -> CommandResult:
    choice = _REFUND_CHOICE.get(text.strip().lower())
    if choice is None:
        return CommandResult(
            reply='Reply "refund cash", "refund bank", or "credit" — '
            'or "cancel" to drop this return.'
            if text.strip().lower() != "cancel"
            else "Return cancelled — nothing was changed."
        )

    preview = ReturnPreview.from_context(state.context)
    try:
        async with ctx.session_factory() as session:
            recorded = await ReturnService(session).execute(
                ctx.user, preview, settlement=choice, whatsapp_message_id=ctx.message_id
            )
    except DomainError as exc:
        return CommandResult(reply=exc.message)

    sessions = SessionService(ctx.session_factory)
    await sessions.set(ctx.user.org_id, ctx.user.id, IDLE, {})
    return CommandResult(reply=render_recorded(recorded))
