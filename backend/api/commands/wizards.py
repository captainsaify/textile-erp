"""Command wizards -- docs/20_ConversationalIntake.md §7.

> A partial command is a question, not an error.

`paid` on its own used to print a usage line. It now asks "Which
supplier?" and offers the ones you actually trade with. The same applies
to `received`, `sale`, `expense`, `income` and `export`.

**How equivalence is guaranteed.** A finished wizard does not call a
service. It assembles the canonical one-shot argument string and hands
it to the command's existing handler — so the wizard cannot drift from
the typed form, because it *becomes* the typed form (§10.5). One place
parses, one place validates, one place posts.

A complete command still runs in one shot, untouched: for someone
fluent, typing it fully is one round trip instead of four.
"""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import Awaitable, Callable
from typing import Any

from backend.api.amounts import looks_like_amount, parse_amount
from backend.api.command_types import CommandResult, RequestContext
from backend.api.interactive import Buttons, Choice, Interactive, ListMenu, Section
from backend.core.exceptions import DomainError, ValidationError
from backend.services.session_service import (
    AWAITING_COMMAND_SLOT,
    IDLE,
    SessionService,
    SessionState,
)

#: A list menu caps at 10 rows (docs/19 §2), and one is spent on the
#: escape hatch for a name that isn't listed.
PARTY_ROWS = 9

ChoiceBuilder = Callable[[RequestContext], Awaitable[Interactive | None]]


@dataclasses.dataclass(frozen=True)
class CommandSlot:
    """One missing argument: how to ask, how to offer, how to check."""

    name: str
    question: str
    #: Builds buttons or a list when the options are knowable. Returning
    #: None means "this one has to be typed" -- a supplier the business
    #: has never traded with cannot be offered.
    choices: ChoiceBuilder | None = None
    #: Raises ValidationError with copy that names the problem. Returns
    #: the value as it should appear in the assembled command.
    validate: Callable[[str], str] = lambda value: value.strip()
    example: str = ""


@dataclasses.dataclass(frozen=True)
class CommandWizard:
    command: str
    slots: tuple[CommandSlot, ...]
    #: Assembles the canonical argument string the typed command takes.
    assemble: Callable[[dict[str, str]], str]
    #: What the user already typed, mapped onto slots, so `paid wagdia`
    #: asks only for the amount and the method.
    prefill: Callable[[str], dict[str, str]] = lambda args: {}


# --------------------------------------------------------------------
# validators
# --------------------------------------------------------------------


def _amount(value: str) -> str:
    return str(parse_amount(value, field="Amount"))


def _quantity(value: str) -> str:
    quantity = parse_amount(value, field="Quantity")
    return str(quantity)


def _method(value: str) -> str:
    token = value.strip().lower()
    if token not in {"cash", "bank"}:
        raise ValidationError(f"'{value.strip()}' isn't a payment method — say cash or bank.")
    return token


def _code(value: str) -> str:
    token = value.strip().upper()
    if not token or " " in token:
        raise ValidationError("A product code is a single word, e.g. TRP.")
    return token


def _nonempty(label: str) -> Callable[[str], str]:
    def check(value: str) -> str:
        text = value.strip().rstrip(":")
        if not text:
            raise ValidationError(f"{label} can't be blank.")
        return text

    return check


# --------------------------------------------------------------------
# choice builders
# --------------------------------------------------------------------


def _party_menu(names: list[str], *, label: str, body: str) -> Interactive | None:
    """The parties this business actually deals with, most recent first.

    Typing still works and still matches fuzzily -- the list is a
    shortcut, never a restriction (docs/20 §9).
    """
    rows = tuple(
        Choice(id=f"slot {name}", title=name[:24])
        for name in list(dict.fromkeys(names))[:PARTY_ROWS]
    )
    if not rows:
        return None
    return ListMenu(
        body=body,
        menu_label=f"Pick {label}",
        sections=(
            Section(title="Recent", rows=rows),
            Section(
                title="Or",
                rows=(
                    Choice(id="slot new", title="Someone else", description="You'll type the name"),
                ),
            ),
        ),
    )


async def _suppliers(ctx: RequestContext) -> Interactive | None:
    from backend.repositories.party_repository import SupplierRepository

    async with ctx.session_factory() as session:
        found = await SupplierRepository(session).search(ctx.user.org_id, "", limit=PARTY_ROWS)
    return _party_menu(
        [s.name for s in found], label="supplier", body="Which supplier is this for?"
    )


async def _customers(ctx: RequestContext) -> Interactive | None:
    from backend.repositories.party_repository import CustomerRepository

    async with ctx.session_factory() as session:
        found = await CustomerRepository(session).search(ctx.user.org_id, "", limit=PARTY_ROWS)
    return _party_menu(
        [c.name for c in found], label="customer", body="Which customer is this for?"
    )


async def _method_buttons(ctx: RequestContext) -> Interactive | None:
    return Buttons(
        body="Cash or bank?",
        choices=(
            Choice(id="slot cash", title="Cash"),
            Choice(id="slot bank", title="Bank"),
        ),
    )


async def _expense_categories(ctx: RequestContext) -> Interactive | None:
    """Categories this business has actually used, rather than a fixed
    list somebody guessed at."""
    from backend.repositories.accounting_repository import ExpenseRepository

    async with ctx.session_factory() as session:
        used = await ExpenseRepository(session).distinct_categories(ctx.user.org_id)
    rows = tuple(Choice(id=f"slot {name}", title=name[:24]) for name in used[:PARTY_ROWS])
    if not rows:
        return None
    return ListMenu(
        body="What kind of expense?",
        menu_label="Pick category",
        sections=(
            Section(title="Used before", rows=rows),
            Section(
                title="Or",
                rows=(Choice(id="slot new", title="Something else", description="You'll type it"),),
            ),
        ),
    )


async def _report_buttons(ctx: RequestContext) -> Interactive | None:
    return Buttons(
        body="Which report?",
        choices=(
            Choice(id="slot purchases", title="Purchases"),
            Choice(id="slot sales", title="Sales"),
            Choice(id="slot stock", title="Stock"),
        ),
    )


async def _period_menu(ctx: RequestContext) -> Interactive | None:
    from backend.api.period import period_menu

    menu = period_menu("slot")
    return dataclasses.replace(menu, body="Which period?")


# --------------------------------------------------------------------
# prefill -- what the user already typed
# --------------------------------------------------------------------


def _prefill_settlement(args: str) -> dict[str, str]:
    """`paid wagdia` -> the party is known, ask the rest.

    Deliberately conservative: anything it cannot place with certainty
    is left unfilled, because a wrong guess here is a wrong payment.
    """
    tokens = args.split()
    if tokens and tokens[0].lower() in {"supplier:", "supplier", "customer:", "customer"}:
        tokens = tokens[1:]
    filled: dict[str, str] = {}

    method = next((t for t in tokens if t.lower() in {"cash", "bank"}), None)
    if method is not None:
        filled["method"] = method.lower()
        tokens = [t for t in tokens if t.lower() not in {"cash", "bank"}]

    amount_at = next(
        (i for i in range(len(tokens) - 1, -1, -1) if looks_like_amount(tokens[i])), None
    )
    if amount_at is not None:
        try:
            filled["amount"] = str(parse_amount(tokens[amount_at]))
        except DomainError:
            amount_at = None
    party = " ".join(tokens[:amount_at] if amount_at is not None else tokens).strip().rstrip(":")
    if party:
        filled["party"] = party
    return filled


def _prefill_money(args: str) -> dict[str, str]:
    tokens = args.split()
    filled: dict[str, str] = {}
    if tokens:
        filled["category"] = tokens[0]
    if len(tokens) > 1 and looks_like_amount(tokens[1]):
        # a malformed amount is left unfilled so the slot asks for it,
        # rather than the command failing on something the user can fix
        with contextlib.suppress(DomainError):
            filled["amount"] = str(parse_amount(tokens[1]))
    if len(tokens) > 2 and tokens[2].lower() in {"cash", "bank"}:
        filled["method"] = tokens[2].lower()
    return filled


def _prefill_export(args: str) -> dict[str, str]:
    tokens = args.split()
    filled: dict[str, str] = {}
    if tokens and tokens[0].lower() in {"purchases", "sales", "stock"}:
        filled["report"] = tokens[0].lower()
        if len(tokens) > 1:
            filled["period"] = " ".join(tokens[1:])
    return filled


# --------------------------------------------------------------------
# the wizards
# --------------------------------------------------------------------


def _settlement_wizard(command: str, party_label: str, choices: ChoiceBuilder) -> CommandWizard:
    return CommandWizard(
        command=command,
        slots=(
            CommandSlot(
                name="party",
                question=f"Which {party_label}?",
                choices=choices,
                validate=_nonempty(party_label.capitalize()),
                example="e.g. Wagdia",
            ),
            CommandSlot(
                name="amount",
                question="How much?",
                validate=_amount,
                example="e.g. 40000",
            ),
            CommandSlot(
                name="method",
                question="Cash or bank?",
                choices=_method_buttons,
                validate=_method,
            ),
        ),
        assemble=lambda f: f"{f['party']} {f['amount']} {f['method']}",
        prefill=_prefill_settlement,
    )


def _money_wizard(command: str, choices: ChoiceBuilder | None) -> CommandWizard:
    return CommandWizard(
        command=command,
        slots=(
            CommandSlot(
                name="category",
                question=f"What kind of {command}?",
                choices=choices,
                validate=_nonempty("Category"),
                example="e.g. transport",
            ),
            CommandSlot(name="amount", question="How much?", validate=_amount, example="e.g. 1500"),
            CommandSlot(
                name="method", question="Cash or bank?", choices=_method_buttons, validate=_method
            ),
        ),
        assemble=lambda f: f"{f['category']} {f['amount']} {f['method']}",
        prefill=_prefill_money,
    )


WIZARDS: dict[str, CommandWizard] = {
    "paid": _settlement_wizard("paid", "supplier", _suppliers),
    "received": _settlement_wizard("received", "customer", _customers),
    "expense": _money_wizard("expense", _expense_categories),
    "income": _money_wizard("income", None),
    "sale": CommandWizard(
        command="sale",
        slots=(
            CommandSlot(
                name="customer",
                question="Which customer?",
                choices=_customers,
                validate=_nonempty("Customer"),
                example="e.g. Ravi Traders",
            ),
            CommandSlot(
                name="code", question="Which product code?", validate=_code, example="e.g. TRP"
            ),
            CommandSlot(name="qty", question="How many?", validate=_quantity, example="e.g. 100"),
            CommandSlot(
                name="rate", question="At what rate per unit?", validate=_amount, example="e.g. 150"
            ),
        ),
        # the sale grammar is two lines: header, then one line per item
        assemble=lambda f: f"Customer: {f['customer']}\n{f['code']} {f['qty']} {f['rate']}",
    ),
    "export": CommandWizard(
        command="export",
        slots=(
            CommandSlot(
                name="report",
                question="Which report?",
                choices=_report_buttons,
                validate=_nonempty("Report"),
            ),
            CommandSlot(
                name="period",
                question="Which period?",
                choices=_period_menu,
                validate=_nonempty("Period"),
                example="e.g. month",
            ),
        ),
        assemble=lambda f: f"{f['report']} {f['period']}",
        prefill=_prefill_export,
    ),
}


# --------------------------------------------------------------------
# running one
# --------------------------------------------------------------------


def missing(wizard: CommandWizard, args: str) -> tuple[dict[str, str], list[str]]:
    """What the typed args already answered, and what is still needed."""
    filled = {k: v for k, v in wizard.prefill(args).items() if v}
    queue = [slot.name for slot in wizard.slots if slot.name not in filled]
    return filled, queue


async def ask(wizard: CommandWizard, slot_name: str, ctx: RequestContext) -> CommandResult:
    slot = next(s for s in wizard.slots if s.name == slot_name)
    body = f"{slot.question}\n{slot.example}".strip()
    interactive = await slot.choices(ctx) if slot.choices is not None else None
    return CommandResult(reply=body, interactive=interactive)


async def start(wizard: CommandWizard, args: str, ctx: RequestContext) -> CommandResult | None:
    """None means the command was complete -- run it as typed."""
    filled, queue = missing(wizard, args)
    if not queue:
        return None
    await SessionService(ctx.session_factory).set(
        ctx.user.org_id,
        ctx.user.id,
        AWAITING_COMMAND_SLOT,
        {"command": wizard.command, "filled": filled, "queue": queue},
    )
    return await ask(wizard, queue[0], ctx)


async def handle_reply(text: str, ctx: RequestContext, state: SessionState) -> CommandResult:
    """One answer fills the head of the queue; an empty queue runs the
    command exactly as if it had been typed in full."""
    context: dict[str, Any] = dict(state.context)
    wizard = WIZARDS[str(context["command"])]
    filled: dict[str, str] = dict(context.get("filled", {}))
    queue: list[str] = list(context.get("queue", []))
    sessions = SessionService(ctx.session_factory)

    answer = text.strip().removeprefix("slot ").strip()
    lowered = answer.lower()

    if lowered == "cancel":
        await sessions.set(ctx.user.org_id, ctx.user.id, IDLE, {})
        return CommandResult(reply=f"Cancelled — no {wizard.command} was recorded.")

    if not queue:  # defensive; an empty queue should have run already
        await sessions.set(ctx.user.org_id, ctx.user.id, IDLE, {})
        return CommandResult(reply=f"That {wizard.command} is finished. Start again when ready.")

    if lowered == "back":
        done = list(filled)
        if not done:
            return await _reask(wizard, queue, ctx, prefix="That's the first question.")
        previous = done[-1]
        filled.pop(previous)
        queue.insert(0, previous)
        await _save(sessions, ctx, wizard, filled, queue)
        return await _reask(wizard, queue, ctx, prefix="Going back.")

    current = queue[0]
    if lowered in {"new", "other", "custom"}:
        # "Someone else" / "Custom range": the row can't answer itself
        return await _reask(wizard, queue, ctx, prefix="Go ahead and type it.")

    slot = next(s for s in wizard.slots if s.name == current)
    try:
        filled[current] = slot.validate(answer)
    except DomainError as exc:
        return await _reask(wizard, queue, ctx, prefix=exc.message)

    queue.pop(0)
    if queue:
        await _save(sessions, ctx, wizard, filled, queue)
        return await ask(wizard, queue[0], ctx)

    # Complete. Hand the assembled one-shot to the real handler, so the
    # wizard and the typed command cannot diverge (§10.5).
    from backend.api.whatsapp_commands import COMMAND_REGISTRY

    await sessions.set(ctx.user.org_id, ctx.user.id, IDLE, {})
    return await COMMAND_REGISTRY[wizard.command].handler(wizard.assemble(filled), ctx)


async def _save(
    sessions: SessionService,
    ctx: RequestContext,
    wizard: CommandWizard,
    filled: dict[str, str],
    queue: list[str],
) -> None:
    await sessions.set(
        ctx.user.org_id,
        ctx.user.id,
        AWAITING_COMMAND_SLOT,
        {"command": wizard.command, "filled": filled, "queue": queue},
    )


async def _reask(
    wizard: CommandWizard, queue: list[str], ctx: RequestContext, *, prefix: str
) -> CommandResult:
    question = await ask(wizard, queue[0], ctx)
    return dataclasses.replace(question, reply=f"{prefix}\n{question.reply}")
