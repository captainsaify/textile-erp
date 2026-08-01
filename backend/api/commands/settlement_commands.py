"""`received` and `paid` -- docs/08_WhatsApp.md #received / #paid.

Overpayment parks the request in the session for one confirmation
("confirm advance") rather than being silently clamped or rejected.
"""

from __future__ import annotations

import dataclasses
import decimal
import re

from backend.api.amounts import looks_like_amount, parse_amount
from backend.api.command_types import CommandResult, RequestContext
from backend.api.commands.documents import attach_document
from backend.api.formatting import fmt_money
from backend.api.interactive import is_abandon
from backend.core.dates import split_date
from backend.core.exceptions import DomainError, ValidationError
from backend.services.session_service import (
    AWAITING_SETTLEMENT_CONFIRMATION,
    SessionService,
    SessionState,
)
from backend.services.settlement_service import SettlementResult, SettlementService

#: `against`, but people write it several ways
_AGAINST_WORDS = {"against", "ref", "ref:", "for", "#", "invoice"}
#: an optional, redundant label -- the command already says which side
_LABELS = {"customer:", "supplier:", "customer", "supplier"}

#: How a note is introduced. Everything after it is the note.
_NOTE = re.compile(r"\b(?:note|memo|desc|description|remark)s?\s*[:\-]\s*(?P<note>.+)$", re.I)


def _split_note(args: str) -> tuple[str, str | None]:
    match = _NOTE.search(args)
    if match is None:
        return args, None
    return args[: match.start()].strip(), match["note"].strip() or None


@dataclasses.dataclass(frozen=True)
class SettlementCommand:
    party: str
    amount: decimal.Decimal
    via: str
    against: str | None
    #: Raw, not a date: "today" can only be resolved against the org's
    #: business date, which the service reads inside its transaction.
    on: str | None = None
    #: Why this payment looks the way it does. The partners' own book
    #: carries one on half its rows -- "in ac mahadev", "through hanif
    #: pune" -- and without it a statement can show that ₹1,65,000 moved
    #: but not that it moved through someone else.
    note: str | None = None


def parse_settlement(args: str, kind: str) -> SettlementCommand:
    """Deliberately forgiving, and specific when it can't cope.

    A single regex here rejected every one of eight real attempts with
    the same usage line, which told the user nothing about *which* part
    was wrong -- one of them differed only by writing "ref 001" instead
    of "against 001". This walks the tokens instead, so each failure can
    name the actual problem.
    """
    label = "Customer" if kind == "received" else "Supplier"
    example = f"{kind} {'ABC' if kind == 'received' else 'Wagdia'} 40000 cash"
    usage = (
        f"Usage: {kind} [{label}:] <name> <amount> <cash|bank> [against <ref>] "
        f"[on DD-MM-YYYY] [note: <text>]\n"
        f"e.g. {example}"
    )

    # The note comes off first and takes the rest of the line, so it can
    # hold anything -- including words the rest of this parser would
    # otherwise read as a method or an amount.
    args, note = _split_note(args)

    # Pulled out first: money is routinely entered days after it moved,
    # and `on 28-07-2026` must not be mistaken for the invoice reference
    # or absorbed into the party's name.
    args, on = split_date(args)
    tokens = args.split()
    if not tokens:
        raise ValidationError(usage)

    # the label is optional: `paid Wagdia 40000 cash` is unambiguous
    if tokens[0].lower() in _LABELS:
        tokens = tokens[1:]

    against: str | None = None
    for index, token in enumerate(tokens):
        if token.lower() in _AGAINST_WORDS and index + 1 < len(tokens):
            against = tokens[index + 1]
            tokens = tokens[:index]
            break

    via_index = next(
        (i for i, token in enumerate(tokens) if token.lower() in {"cash", "bank"}), None
    )
    if via_index is None:
        raise ValidationError(f"I need to know whether this was cash or bank.\n{usage}")
    via = tokens[via_index].lower()
    trailing = tokens[via_index + 1 :]
    tokens = tokens[:via_index]

    # The amount is looked for *before* the method, because canonical
    # order is <name> <amount> <cash|bank>. Searching the whole line
    # backwards instead read `paid Wagdia 40000 cash 001` as ₹1.00,
    # absorbed the real 40000 into the supplier name, and reported
    # success -- silent, and wrong in money.
    amount_index = next(
        (i for i in range(len(tokens) - 1, -1, -1) if looks_like_amount(tokens[i])), None
    )

    if trailing:
        if amount_index is None and len(trailing) == 1 and looks_like_amount(trailing[0]):
            # `paid Wagdia cash 40000` -- method and amount swapped. There
            # is no amount before the method, so this can only be one.
            tokens = [*tokens, trailing[0]]
            amount_index = len(tokens) - 1
        elif against is None and len(trailing) == 1:
            # `paid Wagdia 40000 cash 001` -- a bare invoice number with
            # "against" left out.
            against = trailing[0]
        else:
            extra = " ".join(trailing)
            raise ValidationError(
                f"I don't know what '{extra}' means after '{via}'.\n"
                f"If it's an invoice, write 'against {trailing[0]}'.\n{usage}"
            )

    if amount_index is None:
        raise ValidationError(f"I couldn't find an amount in that.\n{usage}")
    amount = parse_amount(tokens[amount_index])

    party = " ".join(tokens[:amount_index]).strip().rstrip(":")
    if not party:
        raise ValidationError(f"Which {label.lower()}?\n{usage}")

    return SettlementCommand(party=party, amount=amount, via=via, against=against, on=on, note=note)


def render_settlement(result: SettlementResult, kind: str) -> str:
    verb = "Payment received" if kind == "received" else "Payment made"
    applied = (
        ", ".join(f"{a.reference} ({fmt_money(a.applied)})" for a in result.allocations)
        or "no open invoices — recorded as an advance"
    )
    owes = "outstanding" if kind == "received" else "payable"
    lines = [
        f"✅ {verb} — {result.party_name} {fmt_money(result.amount)} ({result.via})",
        f"Applied to: {applied}",
    ]
    if result.advance > 0:
        lines.append(f"Advance recorded: {fmt_money(result.advance)}")
    lines.append(f"{result.party_name}'s {owes} now {fmt_money(result.outstanding_after)}")
    lines.append(f"{result.via.capitalize()} balance now {fmt_money(result.ledger_balance)}")
    if result.note:
        lines.append(f"Note: {result.note}")
    if result.reference:
        lines.append(f"Ref: {result.reference}")
    return "\n".join(lines)


async def _run(
    command: SettlementCommand, ctx: RequestContext, kind: str, *, allow_advance: bool
) -> CommandResult:
    sessions = SessionService(ctx.session_factory)
    try:
        async with ctx.session_factory() as session:
            service = SettlementService(session)
            if kind == "received":
                result = await service.receive_from_customer(
                    ctx.user,
                    customer_name=command.party,
                    amount=command.amount,
                    via=command.via,
                    against=command.against,
                    on=command.on,
                    note=command.note,
                    allow_advance=allow_advance,
                    whatsapp_message_id=ctx.message_id,
                )
            else:
                result = await service.pay_supplier(
                    ctx.user,
                    supplier_name=command.party,
                    amount=command.amount,
                    via=command.via,
                    against=command.against,
                    on=command.on,
                    note=command.note,
                    allow_advance=allow_advance,
                    whatsapp_message_id=ctx.message_id,
                )
    except ValidationError as exc:
        if "advance" in exc.message and not allow_advance:
            await sessions.set(
                ctx.user.org_id,
                ctx.user.id,
                AWAITING_SETTLEMENT_CONFIRMATION,
                {
                    "kind": kind,
                    "party": command.party,
                    "amount": str(command.amount),
                    "via": command.via,
                    "against": command.against,
                    "on": command.on,
                    "note": command.note,
                },
            )
        return CommandResult(reply=exc.message)
    except DomainError as exc:
        return CommandResult(reply=exc.message)
    await sessions.clear(ctx.user.org_id, ctx.user.id)
    return await attach_document(
        CommandResult(reply=render_settlement(result, kind)),
        ctx,
        kind="payment",
        reference=result.reference,
    )


async def handle_received(args: str, ctx: RequestContext) -> CommandResult:
    try:
        command = parse_settlement(args, "received")
    except DomainError as exc:
        return CommandResult(reply=exc.message)
    return await _run(command, ctx, "received", allow_advance=False)


async def handle_paid(args: str, ctx: RequestContext) -> CommandResult:
    try:
        command = parse_settlement(args, "paid")
    except DomainError as exc:
        return CommandResult(reply=exc.message)
    return await _run(command, ctx, "paid", allow_advance=False)


async def handle_settlement_session_reply(
    text: str, ctx: RequestContext, state: SessionState
) -> CommandResult:
    lowered = text.strip().lower()
    sessions = SessionService(ctx.session_factory)
    if is_abandon(lowered):
        await sessions.clear(ctx.user.org_id, ctx.user.id)
        return CommandResult(reply="Payment discarded.")
    if lowered not in {"confirm advance", "confirm"}:
        return CommandResult(
            reply="Reply 'confirm advance' to record the extra as an advance, "
            "or 'cancel' and resend with a corrected amount."
        )
    context = state.context
    command = SettlementCommand(
        party=str(context["party"]),
        amount=decimal.Decimal(str(context["amount"])),
        via=str(context["via"]),
        against=context["against"] if context.get("against") else None,
        on=context.get("on") or None,
        note=context.get("note") or None,
    )
    return await _run(command, ctx, str(context["kind"]), allow_advance=True)
