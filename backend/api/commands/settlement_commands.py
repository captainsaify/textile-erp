"""`received` and `paid` -- docs/08_WhatsApp.md #received / #paid.

Overpayment parks the request in the session for one confirmation
("confirm advance") rather than being silently clamped or rejected.
"""

from __future__ import annotations

import dataclasses
import decimal
import re

from backend.api.command_types import CommandResult, RequestContext
from backend.api.formatting import fmt_money
from backend.core.exceptions import DomainError, ValidationError
from backend.services.session_service import (
    AWAITING_SETTLEMENT_CONFIRMATION,
    SessionService,
    SessionState,
)
from backend.services.settlement_service import SettlementResult, SettlementService

_PATTERN = re.compile(
    r"^(?:Customer|Supplier):\s*(?P<party>.+?)\s+(?P<amount>[\d.]+)\s+"
    r"(?P<via>cash|bank)(?:\s+against\s+(?P<against>\S+))?\s*$",
    re.IGNORECASE,
)


@dataclasses.dataclass(frozen=True)
class SettlementCommand:
    party: str
    amount: decimal.Decimal
    via: str
    against: str | None


def parse_settlement(args: str, kind: str) -> SettlementCommand:
    label = "Customer" if kind == "received" else "Supplier"
    usage = f"Usage: {kind} {label}: <name> <amount> <cash|bank> [against <ref>]"
    match = _PATTERN.match(args.strip())
    if match is None:
        raise ValidationError(usage)
    try:
        amount = decimal.Decimal(match["amount"])
    except decimal.InvalidOperation:
        raise ValidationError(f"'{match['amount']}' is not a number. {usage}") from None
    return SettlementCommand(
        party=match["party"].strip(),
        amount=amount,
        via=match["via"].lower(),
        against=match["against"],
    )


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
                },
            )
        return CommandResult(reply=exc.message)
    except DomainError as exc:
        return CommandResult(reply=exc.message)
    await sessions.clear(ctx.user.org_id, ctx.user.id)
    return CommandResult(reply=render_settlement(result, kind))


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
    if lowered in {"cancel", "discard"}:
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
    )
    return await _run(command, ctx, str(context["kind"]), allow_advance=True)
