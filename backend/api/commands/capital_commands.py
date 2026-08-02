"""`capital`, `withdraw`, `approve`, `reject` -- docs/08_WhatsApp.md
#capital, #withdraw; postings and the dual-approval rule in
docs/06_Accounting.md §8.

Both directions need a second partner. `approve <id>` answers either --
the pending row says which it was, so nobody has to remember whether
they were sent a contribution or a withdrawal.
"""

from __future__ import annotations

import dataclasses
import decimal
import re

from backend.api.amounts import parse_amount
from backend.api.command_types import CommandResult, Notification, RequestContext
from backend.api.formatting import fmt_money
from backend.api.interactive import Buttons, Choice
from backend.core.dates import split_date
from backend.core.exceptions import DomainError, ValidationError
from backend.services.capital_service import (
    CapitalPending,
    CapitalPosted,
    CapitalService,
)

CAPITAL_USAGE = (
    "Usage: capital <partner> <amount> <cash|bank> [contribution|withdrawal] [on DD-MM-YYYY]"
)
WITHDRAW_USAGE = "Usage: withdraw <partner> <amount> <cash|bank>"

_KINDS = {"contribution", "withdrawal"}
_APPROVAL = re.compile(r"^(?:withdraw(?:al)?\s+)?(?P<ref>[0-9a-f-]{4,36})$", re.IGNORECASE)


@dataclasses.dataclass(frozen=True)
class CapitalCommand:
    partner_name: str
    amount: decimal.Decimal
    via: str
    kind: str
    #: Raw text: only the org's business date can resolve "today".
    on: str | None = None


def parse_capital_command(
    args: str, *, usage: str, force_kind: str | None = None
) -> CapitalCommand:
    """`<partner> <amount> <cash|bank> [kind]`.

    The partner name may contain spaces, so the amount is found first and
    everything before it is the name -- the same shape the `expense`
    grammar avoids by putting the category first.
    """
    # Money that went in weeks ago is entered today; `on 01-07-2026`
    # comes off the line before the amount is looked for.
    args, on = split_date(args)
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
    amount = parse_amount(amount_raw)

    partner_name = " ".join(tokens).strip()
    if not partner_name:
        raise ValidationError(usage)

    assert kind is not None
    return CapitalCommand(partner_name=partner_name, amount=amount, via=via, kind=kind, on=on)


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


def _render_pending(pending: CapitalPending) -> CommandResult:
    """The request, and the buttons the *other* partner needs.

    Worded by direction rather than by one word covering both: "wants to
    put in ₹1,00,000" and "wants to take out ₹1,00,000" are opposite
    facts, and an approver skimming a phone should not have to work out
    which one they are signing.
    """
    waiting_on = ", ".join(name for name, _ in pending.approvers)
    reply = (
        f"🔒 This {pending.noun} ({fmt_money(pending.amount)}) needs approval from another "
        f"partner before it's recorded. Waiting on: {waiting_on}."
    )
    body = (
        f"{pending.partner_name} wants to {pending.direction} "
        f"{fmt_money(pending.amount)} ({pending.via}) as capital.\n"
        f'Reply "approve {pending.short_id}" or "reject {pending.short_id}".'
    )
    approve = Buttons(
        body=(
            f"{pending.partner_name} wants to {pending.direction} "
            f"{fmt_money(pending.amount)} ({pending.via})."
        ),
        choices=(
            Choice(id=f"approve capital {pending.short_id}", title="Approve"),
            Choice(id=f"reject capital {pending.short_id}", title="Reject"),
        ),
        footer="Needs a second partner.",
    )
    return CommandResult(
        reply=reply,
        notifications=tuple(
            Notification(to_number=number, body=body, interactive=approve)
            for _, number in pending.approvers
        ),
    )


async def handle_capital(args: str, ctx: RequestContext) -> CommandResult:
    try:
        command = parse_capital_command(args, usage=CAPITAL_USAGE)
        async with ctx.session_factory() as session:
            service = CapitalService(session)
            record = (
                service.record_contribution
                if command.kind == "contribution"
                else service.record_withdrawal
            )
            outcome = await record(
                ctx.user,
                partner_name=command.partner_name,
                amount=command.amount,
                via=command.via,
                on=command.on,
                whatsapp_message_id=ctx.message_id,
            )
    except DomainError as exc:
        return CommandResult(reply=exc.message)

    if isinstance(outcome, CapitalPending):
        return _render_pending(outcome)
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
                on=command.on,
                whatsapp_message_id=ctx.message_id,
            )
    except DomainError as exc:
        return CommandResult(reply=exc.message)

    if isinstance(outcome, CapitalPending):
        return _render_pending(outcome)
    return CommandResult(reply=_render_posted(outcome))


def _parse_reference(args: str, verb: str) -> str:
    match = _APPROVAL.match(args.strip())
    if match is None:
        raise ValidationError(f"Usage: {verb} <id> — the id is in the request message.")
    return match["ref"]


async def handle_approve(args: str, ctx: RequestContext) -> CommandResult:
    try:
        reference = _parse_reference(args, "approve")
        async with ctx.session_factory() as session:
            posted = await CapitalService(session).approve_request(
                ctx.user, reference, whatsapp_message_id=ctx.message_id
            )
    except DomainError as exc:
        return CommandResult(reply=exc.message)
    return CommandResult(reply="✅ Approved.\n" + _render_posted(posted))


async def handle_reject(args: str, ctx: RequestContext) -> CommandResult:
    try:
        reference = _parse_reference(args, "reject")
        async with ctx.session_factory() as session:
            partner_name, amount, entry_type = await CapitalService(session).reject_request(
                ctx.user, reference, whatsapp_message_id=ctx.message_id
            )
    except DomainError as exc:
        return CommandResult(reply=exc.message)
    noun = "contribution" if entry_type.value == "contribution" else "withdrawal"
    return CommandResult(
        reply=f"🚫 Rejected — {partner_name}'s {fmt_money(amount)} {noun} was not recorded."
    )
