"""`purchase` command and its confirmation session -- docs/04_Purchases.md
§3, docs/08_WhatsApp.md #purchase and §5 (state machine).

The draft lives in the WhatsApp session until CONFIRM. While the session
is in awaiting_purchase_confirmation, non-command replies route here:
CONFIRM vocabulary, corrections (`line N field value`), `create
supplier`, `create product CODE desc`, duplicate override, total-
mismatch resolution, `discard`.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import re

from backend.api.command_types import CommandResult, RequestContext
from backend.api.commands.charges import apply_charge, describe, parse_charge
from backend.api.commands.documents import attach_document
from backend.api.formatting import fmt_date, fmt_money, fmt_qty
from backend.api.interactive import Buttons, Choice, is_abandon
from backend.core.exceptions import (
    DomainError,
    ExactDuplicateInvoiceError,
    FuzzyDuplicateInvoiceError,
    TotalMismatchWarning,
    ValidationError,
)
from backend.core.security import role_at_least
from backend.models.enums import UserRole
from backend.services.ocr_service import OcrService
from backend.services.purchase_service import (
    QTY_SANITY_CEILING,
    ConfirmedPurchase,
    Draft,
    DraftLine,
    PurchaseService,
)
from backend.services.session_service import (
    AWAITING_PURCHASE_CONFIRMATION,
    SessionService,
    SessionState,
)

_HEADER = re.compile(
    r"Supplier:\s*(?P<supplier>.+?)\s+Invoice:\s*(?P<invoice>\S+)\s+"
    r"Date:\s*(?P<date>\d{2}-\d{2}-\d{4})(?:\s+Brand:\s*(?P<brand>.+))?\s*$",
    re.IGNORECASE,
)
#: Same permissive code class as the sale grammar -- see the note there.
_ITEM = re.compile(r"^(?P<code>[A-Za-z0-9][\w.\-/&]*)\s+(?P<qty>[\d.]+)\s+(?P<rate>[\d.]+)$")
_LABELED = re.compile(r"^(?P<label>freight|other|total):\s*(?P<amount>[\d.]+)$", re.IGNORECASE)
_CORRECTION = re.compile(
    r"^line\s+(?P<line>\d+)\s+(?P<field>code|qty|rate|brand)\s+(?P<value>.+)$", re.IGNORECASE
)
#: "This message was *meant* as corrections." Matched per line, so a
#: message whose lines are all malformed still gets told so, instead of
#: falling through to the generic re-prompt and looking like assent.
_CORRECTION_HINT = re.compile(r"^line\s+\d+", re.IGNORECASE)
#: Fixing the supplier mid-draft. A bill often prints the *buyer's* name
#: most prominently -- Iqbal Bhai's book says "FIROZ-PNP", which is the
#: customer, us -- so whatever is read off the sheet is a guess that has
#: to be correctable without abandoning the draft.
_SET_SUPPLIER = re.compile(r"^supplier\s*:?\s+(?P<name>.+)$", re.IGNORECASE)
_CREATE_PRODUCT = re.compile(
    r"^create\s+product\s+(?P<code>[A-Za-z0-9][\w.\-/&]*)(?:\s+(?P<description>.+))?$",
    re.IGNORECASE,
)
_CREATE_ALL = re.compile(r"^create\s+all\s+products?$", re.IGNORECASE)
#: button ids that mean "tell me how, I'll do it myself"
_GUIDANCE = {"one by one", "fix"}

CONFIRM_VOCAB = {"confirm", "yes", "ok", "save"}

USAGE = (
    "Usage:\n"
    "purchase Supplier: <name> Invoice: <no> Date: <DD-MM-YYYY> [Brand: <name>]\n"
    "<CODE> <qty> <rate>   (one line per item)\n"
    "Freight: <amount>\n"
    "Other: <amount>\n"
    "Total: <amount on the invoice, optional>"
)


def parse_purchase_command(args: str) -> Draft:
    lines = [line.strip() for line in args.strip().splitlines() if line.strip()]
    if not lines:
        raise ValidationError(USAGE)
    header = _HEADER.match(lines[0])
    if header is None:
        raise ValidationError(f"Couldn't read the first line. {USAGE}")
    try:
        invoice_date = datetime.datetime.strptime(header["date"], "%d-%m-%Y").date()
    except ValueError:
        raise ValidationError(f"'{header['date']}' is not a valid DD-MM-YYYY date.") from None

    items: list[DraftLine] = []
    freight = other = decimal.Decimal("0")
    declared_total: decimal.Decimal | None = None
    for raw in lines[1:]:
        labeled = _LABELED.match(raw)
        if labeled:
            amount = decimal.Decimal(labeled["amount"])
            label = labeled["label"].lower()
            if label == "freight":
                freight = amount
            elif label == "other":
                other = amount
            else:
                declared_total = amount
            continue
        item = _ITEM.match(raw)
        if item is None:
            raise ValidationError(
                f"Couldn't read item line '{raw}' — expected: <CODE> <qty> <rate>"
            )
        items.append(
            DraftLine(
                code=item["code"].upper(),
                qty=decimal.Decimal(item["qty"]),
                rate=decimal.Decimal(item["rate"]),
                product_id=None,
                resolved_code=None,
                unit_code=None,
            )
        )
    if not items:
        raise ValidationError("Send at least one item line.")

    return Draft(
        supplier_id=None,
        supplier_name=header["supplier"].strip(),
        invoice_no=header["invoice"].strip(),
        invoice_date=invoice_date,
        brand_id=None,
        brand_name=header["brand"].strip() if header["brand"] else None,
        lines=items,
        freight=freight,
        other_charges=other,
        declared_total=declared_total,
    )


#: Above this many unknown codes, per-line "create product X" hints stop
#: being help and become a wall of text -- a first purchase has *every*
#: code unknown. Past it, the single `create all products` line says the
#: same thing once.
_PER_LINE_HINT_LIMIT = 3


def next_step(draft: Draft) -> str:
    """The one thing this draft is waiting for.

    Read by both the reply text and the buttons, so the two can never
    ask for different things. They used to be computed separately and a
    real sheet produced "reply 'create supplier'", "reply *create all
    products*" and "then reply CONFIRM to save" in a single message --
    three instructions, of which only one would work, and no way to tell
    which.

    Order is by what blocks what: the header details make the rest
    readable; a brand collision is a sharper question than "shall I
    create these?" and asking it second would create duplicates in one
    tap; codes must exist before the bill can be saved; and the supplier
    is asked last because it is the one step that needs no thought.
    """
    if not draft.supplier_name or not draft.invoice_no:
        return "details"
    if draft.brand_collisions:
        return "brand"
    # Before "codes": a code carried by two brands is not missing from
    # the catalogue, and offering to create it would add a third product
    # sharing the code -- which is the mess, not the fix.
    if draft.needs_brand is not None:
        return "line_brand"
    if draft.unresolved_codes:
        return "codes"
    if draft.supplier_id is None:
        return "supplier"
    return "confirm"


def render_preview(draft: Draft) -> str:
    lines = [
        f"✅ Purchase draft ready — {draft.supplier_name}, {draft.invoice_no}, "
        f"{fmt_date(draft.invoice_date)}"
    ]
    warnings: list[str] = []
    verbose_unknown_hints = len(draft.unresolved_codes) <= _PER_LINE_HINT_LIMIT
    for index, line in enumerate(draft.lines, start=1):
        if line.product_id is None:
            lines.append(
                f"❓ {line.code}"
                + (f" {line.description}" if line.description else "")
                + f"  {fmt_qty(line.qty)} × {fmt_money(line.rate)} — unknown product"
            )
            if verbose_unknown_hints:
                if line.description:
                    warnings.append(
                        f"Reply 'create product {line.code}' to add it as "
                        f"'{line.description}', or 'line {index} code <CODE>' to correct."
                    )
                else:
                    warnings.append(
                        f"Reply 'create product {line.code} <description>' to add it, "
                        f"or 'line {index} code <CODE>' to correct."
                    )
            continue
        unit = line.unit_code or "KG"
        matched = (
            f" (matched '{line.code}' → {line.resolved_code})"
            if line.resolved_code and line.resolved_code != line.code
            else ""
        )
        label = line.description or ""
        breakdown = ""
        if line.pieces is not None and line.weight_per_unit is not None:
            breakdown = f" [{fmt_qty(line.pieces)}×{fmt_qty(line.weight_per_unit)}{unit}]"
        lines.append(
            f"{line.resolved_code or line.code}"
            + (f" {label}" if label else "")
            + f"  {fmt_qty(line.qty)} {unit}{breakdown} × "
            f"{fmt_money(line.rate)} = {fmt_money(line.line_total)}{matched}"
        )
        if line.rate == 0:
            warnings.append(f"{line.code} has a rate of ₹0 — free goods?")
        if line.qty > QTY_SANITY_CEILING:
            warnings.append(
                f"{fmt_qty(line.qty)} of {line.code} — that's unusually high, please check."
            )
    lines.append(f"Subtotal: {fmt_money(draft.subtotal)}")
    if draft.freight:
        lines.append(f"Freight: {fmt_money(draft.freight)} (allocated by weight)")
    if draft.other_charges:
        parts = describe(draft)
        itemised = f"  ({parts})" if parts else ""
        lines.append(f"Other charges: {fmt_money(draft.other_charges)}{itemised}")
    lines.append(f"Grand total: {fmt_money(draft.grand_total)}")
    if draft.declared_total is not None:
        lines.append(f"Invoice shows: {fmt_money(draft.declared_total)}")
    step = next_step(draft)
    if draft.supplier_name and draft.supplier_id is None and step != "supplier":
        # A statement, not an instruction: something else is being asked
        # first, and two instructions in one message is one too many.
        warnings.append(
            f"Supplier '{draft.supplier_name}' isn't in your list yet — I'll ask about that next."
        )
    if any(line.rate == 0 for line in draft.lines):
        warnings.append("Some lines have no rate yet.")
    lines.extend(f"⚠️ {warning}" for warning in warnings)
    if step == "details":
        from backend.api.commands.ocr_commands import DETAILS_PROMPT

        lines.append(DETAILS_PROMPT)
    elif step == "codes":
        # Naming the command matters: `create all products` existed from
        # the start but nothing ever mentioned it, so a first purchase --
        # where *every* code is new -- looked like a dead end.
        lines.append(unresolved_help(draft.unresolved_codes))
    elif step == "line_brand":
        pending = draft.needs_brand
        assert pending is not None
        listed = ", ".join(f"*{name}*" for name in pending.brand_choices)
        lines.append(
            f"*{pending.code}* is carried by {listed}. Which one is this line?\n"
            f"Reply *line {draft.lines.index(pending) + 1} brand <name>*."
        )
    elif step == "supplier":
        lines.append(f"One thing left: *{draft.supplier_name}* isn't in your supplier list yet.")
    elif step == "confirm":
        # The same lesson as `create all products` three branches up, and
        # it was learned twice. `supplier` and the charge words shipped
        # and were immediately typed at an *idle* session instead --
        # because the only thing this prompt had ever named was
        # `line 1 qty 90`, so that looked like the only thing on offer.
        # A command nobody names does not exist.
        lines.append(
            "Reply CONFIRM to save, or fix anything first:\n"
            "• *line 1 qty 800* — also *rate*, *code*\n"
            "• *supplier <name>* — the biggest name on a bill is often "
            "the buyer's, not the seller's\n"
            "• *GST 2240*, *packing 2100*, *freight 500* — charges at the foot of the bill"
        )
    return "\n".join(lines)


def unresolved_help(codes: list[str]) -> str:
    """What to actually do about codes that aren't in the catalogue yet.

    On a first purchase every code is new, so this is the normal path,
    not an error state -- the copy says so rather than reading like a
    failure.
    """
    count = len(codes)
    noun = "item isn't" if count == 1 else "items aren't"
    return (
        f"{count} {noun} in your catalogue yet.\n"
        f"• Reply *create all products* to add them all, using the descriptions "
        f"from the sheet\n"
        f"• Or add them one at a time: *create product {codes[0]} <description>*\n"
        # Deliberately not "then reply CONFIRM": the bill comes back the
        # moment they're created, and it will say so itself. Two
        # instructions in one message leaves the reader picking.
        f"I'll show you the bill again once they're in."
    )


def preview_result(draft: Draft) -> CommandResult:
    """The preview plus whatever decision it is actually asking for.

    Both the buttons and the reply text branch on `next_step`, so the
    offer can never contradict the words above it.
    """
    reply = render_preview(draft)
    step = next_step(draft)
    choices: tuple[Choice, ...]
    # Asked before "these aren't in your catalogue": a collided code is
    # always also an unresolved one, and "are these really different
    # products?" is the sharper question. Getting the bulk-create prompt
    # first would have someone create six duplicates in one tap.
    if step == "brand":
        listed = "\n".join(f"• {entry}" for entry in draft.brand_collisions)
        under = draft.brand_name or "no brand"
        reply = (
            f"{reply}\n\n⚠️ {len(draft.brand_collisions)} code(s) already exist under "
            f"another brand:\n{listed}\n"
            f"Under *{under}* these become separate products. That's right if they really "
            "are different items — say so, or fix the brand."
        )
        body = f"Same codes, different brand ({under}). Separate products?"
        choices = (
            Choice(id="create all products", title="Yes, separate"),
            Choice(id="fix brand", title="Fix the brand"),
            Choice(id="discard", title="Discard"),
        )
    elif step == "codes":
        count = len(draft.unresolved_codes)
        bulk = f"Create all {count}" if count < 100000 else "Create all"
        body = f"{count} item(s) aren't in your catalogue yet."
        choices = (
            Choice(id="create all products", title=bulk),
            Choice(id="one by one", title="One by one"),
            Choice(id="discard", title="Discard"),
        )
    elif step == "supplier":
        body = f"Add '{draft.supplier_name}' as a supplier?"
        choices = (
            Choice(id="create supplier", title="Add supplier"),
            Choice(id="discard", title="Discard"),
        )
    elif step == "details":
        # still waiting on `details`; nothing to decide yet
        return CommandResult(reply=reply)
    else:
        body = f"Save this purchase? {fmt_money(draft.grand_total)} to {draft.supplier_name}."
        choices = (
            # Three is the button cap (docs/19 §2). "Fix a line" loses
            # its button rather than the sheet, because the reply text
            # already spells out how to fix one and nothing spells out
            # that a sheet is available.
            Choice(id="confirm", title="Confirm"),
            Choice(id="sheet", title="See as sheet"),
            Choice(id="discard", title="Discard"),
        )
        if draft.shared_codes:
            # These resolved under the answered brand, which is right --
            # but VVP is a different garment under TOP than under MKD, so
            # say which ones were a choice, and keep the fix one tap away.
            listed = "\n".join(f"• {entry}" for entry in draft.shared_codes)
            under = draft.brand_name or "no brand"
            reply = (
                f"{reply}\n\nℹ️ {len(draft.shared_codes)} code(s) exist under more than one "
                f"brand:\n{listed}\nI've used *{under}*'s. Fix the brand if that's wrong."
            )
            choices = (
                Choice(id="confirm", title=f"Confirm ({under})"[:20]),
                Choice(id="fix brand", title="Fix the brand"),
                Choice(id="discard", title="Discard"),
            )
    return CommandResult(reply=reply, interactive=Buttons(body=body, choices=choices))


def render_confirmed(purchase: ConfirmedPurchase) -> str:
    lines = [
        f"✅ Purchase confirmed — {purchase.supplier_name}, {purchase.invoice_no}, "
        f"{fmt_date(purchase.invoice_date)}"
    ]
    for line in purchase.lines:
        lines.append(
            f"{line.code}  {fmt_qty(line.qty)} {line.unit_code} × {fmt_money(line.rate)} "
            f"= {fmt_money(line.line_total)} "
            f"(landed {fmt_money(line.landed_cost_per_unit)}/{line.unit_code})"
        )
    lines.append(f"Subtotal: {fmt_money(purchase.subtotal)}")
    if purchase.freight:
        lines.append(f"Freight: {fmt_money(purchase.freight)}")
    if purchase.other_charges:
        lines.append(f"Other charges: {fmt_money(purchase.other_charges)}")
    lines.append(f"Grand total: {fmt_money(purchase.grand_total)} — payable to supplier")
    lines.append("Stock updated:")
    for line in purchase.lines:
        lines.append(
            f"• {line.code} now {fmt_qty(line.resulting_qty)} {line.unit_code} "
            f"@ {fmt_money(line.resulting_avg_cost)}/{line.unit_code} avg"
        )
    return "\n".join(lines)


async def _resolve_draft(draft: Draft, ctx: RequestContext) -> Draft:
    async with ctx.session_factory() as session:
        service = PurchaseService(session)
        org_id = ctx.user.org_id
        if draft.supplier_id is None:
            supplier = await service.resolve_supplier(org_id, draft.supplier_name)
            if supplier is not None:
                draft.supplier_id = supplier.id
                draft.supplier_name = supplier.name
        if draft.brand_name and draft.brand_id is None:
            async with session.begin():
                brand = await service.resolve_or_create_brand(org_id, draft.brand_name)
                draft.brand_id = brand.id
        for line in draft.lines:
            if line.product_id is not None:
                continue
            # The line's own brand wins over the bill's. One bill can
            # carry the same code under two brands -- 1051 had 55X under
            # BSQ and 55X under AR on consecutive rows -- and with only
            # a bill-level brand that had to be entered as two bills.
            brand_id = line.brand_id or draft.brand_id
            product = await service.resolve_product(org_id, line.code, brand_id)
            if product is not None:
                line.product_id = product.id
                line.resolved_code = product.code
                line.unit_code = product.unit.code
                line.brand_choices = []
                continue
            if brand_id is None:
                # Ambiguous rather than unknown: asking beats guessing,
                # and beats offering to create a third product under a
                # code two already share.
                line.brand_choices = await service.brands_carrying(org_id, line.code)
    return draft


async def _apply_correction(
    draft: Draft, correction: re.Match[str], ctx: RequestContext
) -> str | None:
    """Apply one `line N field value` to the draft in place.

    Returns None on success, or a fragment saying what was wrong with
    that one line -- the caller is applying several and has to report on
    each, so this cannot raise or return a whole reply.
    """
    index = int(correction["line"]) - 1
    if not 0 <= index < len(draft.lines):
        return f"there is no line {correction['line']} in this bill"
    field, value = correction["field"].lower(), correction["value"].strip()
    line = draft.lines[index]
    previous_code = line.code
    try:
        if field == "qty":
            line.qty = decimal.Decimal(value)
        elif field == "rate":
            line.rate = decimal.Decimal(value)
        elif field == "brand":
            async with ctx.session_factory() as session:
                brand = await PurchaseService(session).find_brand(ctx.user.org_id, value)
            if brand is None:
                carried = ", ".join(line.brand_choices) or "none on this code"
                return f"there is no brand *{value}* — this code is carried by {carried}"
            # Cleared so the line resolves again under the brand just
            # named; left set it would show the new brand while still
            # pointing at the other one's product.
            line.brand_id = brand
            line.product_id = None
            line.resolved_code = None
            line.unit_code = None
            line.brand_choices = []
        else:
            line.code = value.upper()
            line.product_id = None
            line.resolved_code = None
            line.unit_code = None
            line.brand_choices = []
    except decimal.InvalidOperation:
        return f"*{value}* is not a number"
    if field == "code" and previous_code and previous_code != line.code:
        # the user just told us what that OCR text really meant (§8)
        async with ctx.session_factory() as session, session.begin():
            await OcrService(session).record_correction(
                ctx.user.org_id,
                field="code",
                raw_ocr_text=previous_code,
                corrected_value=line.code,
                supplier_id=draft.supplier_id,
            )
    return None


async def handle_purchase(args: str, ctx: RequestContext) -> CommandResult:
    sessions = SessionService(ctx.session_factory)
    current = await sessions.get(ctx.user.org_id, ctx.user.id)
    if not current.is_idle:
        return CommandResult(
            reply=(
                "You have an unfinished purchase draft — reply CONFIRM to save it, "
                "'discard' to drop it, or finish your corrections first."
            )
        )
    try:
        draft = parse_purchase_command(args)
    except DomainError as exc:
        return CommandResult(reply=exc.message)

    draft = await _resolve_draft(draft, ctx)
    await sessions.set(
        ctx.user.org_id, ctx.user.id, AWAITING_PURCHASE_CONFIRMATION, draft.to_context()
    )
    return preview_result(draft)


async def handle_purchase_session_reply(
    text: str, ctx: RequestContext, state: SessionState
) -> CommandResult:
    sessions = SessionService(ctx.session_factory)
    draft = Draft.from_context(state.context)
    lowered = text.strip().lower()

    if is_abandon(lowered):
        await sessions.clear(ctx.user.org_id, ctx.user.id)
        return CommandResult(reply="Draft discarded.")

    if lowered in {"fix brand", "fix the brand"}:
        # Back to the brand question with the draft intact. Re-answering
        # re-resolves every code against the corrected brand, which is
        # the whole point -- a wrong brand silently duplicates products.
        from backend.api.commands.intake_commands import begin_slots

        draft.brand_name = None
        draft.brand_id = None
        draft.brand_collisions = []
        draft.shared_codes = []
        return await begin_slots(draft, ["brand"], ctx)

    if lowered == "create supplier":
        if draft.supplier_id is not None:
            return CommandResult(reply="Supplier is already set.")
        async with ctx.session_factory() as session:
            service = PurchaseService(session)
            async with session.begin():
                supplier = await service.create_supplier(ctx.user, draft.supplier_name)
            draft.supplier_id = supplier.id
        await sessions.set(
            ctx.user.org_id, ctx.user.id, AWAITING_PURCHASE_CONFIRMATION, draft.to_context()
        )
        return preview_result(draft)

    if _CREATE_ALL.match(text.strip()):
        # A first-ever sheet can carry dozens of unknown codes; creating
        # them one message at a time is unusable. Only codes the sheet
        # gave a description for are eligible -- the rest still get asked
        # about individually rather than invented (docs/04_Purchases.md §10).
        pending = [
            line
            for line in draft.lines
            if line.product_id is None and line.code and line.description
        ]
        if not pending:
            return CommandResult(
                reply="Nothing to create — every line either resolved already or has "
                "no description on the sheet."
            )
        created: list[str] = []
        async with ctx.session_factory() as session:
            service = PurchaseService(session)
            async with session.begin():
                for line in pending:
                    if any(c == line.code for c in created):
                        continue
                    assert line.description is not None
                    await service.create_product(
                        ctx.user, line.code, line.description, draft.brand_id
                    )
                    created.append(line.code)
        draft = await _resolve_draft(draft, ctx)
        await sessions.set(
            ctx.user.org_id, ctx.user.id, AWAITING_PURCHASE_CONFIRMATION, draft.to_context()
        )
        return CommandResult(
            reply=f"✅ Created {len(created)} products: {', '.join(created)}\n\n"
            + render_preview(draft)
        )

    create_match = _CREATE_PRODUCT.match(text.strip())
    if create_match:
        code = create_match["code"].upper()
        if code not in draft.unresolved_codes:
            return CommandResult(reply=f"'{code}' isn't an unresolved item in this draft.")
        described = create_match["description"]
        if not described:
            described = next(
                (
                    line.description
                    for line in draft.lines
                    if line.code == code and line.description
                ),
                None,
            )
        if not described:
            return CommandResult(
                reply=f"I don't have a description for {code} — send "
                f"'create product {code} <description>'."
            )
        async with ctx.session_factory() as session:
            service = PurchaseService(session)
            async with session.begin():
                product = await service.create_product(
                    ctx.user, code, described.strip(), draft.brand_id
                )
            unit_code = await service.resolve_product(ctx.user.org_id, code, draft.brand_id)
            for line in draft.lines:
                if line.code == code:
                    line.product_id = product.id
                    line.resolved_code = product.code
                    line.unit_code = unit_code.unit.code if unit_code else "KG"
        await sessions.set(
            ctx.user.org_id, ctx.user.id, AWAITING_PURCHASE_CONFIRMATION, draft.to_context()
        )
        return preview_result(draft)

    supplier_change = _SET_SUPPLIER.match(text.strip())
    if supplier_change:
        draft.supplier_name = " ".join(supplier_change["name"].split())
        # Cleared so _resolve_draft looks the new name up. Left set, the
        # draft would show the new name while still billing the old one.
        draft.supplier_id = None
        draft = await _resolve_draft(draft, ctx)
        await sessions.set(
            ctx.user.org_id, ctx.user.id, AWAITING_PURCHASE_CONFIRMATION, draft.to_context()
        )
        return preview_result(draft)

    charge = parse_charge(text)
    if charge is not None:
        apply_charge(draft, charge)
        await sessions.set(
            ctx.user.org_id, ctx.user.id, AWAITING_PURCHASE_CONFIRMATION, draft.to_context()
        )
        return preview_result(draft)

    # Corrections arrive one per line, and a bill being fixed usually
    # needs more than one of them. Matching the whole message against
    # `_CORRECTION` meant a two-line message matched nothing at all --
    # `$` without re.MULTILINE will not stop at a newline -- so *both*
    # lines were dropped and the fall-through re-prompt read as
    # acknowledgement. Observed live: the same pair of corrections sent
    # five times, silently discarded five times, and the bill confirmed
    # with the numbers the sender believed they had just changed.
    raw_lines = [entry.strip() for entry in text.strip().splitlines() if entry.strip()]
    if any(_CORRECTION_HINT.match(entry) for entry in raw_lines):
        applied = 0
        problems: list[str] = []
        for entry in raw_lines:
            match = _CORRECTION.match(entry)
            if match is None:
                problems.append(f"• *{entry}* — expected *line <n> qty|rate|code <value>*")
                continue
            failure = await _apply_correction(draft, match, ctx)
            if failure is None:
                applied += 1
            else:
                problems.append(f"• *{entry}* — {failure}")

        if not applied:
            return CommandResult(
                reply="I didn't change anything:\n" + "\n".join(problems),
            )

        draft = await _resolve_draft(draft, ctx)
        await sessions.set(
            ctx.user.org_id, ctx.user.id, AWAITING_PURCHASE_CONFIRMATION, draft.to_context()
        )
        result = preview_result(draft)
        if not problems:
            return result
        # The ones that failed go *above* the redrawn bill. Underneath it
        # they sit below the CONFIRM prompt, and a bill that looks right
        # is confirmed without the warning ever being read.
        notice = f"Applied {applied}, but not these:\n" + "\n".join(problems)
        return dataclasses.replace(result, reply=f"{notice}\n\n{result.reply}")

    if lowered in _GUIDANCE:
        # the button said "I'll do it myself" -- say how, don't re-send
        # the whole preview the user is already looking at
        if draft.unresolved_codes:
            return CommandResult(
                reply="Add them one at a time with:\n"
                f"*create product {draft.unresolved_codes[0]} <description>*\n"
                "Or reply *create all products* to add them all at once."
            )
        return CommandResult(
            reply="Correct a line with:\n*line <n> qty <value>* — or *rate*, or *code*\n"
            "e.g. *line 3 qty 90*"
        )

    if lowered in {"use invoice total", "use calculated total"}:
        draft.total_resolution = "invoice" if "invoice" in lowered else "calculated"
        await sessions.set(
            ctx.user.org_id, ctx.user.id, AWAITING_PURCHASE_CONFIRMATION, draft.to_context()
        )
        return CommandResult(
            reply="Noted. Reply CONFIRM to save with "
            + (
                "the invoice total."
                if draft.total_resolution == "invoice"
                else "the calculated total."
            )
        )

    override = False
    if lowered == "confirm anyway":
        if not draft.pending_override:
            return CommandResult(reply="There's no duplicate warning to override.")
        if not role_at_least(ctx.user.role, UserRole.OWNER):
            return CommandResult(
                reply="Only an owner can override a duplicate warning — "
                "please ask a partner to review this."
            )
        override = True
    elif lowered not in CONFIRM_VOCAB:
        return CommandResult(
            reply="Reply CONFIRM to save this purchase, or tell me what to fix "
            "(e.g. 'line 1 qty 90'), or 'discard'."
        )

    try:
        async with ctx.session_factory() as session:
            confirmed = await PurchaseService(session).confirm(
                ctx.user,
                draft,
                override_duplicate=override,
                whatsapp_message_id=ctx.message_id,
            )
    except ExactDuplicateInvoiceError as exc:
        details = exc.details
        extra = (
            f" (confirmed {details['confirmed_date']}, ₹{details['grand_total']})"
            if "confirmed_date" in details
            else ""
        )
        return CommandResult(
            reply=f"❌ Invoice {draft.invoice_no} from {draft.supplier_name} is already "
            f"recorded{extra}. This wasn't saved — correct the invoice number with "
            "'line ...' or 'discard'."
        )
    except FuzzyDuplicateInvoiceError as exc:
        draft.pending_override = True
        await sessions.set(
            ctx.user.org_id, ctx.user.id, AWAITING_PURCHASE_CONFIRMATION, draft.to_context()
        )
        if role_at_least(ctx.user.role, UserRole.OWNER):
            return CommandResult(reply=exc.message)
        return CommandResult(
            reply=exc.message.replace(
                'Reply "confirm anyway" or "cancel".',
                "Only an owner can confirm past this warning — please forward it to a partner.",
            )
        )
    except TotalMismatchWarning as exc:
        await sessions.set(
            ctx.user.org_id, ctx.user.id, AWAITING_PURCHASE_CONFIRMATION, draft.to_context()
        )
        return CommandResult(reply=exc.message)
    except DomainError as exc:
        return CommandResult(reply=exc.message)

    await sessions.clear(ctx.user.org_id, ctx.user.id)
    return await attach_document(
        CommandResult(reply=render_confirmed(confirmed)),
        ctx,
        kind="purchase",
        reference=str(confirmed.header_id),
    )
