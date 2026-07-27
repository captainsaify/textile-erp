"""`expense`, `income`, `cash`, `bank` -- docs/08_WhatsApp.md #expense,
#income, #cash-bank. Money-moving grammar never defaults the cash/bank
qualifier silently (docs/06_Accounting.md §13)."""

from __future__ import annotations

import dataclasses
import decimal
import re

from backend.api.amounts import parse_amount
from backend.api.command_types import CommandResult, RequestContext
from backend.api.formatting import fmt_date, fmt_money
from backend.core.exceptions import DomainError, ValidationError
from backend.repositories.accounting_repository import LedgerRepository
from backend.services.money_service import MoneyService

_PAID_BY = re.compile(r"\bpaid\s+by\s+(.+)$", re.IGNORECASE)


@dataclasses.dataclass(frozen=True)
class MoneyCommand:
    category: str
    amount: decimal.Decimal
    via: str
    description: str | None
    paid_by: str | None


def parse_money_command(args: str, command: str) -> MoneyCommand:
    usage = f"Usage: {command} <category> <amount> <cash|bank> [description]"
    tokens = args.split()
    if len(tokens) < 3:
        raise ValidationError(usage)
    category, amount_raw, via = tokens[0], tokens[1], tokens[2].lower()
    # Indian grouping accepted: the system prints ₹40,92,000.00, so
    # refusing that shape back would be the system contradicting itself.
    amount = parse_amount(amount_raw)
    if via not in {"cash", "bank"}:
        raise ValidationError(f"Say cash or bank — e.g. {command} {category} {amount_raw} cash")
    description = " ".join(tokens[3:]).strip() or None

    paid_by = None
    if description:
        match = _PAID_BY.search(description)
        if match:
            paid_by = match.group(1).strip()
            description = description[: match.start()].strip() or None

    return MoneyCommand(
        category=category, amount=amount, via=via, description=description, paid_by=paid_by
    )


async def handle_expense(args: str, ctx: RequestContext) -> CommandResult:
    try:
        command = parse_money_command(args, "expense")
        async with ctx.session_factory() as session:
            recorded = await MoneyService(session).record_expense(
                ctx.user,
                category=command.category,
                amount=command.amount,
                via=command.via,
                description=command.description,
                paid_by_partner_name=command.paid_by,
                whatsapp_message_id=ctx.message_id,
            )
    except DomainError as exc:
        return CommandResult(reply=exc.message)

    label = recorded.category.capitalize()
    if recorded.paid_by_partner:
        reply = (
            f"✅ Expense recorded — {label} {fmt_money(recorded.amount)} "
            f"(paid by {recorded.paid_by_partner}). "
            f"{recorded.paid_by_partner}'s capital balance now "
            f"{fmt_money(recorded.new_balance)}."
        )
    else:
        reply = (
            f"✅ Expense recorded — {label} {fmt_money(recorded.amount)} ({recorded.via}). "
            f"{recorded.via.capitalize()} balance now {fmt_money(recorded.new_balance)}."
        )
    if recorded.similar_category:
        reply += f"\nℹ️ Similar existing category: '{recorded.similar_category}'"
    return CommandResult(reply=reply)


async def handle_income(args: str, ctx: RequestContext) -> CommandResult:
    try:
        command = parse_money_command(args, "income")
        if command.paid_by:
            raise ValidationError("'paid by' applies to expenses, not income.")
        async with ctx.session_factory() as session:
            recorded = await MoneyService(session).record_income(
                ctx.user,
                category=command.category,
                amount=command.amount,
                via=command.via,
                description=command.description,
                whatsapp_message_id=ctx.message_id,
            )
    except DomainError as exc:
        return CommandResult(reply=exc.message)

    reply = (
        f"✅ Income recorded — {recorded.category.capitalize()} "
        f"{fmt_money(recorded.amount)} ({recorded.via}). "
        f"{recorded.via.capitalize()} balance now {fmt_money(recorded.new_balance)}."
    )
    if recorded.similar_category:
        reply += f"\nℹ️ Similar existing category: '{recorded.similar_category}'"
    return CommandResult(reply=reply)


async def _ledger_reply(ctx: RequestContext, ledger: str) -> CommandResult:
    icon = "💵" if ledger == "cash" else "🏦"
    async with ctx.session_factory() as session:
        repo = LedgerRepository(session)
        balance = await repo.balance(ctx.user.org_id, ledger)
        entries = await repo.recent_entries(ctx.user.org_id, ledger)

    lines = [f"{icon} {ledger.capitalize()} balance: {fmt_money(balance)}"]
    if entries:
        lines.append("Recent:")
        for entry in entries:
            sign = "+" if entry.amount > 0 else "-"
            note = f" — {entry.notes}" if entry.notes else ""
            lines.append(
                f"• {fmt_date(entry.entry_date)} {entry.entry_type.value} "
                f"{sign}{fmt_money(abs(entry.amount))}{note}"
            )
    else:
        lines.append("No entries yet.")
    return CommandResult(reply="\n".join(lines))


async def handle_cash(args: str, ctx: RequestContext) -> CommandResult:
    return await _ledger_reply(ctx, "cash")


async def handle_bank(args: str, ctx: RequestContext) -> CommandResult:
    return await _ledger_reply(ctx, "bank")
