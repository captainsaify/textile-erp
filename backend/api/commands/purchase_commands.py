"""`purchase` command and its confirmation session -- docs/04_Purchases.md
§3, docs/08_WhatsApp.md #purchase and §5 (state machine).

The draft lives in the WhatsApp session until CONFIRM. While the session
is in awaiting_purchase_confirmation, non-command replies route here:
CONFIRM vocabulary, corrections (`line N field value`), `create
supplier`, `create product CODE desc`, duplicate override, total-
mismatch resolution, `discard`.
"""

from __future__ import annotations

import datetime
import decimal
import re

from backend.api.command_types import CommandResult, RequestContext
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
    r"^line\s+(?P<line>\d+)\s+(?P<field>code|qty|rate)\s+(?P<value>.+)$", re.IGNORECASE
)
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
        lines.append(f"Other charges: {fmt_money(draft.other_charges)}")
    lines.append(f"Grand total: {fmt_money(draft.grand_total)}")
    if draft.declared_total is not None:
        lines.append(f"Invoice shows: {fmt_money(draft.declared_total)}")
    if draft.supplier_name and draft.supplier_id is None:
        warnings.append(
            f"Supplier '{draft.supplier_name}' not found — reply 'create supplier' to add them."
        )
    missing_details = not draft.supplier_name or not draft.invoice_no
    if any(line.rate == 0 for line in draft.lines):
        warnings.append("Some lines have no rate yet.")
    lines.extend(f"⚠️ {warning}" for warning in warnings)
    if missing_details:
        from backend.api.commands.ocr_commands import DETAILS_PROMPT

        lines.append(DETAILS_PROMPT)
    elif draft.unresolved_codes:
        # Naming the command matters: `create all products` existed from
        # the start but nothing ever mentioned it, so a first purchase --
        # where *every* code is new -- looked like a dead end.
        lines.append(unresolved_help(draft.unresolved_codes))
    elif draft.supplier_id is None:
        lines.append("Resolve the supplier above, then reply CONFIRM to save.")
    else:
        lines.append("Reply CONFIRM to save, or send corrections (e.g. 'line 1 qty 90').")
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
        f"Then reply CONFIRM to save."
    )


def preview_result(draft: Draft) -> CommandResult:
    """The preview plus whatever decision it is actually asking for.

    Which buttons appear follows the draft's own state, so the offer can
    never contradict the text: unresolved codes ask to create them,
    an unknown supplier asks to create it, and only a draft that is
    ready to save offers CONFIRM.
    """
    reply = render_preview(draft)
    choices: tuple[Choice, ...]
    # Asked before "these aren't in your catalogue": a collided code is
    # always also an unresolved one, and "are these really different
    # products?" is the sharper question. Getting the bulk-create prompt
    # first would have someone create six duplicates in one tap.
    if draft.brand_collisions:
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
    elif draft.unresolved_codes:
        count = len(draft.unresolved_codes)
        bulk = f"Create all {count}" if count < 100000 else "Create all"
        body = f"{count} item(s) aren't in your catalogue yet."
        choices = (
            Choice(id="create all products", title=bulk),
            Choice(id="one by one", title="One by one"),
            Choice(id="discard", title="Discard"),
        )
    elif draft.supplier_id is None and draft.supplier_name:
        body = f"Supplier '{draft.supplier_name}' isn't in your list yet."
        choices = (
            Choice(id="create supplier", title="Add supplier"),
            Choice(id="discard", title="Discard"),
        )
    elif not draft.supplier_name or not draft.invoice_no:
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
            if line.product_id is None:
                product = await service.resolve_product(org_id, line.code, draft.brand_id)
                if product is not None:
                    line.product_id = product.id
                    line.resolved_code = product.code
                    line.unit_code = product.unit.code
    return draft


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

    correction = _CORRECTION.match(text.strip())
    if correction:
        index = int(correction["line"]) - 1
        if not 0 <= index < len(draft.lines):
            return CommandResult(reply=f"There's no line {correction['line']} in this draft.")
        field, value = correction["field"].lower(), correction["value"].strip()
        line = draft.lines[index]
        previous_code = line.code
        try:
            if field == "qty":
                line.qty = decimal.Decimal(value)
            elif field == "rate":
                line.rate = decimal.Decimal(value)
            else:
                line.code = value.upper()
                line.product_id = None
                line.resolved_code = None
                line.unit_code = None
        except decimal.InvalidOperation:
            return CommandResult(reply=f"'{value}' is not a valid number.")
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
        draft = await _resolve_draft(draft, ctx)
        await sessions.set(
            ctx.user.org_id, ctx.user.id, AWAITING_PURCHASE_CONFIRMATION, draft.to_context()
        )
        return preview_result(draft)

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
    return CommandResult(reply=render_confirmed(confirmed))
