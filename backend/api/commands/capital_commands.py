"""`capital`, `withdraw`, `approve withdraw`, `reject withdraw` --
docs/08_WhatsApp.md #capital, #withdraw; postings and the dual-approval
rule in docs/06_Accounting.md §8.
"""

from __future__ import annotations

import dataclasses
import decimal
import re

from backend.api.command_types import CommandResult, RequestContext
from backend.api.formatting import fmt_money
from backend.core.exceptions import DomainError, ValidationError
from backend.services.capital_service import (
    CapitalPosted,
    CapitalService,
    WithdrawalPending,
)

CAPITAL_USAGE = "Usage: capital <partner> <amount> <cash|bank> [contribution|withdrawal]"
WITHDRAW_USAGE = "Usage: withdraw <partner> <amount> <cash|bank>"

_KINDS = {"contribution", "withdrawal"}
_APPROVAL = re.compile(r"^(?:withdraw(?:al)?\s+)?(?P<ref>[0-9a-f-]{4,36})$", re.IGNORECASE)


@dataclasses.dataclass(frozen=True)
class CapitalCommand:
    partner_name: str
    amount: decimal.Decimal
    via: str
    kind: str


def parse_capital_command(
    args: str, *, usage: str, force_kind: str | None = None
) -> CapitalCommand:
    """`<partner> <amount> <cash|bank> [kind]`.

    The partner name may contain spaces, so the amount is found first and
    everything before it is the name -- the same shape the `expense`
    grammar avoids by putting the category first.
    """
    tokens = args.split()
    if len(tokens) < 3:
        raise ValidationError(usage)

    kind = force_kind
    if force_kind is None:
        kind = "contribution"
        if tokens[-1].lower() in _KINDS:
            kind = tokens.pop().lower()
    elif tokens[-1].lower() in _KINDS:
        # `withdraw Rahul 100 cash withdrawal` -- harmless restatement
        tokens.pop()

    if len(tokens) < 3:
        raise ValidationError(usage)

    via = tokens.pop().lower()
    if via not in {"cash", "bank"}:
        raise ValidationError(f"Say cash or bank. {usage}")

    amount_raw = tokens.pop()
    try:
        amount = decimal.Decimal(amount_raw)
    except decimal.InvalidOperation:
        raise ValidationError(f"'{amount_raw}' is not a number. {usage}") from None

    partner_name = " ".join(tokens).strip()
    if not partner_name:
        raise ValidationError(usage)

    assert kind is not None
    return CapitalCommand(partner_name=partner_name, amount=amount, via=via, kind=kind)


def _render_posted(posted: CapitalPosted) -> str:
    verb = "contribution" if posted.entry_type.value == "contribution" else "withdrawal"
    sign = "+" if verb == "contribution" else "-"
    reply = (
        f"✅ Capital {verb} recorded — {posted.partner_name} "
        f"{sign}{fmt_money(posted.amount)} ({posted.via}). "
        f"{posted.partner_name}'s capital balance now {fmt_money(posted.new_balance)}."
    )
    if posted.negative_balance:
        # allowed, never silently normal -- docs/06_Accounting.md §13
        reply += (
            f"\n⚠️ {posted.partner_name}'s capital balance is now negative — they owe the business."
        )
    return reply


def _render_pending(pending: WithdrawalPending) -> CommandResult:
    waiting_on = ", ".join(name for name, _ in pending.approvers)
    reply = (
        f"🔒 This withdrawal ({fmt_money(pending.amount)}) needs approval from another "
        f"partner before it's processed. Waiting on: {waiting_on}."
    )
    body = (
        f"{pending.partner_name} requested a capital withdrawal of "
        f"{fmt_money(pending.amount)} ({pending.via}).\n"
        f'Reply "approve withdraw {pending.short_id}" or '
        f'"reject withdraw {pending.short_id}".'
    )
    return CommandResult(
        reply=reply,
        notifications=tuple((number, body) for _, number in pending.approvers),
    )


async def handle_capital(args: str, ctx: RequestContext) -> CommandResult:
    try:
        command = parse_capital_command(args, usage=CAPITAL_USAGE)
        async with ctx.session_factory() as session:
            service = CapitalService(session)
            if command.kind == "contribution":
                posted = await service.record_contribution(
                    ctx.user,
                    partner_name=command.partner_name,
                    amount=command.amount,
                    via=command.via,
                    whatsapp_message_id=ctx.message_id,
                )
                return CommandResult(reply=_render_posted(posted))
            # `capital ... withdrawal` is shorthand below the threshold and
            # redirects above it, so the dual-approval path stays the only
            # way a large withdrawal happens (docs/08_WhatsApp.md #capital)
            outcome = await service.record_withdrawal(
                ctx.user,
                partner_name=command.partner_name,
                amount=command.amount,
                via=command.via,
                whatsapp_message_id=ctx.message_id,
            )
    except DomainError as exc:
        return CommandResult(reply=exc.message)

    if isinstance(outcome, WithdrawalPending):
        result = _render_pending(outcome)
        return dataclasses.replace(
            result,
            reply=(
                f"ℹ️ {fmt_money(outcome.amount)} is at or above the "
                f"{fmt_money(outcome.threshold)} dual-approval threshold, so I've routed "
                f"it through the 'withdraw' flow.\n{result.reply}"
            ),
        )
    return CommandResult(reply=_render_posted(outcome))


async def handle_withdraw(args: str, ctx: RequestContext) -> CommandResult:
    try:
        command = parse_capital_command(args, usage=WITHDRAW_USAGE, force_kind="withdrawal")
        async with ctx.session_factory() as session:
            outcome = await CapitalService(session).record_withdrawal(
                ctx.user,
                partner_name=command.partner_name,
                amount=command.amount,
                via=command.via,
                whatsapp_message_id=ctx.message_id,
            )
    except DomainError as exc:
        return CommandResult(reply=exc.message)

    if isinstance(outcome, WithdrawalPending):
        return _render_pending(outcome)
    return CommandResult(reply=_render_posted(outcome))


def _parse_reference(args: str, verb: str) -> str:
    match = _APPROVAL.match(args.strip())
    if match is None:
        raise ValidationError(f"Usage: {verb} withdraw <id> — the id is in the request message.")
    return match["ref"]


async def handle_approve(args: str, ctx: RequestContext) -> CommandResult:
    try:
        reference = _parse_reference(args, "approve")
        async with ctx.session_factory() as session:
            posted = await CapitalService(session).approve_withdrawal(
                ctx.user, reference, whatsapp_message_id=ctx.message_id
            )
    except DomainError as exc:
        return CommandResult(reply=exc.message)
    return CommandResult(reply="✅ Approved.\n" + _render_posted(posted))


async def handle_reject(args: str, ctx: RequestContext) -> CommandResult:
    try:
        reference = _parse_reference(args, "reject")
        async with ctx.session_factory() as session:
            partner_name, amount = await CapitalService(session).reject_withdrawal(
                ctx.user, reference, whatsapp_message_id=ctx.message_id
            )
    except DomainError as exc:
        return CommandResult(reply=exc.message)
    return CommandResult(
        reply=f"🚫 Rejected — {partner_name}'s {fmt_money(amount)} withdrawal was not processed."
    )
