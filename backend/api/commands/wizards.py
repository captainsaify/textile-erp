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
from backend.api.formatting import fmt_date, fmt_money
from backend.api.interactive import (
    Buttons,
    Choice,
    Interactive,
    ListMenu,
    Section,
    is_abandon,
)
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

#: Choices may depend on answers already given -- the field list for
#: `edit` is the fields of the record kind just chosen -- so a builder
#: receives what has been filled so far rather than reaching for it.
ChoiceBuilder = Callable[[RequestContext, dict[str, str]], Awaitable[Interactive | None]]


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
    #: Runs after a valid answer is stored, and may rewrite `filled`.
    #: Clearing a slot's own answer is what lets a wizard loop: a sale
    #: collects one item, banks it, and asks for the next.
    after: Callable[[dict[str, str]], None] | None = None
    #: Whether this slot is needed at all, given what is already filled.
    #: `export` asks which supplier only when the chosen report is about
    #: one -- a queue fixed up front cannot express that.
    applies: Callable[[dict[str, str]], bool] = lambda filled: True


@dataclasses.dataclass(frozen=True)
class CommandWizard:
    command: str
    slots: tuple[CommandSlot, ...]
    #: Assembles the canonical argument string the typed command takes.
    assemble: Callable[[dict[str, str]], str]
    #: What the user already typed, mapped onto slots, so `paid wagdia`
    #: asks only for the amount and the method.
    prefill: Callable[[str], dict[str, str]] = lambda args: {}
    #: Lets a finished wizard hand off to a *different* command. Deleting
    #: a bill is really `undo`, and routing there is what turns "you
    #: can't do that here" into the thing the person wanted.
    reroute: Callable[[dict[str, str]], tuple[str, str] | None] = lambda filled: None


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


def _capital_kind(value: str) -> str:
    token = value.strip().lower()
    if token not in {"contribution", "withdrawal"}:
        raise ValidationError(
            f"'{value.strip()}' isn't a kind — say contribution (money in) or "
            "withdrawal (money out)."
        )
    return token


def _affirmative(value: str) -> str:
    token = value.strip().lower()
    if token not in {"delete", "yes", "confirm"}:
        # anything else is treated as not-yes; `cancel` is handled by the
        # wizard's own escape hatch before this ever runs
        raise ValidationError("Tap 'Yes, delete' to go ahead, or 'Keep it' to stop.")
    return token


#: A confirmed bill is never edited or deleted in place -- stock and the
#: books were already derived from it. These route to `undo`, which
#: posts a compensating reversal (docs/04_Purchases.md §8).
REVERSIBLE_ENTITIES = {"purchase", "sale", "expense"}


def _entity_kind(value: str) -> str:
    token = value.strip().lower().rstrip("s")
    if token not in {"product", "supplier", "customer", "brand", *REVERSIBLE_ENTITIES}:
        raise ValidationError(
            f"I can't change a '{value.strip()}'. Pick product, supplier, customer, "
            "brand, purchase, sale or expense."
        )
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


async def _suppliers(ctx: RequestContext, filled: dict[str, str]) -> Interactive | None:
    from backend.repositories.party_repository import SupplierRepository

    async with ctx.session_factory() as session:
        found = await SupplierRepository(session).search(ctx.user.org_id, "", limit=PARTY_ROWS)
    return _party_menu(
        [s.name for s in found], label="supplier", body="Which supplier is this for?"
    )


async def _customers(ctx: RequestContext, filled: dict[str, str]) -> Interactive | None:
    from backend.repositories.party_repository import CustomerRepository

    async with ctx.session_factory() as session:
        found = await CustomerRepository(session).search(ctx.user.org_id, "", limit=PARTY_ROWS)
    return _party_menu(
        [c.name for c in found], label="customer", body="Which customer is this for?"
    )


async def _method_buttons(ctx: RequestContext, filled: dict[str, str]) -> Interactive | None:
    return Buttons(
        body="Cash or bank?",
        choices=(
            Choice(id="slot cash", title="Cash"),
            Choice(id="slot bank", title="Bank"),
        ),
    )


async def _expense_categories(ctx: RequestContext, filled: dict[str, str]) -> Interactive | None:
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


async def _partners(ctx: RequestContext, filled: dict[str, str]) -> Interactive | None:
    from backend.repositories.party_repository import PartnerRepository

    async with ctx.session_factory() as session:
        found = await PartnerRepository(session).list_active(ctx.user.org_id)
    return _party_menu([p.display_name for p in found], label="partner", body="Which partner?")


async def _capital_kind_buttons(ctx: RequestContext, filled: dict[str, str]) -> Interactive | None:
    return Buttons(
        body="Money in, or money out?",
        choices=(
            Choice(id="slot contribution", title="Contribution"),
            Choice(id="slot withdrawal", title="Withdrawal"),
        ),
    )


async def _entity_menu(ctx: RequestContext, filled: dict[str, str]) -> Interactive | None:
    return ListMenu(
        body="What are you changing?",
        menu_label="Pick a kind",
        sections=(
            Section(
                title="Records",
                rows=(
                    Choice(id="slot product", title="Product", description="Code or description"),
                    Choice(id="slot supplier", title="Supplier", description="Name or contact"),
                    Choice(id="slot customer", title="Customer", description="Name or contact"),
                    Choice(id="slot brand", title="Brand", description="Name"),
                ),
            ),
            # Offering only the four master records left someone wanting
            # to remove a wrong bill staring at a menu that didn't
            # mention bills, with nothing explaining why. These rows are
            # here to say what happens instead, and then do it.
            Section(
                title="Bills — reversed",
                rows=(
                    Choice(
                        id="slot purchase",
                        title="Purchase",
                        description="Reversed with a compensating entry",
                    ),
                    Choice(
                        id="slot sale",
                        title="Sale",
                        description="Reversed; stock goes back",
                    ),
                    Choice(
                        id="slot expense",
                        title="Expense",
                        description="Reversed; money goes back",
                    ),
                ),
            ),
        ),
    )


#: Which fields each record actually has. Mirrors what the `edit`
#: command accepts -- offering a field it would reject is worse than
#: offering nothing.
EDITABLE_FIELDS = {
    "product": ("code", "description", "reorder_level", "brand"),
    "supplier": ("name", "phone", "address"),
    "customer": ("name", "phone", "address", "credit_limit"),
    "brand": ("name",),
}


async def _field_menu(ctx: RequestContext, filled: dict[str, str]) -> Interactive | None:
    """Built from the entity already chosen, which is why the wizard
    recomputes its queue after every answer."""
    entity = filled.get("entity", "")
    fields = EDITABLE_FIELDS.get(entity)
    if not fields:
        return None
    return ListMenu(
        body=f"Which field of the {entity}?",
        menu_label="Pick field",
        sections=(
            Section(
                title=entity.capitalize(),
                rows=tuple(Choice(id=f"slot {name}", title=name) for name in fields),
            ),
        ),
    )


async def _reference_menu(ctx: RequestContext, filled: dict[str, str]) -> Interactive | None:
    """Which record, offered as a list where one exists.

    A bill has no code and no name, so asking "give its code or name,
    e.g. TRP" -- the prompt a product needs -- was asking a product
    question about a sale. Master records still type theirs; there can
    be hundreds and no useful recency order.
    """
    entity = filled.get("entity", "")
    rows: tuple[Choice, ...] = ()

    if entity == "purchase":
        from backend.repositories.purchase_repository import PurchaseRepository

        async with ctx.session_factory() as session:
            recent = await PurchaseRepository(session).recent_invoices(
                ctx.user.org_id, limit=PARTY_ROWS
            )
        rows = tuple(
            Choice(
                id=f"slot {invoice_no}",
                title=invoice_no[:24],
                description=f"{supplier} · {fmt_date(day)}"[:72],
            )
            for invoice_no, supplier, day in recent
        )
    elif entity == "sale":
        from backend.repositories.purchase_repository import SalesLookupRepository

        async with ctx.session_factory() as session:
            recent_sales = await SalesLookupRepository(session).recent(
                ctx.user.org_id, limit=PARTY_ROWS
            )
        rows = tuple(
            Choice(
                id=f"slot {short_id}",
                title=f"{customer[:14]} {fmt_date(day)}"[:24],
                description=f"{fmt_money(total)} · ref {short_id}"[:72],
            )
            for short_id, customer, day, total in recent_sales
        )

    elif entity == "expense":
        from backend.repositories.accounting_repository import ExpenseRepository

        async with ctx.session_factory() as session:
            recent_expenses = await ExpenseRepository(session).recent(
                ctx.user.org_id, limit=PARTY_ROWS
            )
        rows = tuple(
            Choice(
                id=f"slot {short_id}",
                title=f"{category[:14]} {fmt_date(day)}"[:24],
                description=f"{fmt_money(amount)} · ref {short_id}"[:72],
            )
            for short_id, category, day, amount in recent_expenses
        )

    if not rows:
        return None
    return ListMenu(
        body=f"Which {entity}?",
        menu_label=f"Pick {entity}",
        sections=(
            Section(title="Recent", rows=rows),
            Section(
                title="Or",
                rows=(
                    Choice(id="slot other", title="Another one", description="You'll type the ref"),
                ),
            ),
        ),
    )


async def _delete_confirm(ctx: RequestContext, filled: dict[str, str]) -> Interactive | None:
    """Names the thing being deleted.

    A wizard makes a destructive command *easier* to reach -- three taps
    from nothing to gone -- so the last of those taps has to state what
    it is about to do, not just say "confirm?".
    """
    entity = filled.get("entity", "record")
    what = f"{entity} {filled.get('reference', '')}".strip()
    if entity in REVERSIBLE_ENTITIES:
        return Buttons(
            body=f"Reverse {what}?",
            choices=(
                Choice(id="slot delete", title="Yes, reverse"),
                Choice(id="slot cancel", title="Keep it"),
            ),
            footer="Stock and the books are put back by a compensating entry.",
        )
    return Buttons(
        body=f"Delete {what}?",
        choices=(
            Choice(id="slot delete", title="Yes, delete"),
            Choice(id="slot cancel", title="Keep it"),
        ),
        footer="It stays on past transactions either way.",
    )


async def _report_menu(ctx: RequestContext, filled: dict[str, str]) -> Interactive | None:
    """More than three things are exportable, so this is a list rather
    than buttons (docs/19 §2: buttons cap at 3)."""
    return ListMenu(
        body="What would you like to export?",
        menu_label="Pick report",
        sections=(
            Section(
                title="Purchases",
                rows=(
                    Choice(id="slot purchases", title="All purchases", description="For a period"),
                    Choice(
                        id="slot purchases-supplier",
                        title="By supplier",
                        description="One supplier's purchases",
                    ),
                    Choice(id="slot invoice", title="One invoice", description="A single bill"),
                ),
            ),
            Section(
                title="Sales",
                rows=(
                    Choice(id="slot sales", title="All sales", description="For a period"),
                    Choice(
                        id="slot sales-customer",
                        title="By customer",
                        description="One customer's sales",
                    ),
                ),
            ),
            Section(
                title="Statements",
                rows=(
                    Choice(
                        id="slot statement-supplier",
                        title="Supplier statement",
                        description="Bills, payments, balance",
                    ),
                    Choice(
                        id="slot statement-customer",
                        title="Customer statement",
                        description="Sales, receipts, balance",
                    ),
                ),
            ),
            Section(
                title="Ledgers",
                rows=(
                    Choice(
                        id="slot ledger-supplier",
                        title="Supplier ledger",
                        description="Who we owe, and how old",
                    ),
                    Choice(
                        id="slot ledger-customer",
                        title="Customer ledger",
                        description="Who owes us, and how old",
                    ),
                ),
            ),
            Section(
                title="Stock",
                rows=(Choice(id="slot stock", title="Stock on hand", description="With value"),),
            ),
        ),
    )


async def _invoice_menu(ctx: RequestContext, filled: dict[str, str]) -> Interactive | None:
    """Recent invoices, so a bill can be picked rather than remembered.
    Scoped to the chosen supplier when there is one."""
    from backend.repositories.purchase_repository import PurchaseRepository

    async with ctx.session_factory() as session:
        recent = await PurchaseRepository(session).recent_invoices(
            ctx.user.org_id, limit=PARTY_ROWS
        )
    rows = tuple(
        Choice(
            id=f"slot {invoice_no}",
            title=invoice_no[:24],
            description=f"{supplier} · {fmt_date(day)}"[:72],
        )
        for invoice_no, supplier, day in recent
    )
    if not rows:
        return None
    return ListMenu(
        body="Which invoice?",
        menu_label="Pick invoice",
        sections=(
            Section(title="Recent", rows=rows),
            Section(
                title="Or",
                rows=(
                    Choice(
                        id="slot new", title="Another one", description="You'll type the number"
                    ),
                ),
            ),
        ),
    )


async def _period_menu(ctx: RequestContext, filled: dict[str, str]) -> Interactive | None:
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


#: Menu row id -> (report type the command takes, what else it needs).
_REPORT_CHOICES = {
    "purchases": "purchases",
    "purchases-supplier": "purchases",
    "sales": "sales",
    "sales-customer": "sales",
    "statement-supplier": "statement",
    "statement-customer": "statement",
    "invoice": "invoice",
    "ledger-supplier": "ledger",
    "ledger-customer": "ledger",
    "stock": "stock",
}


def _report_choice(value: str) -> str:
    token = value.strip().lower()
    if token not in _REPORT_CHOICES:
        raise ValidationError(
            f"'{value.strip()}' isn't something I can export. "
            "Pick one from the list, or say purchases, sales, stock, statement or invoice."
        )
    return token


def _assemble_export(filled: dict[str, str]) -> str:
    """Turn the answers into the canonical `export ...` line, so the
    wizard runs the same command anyone could have typed."""
    choice = filled["report"]
    report_type = _REPORT_CHOICES[choice]
    if report_type == "invoice":
        return f"invoice {filled['invoice']}"

    parts = [report_type]
    if report_type == "ledger":
        # a ledger is every party of one kind, so the role is the whole
        # argument -- there is no single party to name
        return f"ledger {'customer' if choice.endswith('-customer') else 'supplier'}"
    if choice.endswith("-supplier"):
        parts += ["supplier", filled["supplier"]]
    elif choice.endswith("-customer"):
        parts += ["customer", filled["customer"]]
    parts.append(filled.get("period", "month"))
    return " ".join(parts)


async def _more_items_buttons(ctx: RequestContext, filled: dict[str, str]) -> Interactive | None:
    counted = len(_items_of(filled)) + 1
    return Buttons(
        body=f"{counted} item(s) so far. Anything else?",
        choices=(
            Choice(id="slot add", title="Add another"),
            Choice(id="slot done", title="That's all"),
        ),
    )


def _more_items(value: str) -> str:
    token = value.strip().lower()
    if token in {"add", "another", "more", "yes"}:
        return "add"
    if token in {"done", "no", "that's all", "thats all", "finish"}:
        return "done"
    raise ValidationError("Tap 'Add another' or \"That's all\".")


def _items_of(filled: dict[str, str]) -> list[str]:
    banked = filled.get("items", "")
    return [line for line in banked.split("\n") if line.strip()]


def _bank_item(filled: dict[str, str]) -> None:
    """Move the item just answered into the collected list.

    When there is another to come, the per-item slots are cleared so the
    queue asks for them again -- that is the loop. `filled` is the only
    state a wizard has, so the loop lives in it rather than in a counter
    somewhere else that could disagree.
    """
    item = f"{filled.get('code', '')} {filled.get('qty', '')} {filled.get('rate', '')}".strip()
    if item:
        filled["items"] = "\n".join([*_items_of(filled), item])
    for key in ("code", "qty", "rate"):
        filled.pop(key, None)
    if filled.get("more") == "add":
        filled.pop("more", None)


def _assemble_sale(filled: dict[str, str]) -> str:
    """The sale grammar: a header line, then one line per item."""
    items = _items_of(filled)
    return "\n".join([f"Customer: {filled['customer']}", *items])


def _party_lookup_wizard(role: str, choices: ChoiceBuilder) -> CommandWizard:
    """`supplier` / `customer` with no name asks which one, rather than
    printing a usage line for a command whose only argument is a name
    the system already knows."""
    return CommandWizard(
        command=role,
        slots=(
            CommandSlot(
                name="name",
                question=f"Which {role}?",
                choices=choices,
                validate=_nonempty(role.capitalize()),
                example="e.g. Wagdia",
            ),
        ),
        assemble=lambda f: f["name"],
        prefill=lambda args: {"name": args.strip()} if args.strip() else {},
    )


def _prefill_capital(args: str) -> dict[str, str]:
    """`capital Rahul 50000 cash` is complete; anything less is asked for.

    Conservative on purpose -- a wrong guess here posts the wrong
    partner's capital, which is a correction with a paper trail rather
    than a typo.
    """
    tokens = args.split()
    filled: dict[str, str] = {}

    kind = next((t for t in tokens if t.lower() in {"contribution", "withdrawal"}), None)
    if kind is not None:
        filled["kind"] = kind.lower()
        tokens = [t for t in tokens if t.lower() != kind.lower()]

    method = next((t for t in tokens if t.lower() in {"cash", "bank"}), None)
    if method is not None:
        filled["method"] = method.lower()
        tokens = [t for t in tokens if t.lower() not in {"cash", "bank"}]

    amount_at = next(
        (i for i in range(len(tokens) - 1, -1, -1) if looks_like_amount(tokens[i])), None
    )
    if amount_at is not None:
        with contextlib.suppress(DomainError):
            filled["amount"] = str(parse_amount(tokens[amount_at]))
    partner = " ".join(tokens[:amount_at] if amount_at is not None else tokens).strip()
    if partner:
        filled["partner"] = partner
    return filled


def _prefill_entity(args: str) -> dict[str, str]:
    """Note what this never fills: `confirm`. `delete product TRP` typed
    in full still stops to ask, because the destructive step is the one
    thing that should not be skippable by knowing the syntax."""
    tokens = args.split()
    filled: dict[str, str] = {}
    if tokens:
        with contextlib.suppress(DomainError):
            filled["entity"] = _entity_kind(tokens[0])
    if len(tokens) > 1 and "entity" in filled:
        filled["reference"] = tokens[1]
    if len(tokens) > 2 and "reference" in filled:
        filled["field"] = tokens[2]
    if len(tokens) > 3 and "field" in filled:
        filled["value"] = " ".join(tokens[3:])
    return filled


def _prefill_export(args: str) -> dict[str, str]:
    """Only the plain forms prefill. `export purchases supplier ...` is
    already complete enough for the command itself to parse, and
    half-reading it here would risk asking for something already said."""
    tokens = args.split()
    filled: dict[str, str] = {}
    if tokens and tokens[0].lower() in {"purchases", "sales", "stock"}:
        filled["report"] = tokens[0].lower()
        if len(tokens) > 1 and tokens[1].lower() not in {"supplier", "customer"}:
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
            CommandSlot(
                name="more",
                question="Anything else on this sale?",
                choices=_more_items_buttons,
                validate=_more_items,
                after=_bank_item,
            ),
        ),
        assemble=_assemble_sale,
    ),
    "export": CommandWizard(
        command="export",
        slots=(
            CommandSlot(
                name="report",
                question="What would you like to export?",
                choices=_report_menu,
                validate=_report_choice,
            ),
            CommandSlot(
                name="supplier",
                question="Which supplier?",
                choices=_suppliers,
                validate=_nonempty("Supplier"),
                applies=lambda f: (
                    f.get("report", "") in {"purchases-supplier", "statement-supplier"}
                ),
            ),
            CommandSlot(
                name="customer",
                question="Which customer?",
                choices=_customers,
                validate=_nonempty("Customer"),
                applies=lambda f: f.get("report", "") in {"sales-customer", "statement-customer"},
            ),
            CommandSlot(
                name="invoice",
                question="Which invoice?",
                choices=_invoice_menu,
                validate=_nonempty("Invoice"),
                example="e.g. INV-001",
                applies=lambda f: f.get("report") == "invoice",
            ),
            CommandSlot(
                name="period",
                question="Which period?",
                choices=_period_menu,
                validate=_nonempty("Period"),
                example="e.g. month",
                # one invoice is one bill; asking for a period would only
                # let you exclude the very thing you asked for
                # one invoice is one bill, and a ledger is a position as
                # of today -- neither is bounded by a period
                applies=lambda f: (
                    f.get("report") not in {"invoice", "ledger-supplier", "ledger-customer"}
                ),
            ),
        ),
        assemble=_assemble_export,
        prefill=_prefill_export,
    ),
    "supplier": _party_lookup_wizard("supplier", _suppliers),
    "customer": _party_lookup_wizard("customer", _customers),
    "capital": CommandWizard(
        command="capital",
        slots=(
            CommandSlot(
                name="partner",
                question="Which partner?",
                choices=_partners,
                validate=_nonempty("Partner"),
            ),
            CommandSlot(
                name="amount", question="How much?", validate=_amount, example="e.g. 50000"
            ),
            CommandSlot(
                name="method", question="Cash or bank?", choices=_method_buttons, validate=_method
            ),
            CommandSlot(
                name="kind",
                question="Money in, or money out?",
                choices=_capital_kind_buttons,
                validate=_capital_kind,
            ),
        ),
        assemble=lambda f: f"{f['partner']} {f['amount']} {f['method']} {f['kind']}",
        prefill=_prefill_capital,
    ),
    "withdraw": CommandWizard(
        command="withdraw",
        slots=(
            CommandSlot(
                name="partner",
                question="Which partner is withdrawing?",
                choices=_partners,
                validate=_nonempty("Partner"),
            ),
            CommandSlot(
                name="amount", question="How much?", validate=_amount, example="e.g. 50000"
            ),
            CommandSlot(
                name="method", question="Cash or bank?", choices=_method_buttons, validate=_method
            ),
        ),
        # a withdrawal always needs a second partner's approval, so the
        # wizard ends by *requesting* it rather than by posting anything
        assemble=lambda f: f"{f['partner']} {f['amount']} {f['method']}",
        prefill=_prefill_capital,
    ),
    "edit": CommandWizard(
        command="edit",
        slots=(
            CommandSlot(
                name="entity",
                question="What are you changing?",
                choices=_entity_menu,
                validate=_entity_kind,
            ),
            CommandSlot(
                name="reference",
                question="Which one? Give its code or name.",
                choices=_reference_menu,
                validate=_nonempty("Reference"),
                example="e.g. TRP",
            ),
            CommandSlot(
                name="field",
                question="Which field?",
                choices=_field_menu,
                validate=_nonempty("Field"),
                # a bill isn't edited field-by-field; it is reversed
                applies=lambda f: f.get("entity") not in REVERSIBLE_ENTITIES,
            ),
            CommandSlot(
                name="value",
                question="What should it be?",
                validate=_nonempty("Value"),
                applies=lambda f: f.get("entity") not in REVERSIBLE_ENTITIES,
            ),
        ),
        assemble=lambda f: f"{f['entity']} {f['reference']} {f['field']} {f['value']}",
        prefill=_prefill_entity,
        reroute=lambda f: (
            ("undo", f"{f['entity']} {f['reference']}")
            if f.get("entity") in REVERSIBLE_ENTITIES
            else None
        ),
    ),
    "delete": CommandWizard(
        command="delete",
        slots=(
            CommandSlot(
                name="entity",
                question="What are you deleting?",
                choices=_entity_menu,
                validate=_entity_kind,
            ),
            CommandSlot(
                name="reference",
                question="Which one? Give its code or name.",
                choices=_reference_menu,
                validate=_nonempty("Reference"),
                example="e.g. TRP",
            ),
            CommandSlot(
                name="confirm",
                question="Are you sure?",
                choices=_delete_confirm,
                validate=_affirmative,
            ),
        ),
        # `confirm` is a gate, not an argument -- it never reaches the
        # command
        assemble=lambda f: f"{f['entity']} {f['reference']}",
        prefill=_prefill_entity,
        reroute=lambda f: (
            ("undo", f"{f['entity']} {f['reference']}")
            if f.get("entity") in REVERSIBLE_ENTITIES
            else None
        ),
    ),
}


# --------------------------------------------------------------------
# running one
# --------------------------------------------------------------------


def remaining(wizard: CommandWizard, filled: dict[str, str]) -> list[str]:
    """Recomputed after every answer, not fixed at the start: an answer
    can decide whether a later question is needed at all."""
    return [slot.name for slot in wizard.slots if slot.name not in filled and slot.applies(filled)]


def missing(wizard: CommandWizard, args: str) -> tuple[dict[str, str], list[str]]:
    """What the typed args already answered, and what is still needed."""
    filled = {k: v for k, v in wizard.prefill(args).items() if v}
    return filled, remaining(wizard, filled)


async def ask(
    wizard: CommandWizard,
    slot_name: str,
    ctx: RequestContext,
    filled: dict[str, str] | None = None,
) -> CommandResult:
    """The question goes in exactly one place.

    An interactive message carries its own body, so also sending that
    text asks twice -- which is how "Which period?" arrived as two
    consecutive messages. When there are choices, the menu says it;
    otherwise the plain text does.
    """
    filled = filled or {}
    slot = next(s for s in wizard.slots if s.name == slot_name)
    body = f"{slot.question}\n{slot.example}".strip()
    interactive = await slot.choices(ctx, filled) if slot.choices is not None else None
    if interactive is None:
        return CommandResult(reply=body)
    # The builder's own body wins, and the static example is dropped:
    # the builder knows things the slot can't ("Delete product TRP?"
    # rather than "Are you sure?"), and an example written for the typed
    # fallback is wrong here anyway -- "Which sale? e.g. TRP" was asking
    # a product question about a bill. The rows demonstrate the format
    # better than any example could.
    return CommandResult(reply="", interactive=interactive)


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
    return await ask(wizard, queue[0], ctx, filled)


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

    if is_abandon(answer):
        await sessions.set(ctx.user.org_id, ctx.user.id, IDLE, {})
        return CommandResult(reply=f"Cancelled — no {wizard.command} was recorded.")

    if not queue:  # defensive; an empty queue should have run already
        await sessions.set(ctx.user.org_id, ctx.user.id, IDLE, {})
        return CommandResult(reply=f"That {wizard.command} is finished. Start again when ready.")

    if lowered == "back":
        done = list(filled)
        if not done:
            return await _reask(
                wizard, queue, ctx, prefix="That's the first question.", filled=filled
            )
        previous = done[-1]
        filled.pop(previous)
        queue.insert(0, previous)
        await _save(sessions, ctx, wizard, filled, queue)
        return await _reask(wizard, queue, ctx, prefix="Going back.", filled=filled)

    current = queue[0]
    if lowered in {"new", "other", "custom"}:
        # "Someone else" / "Custom range": the row can't answer itself
        return await _reask(wizard, queue, ctx, prefix="Go ahead and type it.", filled=filled)

    slot = next(s for s in wizard.slots if s.name == current)
    try:
        filled[current] = slot.validate(answer)
    except DomainError as exc:
        return await _reask(wizard, queue, ctx, prefix=exc.message, filled=filled)
    if slot.after is not None:
        slot.after(filled)

    queue = remaining(wizard, filled)
    if queue:
        await _save(sessions, ctx, wizard, filled, queue)
        return await ask(wizard, queue[0], ctx, filled)

    # Complete. Hand the assembled one-shot to the real handler, so the
    # wizard and the typed command cannot diverge (§10.5).
    from backend.api.whatsapp_commands import COMMAND_REGISTRY

    await sessions.set(ctx.user.org_id, ctx.user.id, IDLE, {})
    rerouted = wizard.reroute(filled)
    if rerouted is not None:
        command, args = rerouted
        return await COMMAND_REGISTRY[command].handler(args, ctx)
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
    wizard: CommandWizard,
    queue: list[str],
    ctx: RequestContext,
    *,
    prefix: str,
    filled: dict[str, str] | None = None,
) -> CommandResult:
    question = await ask(wizard, queue[0], ctx, filled)
    return dataclasses.replace(question, reply=f"{prefix}\n{question.reply}".strip())
