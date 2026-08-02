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
import datetime
import decimal
import re
from collections.abc import Awaitable, Callable
from typing import Any

from backend.api.amounts import looks_like_amount, parse_amount
from backend.api.command_types import CommandResult, RequestContext
from backend.api.formatting import fmt_date, fmt_money, fmt_qty
from backend.api.interactive import (
    Buttons,
    Choice,
    Interactive,
    ListMenu,
    Section,
    is_abandon,
)
from backend.core.dates import parse_date, split_date
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
    #: Overrides `question`/`example` once earlier answers are known.
    #: "Give its code or name, e.g. TRP" is right for a product and
    #: wrong for everything else the same slot serves.
    question_of: Callable[[dict[str, str]], tuple[str, str]] | None = None
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
REVERSIBLE_ENTITIES = {"purchase", "sale", "expense", "payment"}


def _entity_kind(value: str) -> str:
    token = value.strip().lower().rstrip("s")
    if token not in {"product", "supplier", "customer", "brand", *REVERSIBLE_ENTITIES}:
        raise ValidationError(
            f"I can't change a '{value.strip()}'. Pick product, supplier, customer, "
            "brand, purchase, sale, expense or payment."
        )
    return token


def _code(value: str) -> str:
    token = value.strip().upper()
    if not token or " " in token:
        raise ValidationError("A product code is a single word, e.g. TRP.")
    return token


#: One answer, several values: "VVP, VVP-1, 35A". A sale of six items
#: was six rounds of three questions; this makes it three answers.
_LIST_SEPARATORS = re.compile(r"[,\n;]+|\s+")


def _split_list(value: str) -> list[str]:
    return [part for part in _LIST_SEPARATORS.split(value.strip()) if part]


def _codes(value: str) -> str:
    parts = _split_list(value)
    if not parts:
        raise ValidationError("Give at least one product code, e.g. TRP.")
    return ", ".join(_code(part) for part in parts)


def _quantities(value: str) -> str:
    parts = _split_list(value)
    if not parts:
        raise ValidationError("How many? e.g. 100")
    return ", ".join(_quantity(part) for part in parts)


def _rates(value: str) -> str:
    parts = _split_list(value)
    if not parts:
        raise ValidationError("At what rate? e.g. 150")
    return ", ".join(_amount(part) for part in parts)


def _counts(value: str) -> str:
    """Bales counted off a truck: several allowed, and **zero is a real
    answer** -- a bale that never turned up is the whole reason `receive`
    exists, so this cannot go through `_quantity`, which refuses it."""
    parts = _split_list(value)
    if not parts:
        raise ValidationError("How many bales actually arrived? e.g. 9")
    checked: list[str] = []
    for part in parts:
        try:
            count = decimal.Decimal(part.replace(",", ""))
        except decimal.InvalidOperation:
            raise ValidationError(f"'{part}' isn't a number of bales I can read.") from None
        if count < 0:
            raise ValidationError("A count can't be negative.")
        checked.append(str(count))
    return ", ".join(checked)


def _line_scope(value: str) -> str:
    """Which lines of a bill a rate change touches: every one, or a
    named few."""
    token = value.strip().lower()
    if token in {"all", "every", "every line", "all lines", "everything", "whole bill"}:
        return "all"
    return _codes(value)


def _when(value: str) -> str:
    """Checked here, resolved later. "today" stays the word it was: only
    the service knows the org's business date, and turning it into a
    calendar date here would file a payment made just before midnight
    under the wrong day."""
    token = value.strip().lower()
    if token in {"today", "yesterday"}:
        return token
    return parse_date(value, today=datetime.date.today()).strftime("%d-%m-%Y")


def _optional_note(value: str) -> str:
    """ "-" is the skip, and is what the button sends. Stored rather than
    dropped so `back` can tell "skipped" from "not asked yet"."""
    text = value.strip()
    return text if text and text not in {"-", "no", "none", "skip"} else "-"


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


async def _note_buttons(ctx: RequestContext, filled: dict[str, str]) -> Interactive | None:
    """Optional, and visibly so. Most payments need no explanation; the
    ones that do are the ones nobody can reconstruct later -- "through
    hanif pune", "in ac mahadev" -- and a wizard with no way to type one
    loses them all."""
    return Buttons(
        body="Add a note? (why, or who it went through)",
        choices=(Choice(id="slot -", title="No note"),),
    )


async def _when_buttons(ctx: RequestContext, filled: dict[str, str]) -> Interactive | None:
    """Most money is entered the day it moved, so that is one tap --
    but a ledger copied out of a paper book is entered weeks later, and
    typing the day is always allowed."""
    return Buttons(
        body="When did this happen?",
        choices=(
            Choice(id="slot today", title="Today"),
            Choice(id="slot yesterday", title="Yesterday"),
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
                    Choice(
                        id="slot payment",
                        title="Payment",
                        description="Paid or received; bills reopen",
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


def _reference_wording(filled: dict[str, str]) -> tuple[str, str]:
    """What to ask when there is no list to offer.

    A bill or an expense has neither a code nor a name, so the product
    wording -- "give its code or name, e.g. TRP" -- was asking the wrong
    question wherever the picker came back empty.
    """
    entity = filled.get("entity", "")
    if entity in REVERSIBLE_ENTITIES:
        return (f"Which {entity}? Give its reference.", "e.g. ec196ee8")
    return ("Which one? Give its code or name.", "e.g. TRP")


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

    elif entity == "payment":
        from backend.repositories.audit_repository import AuditRepository

        async with ctx.session_factory() as session:
            recent_payments = await AuditRepository(session).recent_payments(
                ctx.user.org_id, limit=PARTY_ROWS
            )
        rows = tuple(
            Choice(
                id=f"slot {short_id}",
                title=f"{'Paid' if paid else 'Recvd'} {amount}"[:24],
                description=f"{party} · {fmt_date(day)} · ref {short_id}"[:72],
            )
            for short_id, paid, amount, party, day in recent_payments
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


async def _lines_menu(
    ctx: RequestContext, filled: dict[str, str], *, whole_bill: bool
) -> Interactive | None:
    """The lines of the bill just chosen.

    A correction is about a line that is already on a bill, so the codes
    are knowable and should not have to be remembered -- especially on a
    26-line sheet, where recalling which code was short is the actual
    work. Beyond the menu's ten rows the escape hatch takes a typed
    code, and several typed at once still work.
    """
    from backend.repositories.purchase_repository import InvoiceLine, PurchaseRepository

    invoice = filled.get("invoice", "").strip()
    if not invoice:
        return None
    # one row for "every line", one for the escape hatch (docs/19 §2)
    room = PARTY_ROWS - 1 if whole_bill else PARTY_ROWS
    async with ctx.session_factory() as session:
        lines = await PurchaseRepository(session).invoice_lines(
            ctx.user.org_id, invoice, limit=room
        )
    if not lines:
        return None

    def described(line: InvoiceLine) -> str:
        bales = f"{fmt_qty(line.pieces)} bales × " if line.pieces is not None else ""
        return f"{bales}{fmt_qty(line.qty)} @ {fmt_money(line.rate)} · {line.description}"[:72]

    rows = tuple(
        Choice(id=f"slot {line.code}", title=line.code[:24], description=described(line))
        for line in lines
    )
    sections: list[Section] = []
    if whole_bill:
        sections.append(
            Section(
                title="Everything",
                rows=(
                    Choice(
                        id="slot all",
                        title="Every line",
                        description="The whole bill moves to the new rate",
                    ),
                ),
            )
        )
    sections.append(Section(title=f"On {invoice}", rows=rows))
    sections.append(
        Section(
            title="Or",
            rows=(
                Choice(
                    id="slot new",
                    title="Another code",
                    description="You'll type it — several at once is fine",
                ),
            ),
        )
    )
    return ListMenu(
        body=f"Which line{'s' if whole_bill else ''} of {invoice}?",
        menu_label="Pick line",
        sections=tuple(sections),
    )


async def _rate_scope_menu(ctx: RequestContext, filled: dict[str, str]) -> Interactive | None:
    return await _lines_menu(ctx, filled, whole_bill=True)


async def _receive_line_menu(ctx: RequestContext, filled: dict[str, str]) -> Interactive | None:
    return await _lines_menu(ctx, filled, whole_bill=False)


async def _period_menu(ctx: RequestContext, filled: dict[str, str]) -> Interactive | None:
    from backend.api.period import period_menu

    menu = period_menu("slot")
    return dataclasses.replace(menu, body="Which period?")


# --------------------------------------------------------------------
# prefill -- what the user already typed
# --------------------------------------------------------------------


def _prefill_when(args: str) -> tuple[str, dict[str, str]]:
    """Take `on 28-07-2026` off the line before anything else reads it,
    so it can't be mistaken for an invoice reference or swallowed by a
    party name."""
    args, on = split_date(args)
    filled: dict[str, str] = {}
    if on is not None:
        with contextlib.suppress(DomainError):
            filled["when"] = _when(on)
    return args, filled


def _dated(
    filled: dict[str, str], required: tuple[str, ...], *, optional: tuple[str, ...] = ()
) -> dict[str, str]:
    """A command typed in full still runs in one round trip (§10.5), so
    only a command that was going to ask something anyway gets asked for
    the date or a note; one typed complete means today and no note."""
    if all(name in filled for name in required):
        filled.setdefault("when", "today")
        for name in optional:
            filled.setdefault(name, "-")
    return filled


def _prefill_settlement(args: str) -> dict[str, str]:
    """`paid wagdia` -> the party is known, ask the rest.

    Deliberately conservative: anything it cannot place with certainty
    is left unfilled, because a wrong guess here is a wrong payment.
    """
    args, filled = _prefill_when(args)
    tokens = args.split()
    if tokens and tokens[0].lower() in {"supplier:", "supplier", "customer:", "customer"}:
        tokens = tokens[1:]

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
    return _dated(filled, ("party", "amount", "method"), optional=("note",))


def _prefill_money(args: str) -> dict[str, str]:
    args, filled = _prefill_when(args)
    tokens = args.split()
    if tokens:
        filled["category"] = tokens[0]
    if len(tokens) > 1 and looks_like_amount(tokens[1]):
        # a malformed amount is left unfilled so the slot asks for it,
        # rather than the command failing on something the user can fix
        with contextlib.suppress(DomainError):
            filled["amount"] = str(parse_amount(tokens[1]))
    if len(tokens) > 2 and tokens[2].lower() in {"cash", "bank"}:
        filled["method"] = tokens[2].lower()
    return _dated(filled, ("category", "amount", "method"))


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


def _still_adding(filled: dict[str, str]) -> bool:
    return filled.get("more") != "done"


def _items_of(filled: dict[str, str]) -> list[str]:
    banked = filled.get("items", "")
    return [line for line in banked.split("\n") if line.strip()]


def _zip_items(filled: dict[str, str]) -> list[str]:
    """One line per code, from answers that may each hold several.

    A single quantity or rate is spread across every code -- "VVP,
    VVP-1, 35A" at "100" and "150" is the common case of three items at
    the same price. Anything else has to line up, because guessing which
    code the missing number belonged to would price the wrong item.
    """
    codes = _split_list(filled.get("code", ""))
    quantities = _split_list(filled.get("qty", ""))
    rates = _split_list(filled.get("rate", ""))
    if not codes:
        return []
    for label, values in (("quantities", quantities), ("rates", rates)):
        if len(values) not in {0, 1, len(codes)}:
            raise ValidationError(
                f"{len(codes)} codes but {len(values)} {label} — give one per code "
                f"({', '.join(codes)}), or a single one for all of them."
            )
    if not quantities or not rates:
        return []

    def spread(values: list[str]) -> list[str]:
        return values * len(codes) if len(values) == 1 else values

    return [
        f"{code} {qty} {rate}"
        for code, qty, rate in zip(codes, spread(quantities), spread(rates), strict=True)
    ]


def _check_counts(filled: dict[str, str]) -> None:
    """Runs as the quantity's `after`, so three codes and two quantities
    is caught at the quantity rather than one question later."""
    _zip_items(filled)


def _bank_item(filled: dict[str, str]) -> None:
    """Move the item(s) just answered into the collected list.

    When there is another to come, the per-item slots are cleared so the
    queue asks for them again -- that is the loop. `filled` is the only
    state a wizard has, so the loop lives in it rather than in a counter
    somewhere else that could disagree.
    """
    banked = _zip_items(filled)
    if banked:
        filled["items"] = "\n".join([*_items_of(filled), *banked])
    for key in ("code", "qty", "rate"):
        filled.pop(key, None)
    if filled.get("more") == "add":
        filled.pop("more", None)


async def _more_lines_buttons(ctx: RequestContext, filled: dict[str, str]) -> Interactive | None:
    counted = len(_items_of(filled)) + len(_split_list(filled.get("code", "")))
    return Buttons(
        body=f"{counted} line(s) corrected on {filled.get('invoice', 'this bill')}. Anything else?",
        choices=(
            Choice(id="slot add", title="Another line"),
            Choice(id="slot done", title="That's all"),
        ),
    )


def _zip_receipts(filled: dict[str, str]) -> list[str]:
    """One "CODE bales" line per code answered.

    Unlike a sale, a single count is **not** spread across several codes.
    "35A, 22D" answered "9" would silently claim nine bales of each --
    and unlike a price, that writes stock movements nobody asked for.
    """
    codes = _split_list(filled.get("code", ""))
    counts = _split_list(filled.get("pieces", ""))
    if not codes or not counts:
        return []
    if len(counts) != len(codes):
        raise ValidationError(
            f"{len(codes)} codes but {len(counts)} counts — give one count per code "
            f"({', '.join(codes)}), in the same order."
        )
    return [f"{code} {count}" for code, count in zip(codes, counts, strict=True)]


def _check_receipt_counts(filled: dict[str, str]) -> None:
    _zip_receipts(filled)


def _bank_receipt(filled: dict[str, str]) -> None:
    """The same loop the sale wizard runs, over (code, bales) pairs."""
    banked = _zip_receipts(filled)
    if banked:
        filled["items"] = "\n".join([*_items_of(filled), *banked])
    for key in ("code", "pieces"):
        filled.pop(key, None)
    if filled.get("more") == "add":
        filled.pop("more", None)


def _assemble_receive(filled: dict[str, str]) -> str:
    return " ".join([filled["invoice"], *_items_of(filled)])


def _prefill_receive(args: str) -> dict[str, str]:
    """`receive 001 35A 9` is complete and runs in one shot; `receive
    001` knows only the bill and asks the rest."""
    tokens = args.split()
    filled: dict[str, str] = {}
    if not tokens:
        return filled
    filled["invoice"] = tokens[0]
    rest = tokens[1:]
    pairs = [f"{rest[i].upper()} {rest[i + 1]}" for i in range(0, len(rest) - 1, 2)]
    if pairs:
        filled["items"] = "\n".join(pairs)
    if len(rest) % 2 == 1:
        # a trailing code with no count: keep it, ask what arrived
        filled["code"] = rest[-1].upper()
    elif pairs:
        filled["more"] = "done"
    return filled


def _rate_question(filled: dict[str, str]) -> tuple[str, str]:
    scope = filled.get("codes", "all")
    where = "every line" if scope == "all" else scope
    return (f"What's the correct rate per unit for {where}?", "e.g. 145")


def _assemble_rate(filled: dict[str, str]) -> str:
    codes = filled.get("codes", "all")
    tail = "" if codes == "all" else " " + " ".join(_split_list(codes))
    return f"{filled['invoice']} {filled['rate']}{tail}"


def _prefill_rate(args: str) -> dict[str, str]:
    """`rate 001 145` is complete -- and means every line, which is the
    typed command's own meaning, so the wizard must not turn it into a
    question."""
    tokens = args.split()
    filled: dict[str, str] = {}
    if not tokens:
        return filled
    filled["invoice"] = tokens[0]
    if len(tokens) >= 2 and looks_like_amount(tokens[1]):
        with contextlib.suppress(DomainError):
            filled["rate"] = _amount(tokens[1])
    if "rate" in filled:
        rest = tokens[2:]
        filled["codes"] = ", ".join(token.upper() for token in rest) if rest else "all"
    return filled


def _assemble_sale(filled: dict[str, str]) -> str:
    """The sale grammar: a header line, then one line per item."""
    items = _items_of(filled)
    when = filled.get("when", "today")
    dated = "" if when == "today" else f" on {when}"
    return "\n".join([f"Customer: {filled['customer']}{dated}", *items])


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
            CommandSlot(
                name="when",
                question="When did this happen?",
                choices=_when_buttons,
                validate=_when,
                example="Today, or a date like 28-07-2026",
            ),
            CommandSlot(
                name="note",
                question="Add a note? (why, or who it went through)",
                choices=_note_buttons,
                validate=_optional_note,
                example="e.g. through Hanif Pune — or tap 'No note'",
            ),
        ),
        assemble=lambda f: (
            f"{f['party']} {f['amount']} {f['method']}"
            + (f" on {f['when']}" if f.get("when", "today") != "today" else "")
            + (f" note: {f['note']}" if f.get("note", "-") != "-" else "")
        ),
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
            CommandSlot(
                name="when",
                question="When did this happen?",
                choices=_when_buttons,
                validate=_when,
                example="Today, or a date like 18-07-2026",
            ),
        ),
        assemble=lambda f: (
            f"{f['category']} {f['amount']} {f['method']}"
            + (f" on {f['when']}" if f.get("when", "today") != "today" else "")
        ),
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
            # `applies` is what ends the loop. Without it "That's all"
            # cleared the item slots and `remaining` immediately asked
            # for a code again -- the wizard could not be finished, and
            # the answers typed at the re-asked questions ("confirm",
            # "1", "1") were banked as an item nobody meant to sell.
            CommandSlot(
                name="code",
                question="Which product code?",
                validate=_codes,
                example="e.g. TRP — or several: VVP, VVP-1, 35A",
                applies=_still_adding,
            ),
            CommandSlot(
                name="qty",
                question="How many?",
                validate=_quantities,
                example="e.g. 100 — one per code, or one for all",
                after=_check_counts,
                applies=_still_adding,
            ),
            CommandSlot(
                name="rate",
                question="At what rate per unit?",
                validate=_rates,
                example="e.g. 150 — one per code, or one for all",
                applies=_still_adding,
            ),
            CommandSlot(
                name="more",
                question="Anything else on this sale?",
                choices=_more_items_buttons,
                validate=_more_items,
                after=_bank_item,
            ),
            # Asked once, after the items rather than before them: the
            # date is the same for every line, and asking it first put a
            # question about paperwork in front of the actual sale.
            CommandSlot(
                name="when",
                question="When did this sale happen?",
                choices=_when_buttons,
                validate=_when,
                example="Today, or a date like 28-07-2026",
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
    # Corrections to a confirmed bill. Both used to answer a bare command
    # with a usage line -- the one place where remembering an invoice
    # number *and* a code was the whole difficulty, and the one place the
    # system already knows both.
    "rate": CommandWizard(
        command="rate",
        slots=(
            CommandSlot(
                name="invoice",
                question="Which bill has the wrong rate?",
                choices=_invoice_menu,
                validate=_nonempty("Invoice"),
                example="e.g. 001",
            ),
            CommandSlot(
                name="codes",
                question="Which lines? Tap 'Every line', or name the codes.",
                choices=_rate_scope_menu,
                validate=_line_scope,
                example="e.g. 35A 22D — or 'all'",
            ),
            CommandSlot(
                name="rate",
                question="What's the correct rate per unit?",
                question_of=_rate_question,
                validate=_amount,
                example="e.g. 145",
            ),
        ),
        assemble=_assemble_rate,
        prefill=_prefill_rate,
    ),
    "receive": CommandWizard(
        command="receive",
        slots=(
            CommandSlot(
                name="invoice",
                question="Which bill are you correcting?",
                choices=_invoice_menu,
                validate=_nonempty("Invoice"),
                example="e.g. 001",
            ),
            CommandSlot(
                name="code",
                question="Which item came in short?",
                choices=_receive_line_menu,
                validate=_codes,
                example="e.g. 35A — or several: 35A, 22D",
                applies=_still_adding,
            ),
            CommandSlot(
                name="pieces",
                question="How many bales actually arrived?",
                validate=_counts,
                example="e.g. 9 — one count per code, in the same order",
                after=_check_receipt_counts,
                applies=_still_adding,
            ),
            CommandSlot(
                name="more",
                question="Another line on this bill?",
                choices=_more_lines_buttons,
                validate=_more_items,
                after=_bank_receipt,
            ),
        ),
        assemble=_assemble_receive,
        prefill=_prefill_receive,
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
                question_of=_reference_wording,
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
                question_of=_reference_wording,
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
    question, example = (
        slot.question_of(filled) if slot.question_of is not None else (slot.question, slot.example)
    )
    body = f"{question}\n{example}".strip()
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
    if lowered in {"new", "other", "custom", "someone else", "another one"}:
        # "Someone else" / "Custom range" / "Another one" answer *how* to
        # answer, never the question itself. The picker is deliberately
        # not re-sent: offering again the list someone just declined
        # leaves it ambiguous which message is being answered.
        slot = next(s for s in wizard.slots if s.name == current)
        question, example = (
            slot.question_of(filled)
            if slot.question_of is not None
            else (slot.question, slot.example)
        )
        return CommandResult(reply=f"{question}\n{example}".strip())

    slot = next(s for s in wizard.slots if s.name == current)
    try:
        filled[current] = slot.validate(answer)
        # Inside the same guard as `validate`: an answer that is fine on
        # its own can still be wrong beside the others -- three codes and
        # two quantities -- and that check can only run once both are in.
        if slot.after is not None:
            slot.after(filled)
    except DomainError as exc:
        # Undo it, or `remaining` would treat the rejected answer as
        # given and never ask the question again.
        filled.pop(current, None)
        return await _reask(wizard, queue, ctx, prefix=exc.message, filled=filled)

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
