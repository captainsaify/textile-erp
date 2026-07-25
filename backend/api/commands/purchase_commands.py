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
from backend.core.exceptions import (
    DomainError,
    ExactDuplicateInvoiceError,
    FuzzyDuplicateInvoiceError,
    TotalMismatchWarning,
    ValidationError,
)
from backend.core.security import role_at_least
from backend.models.enums import UserRole
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
_ITEM = re.compile(r"^(?P<code>[A-Za-z0-9_-]+)\s+(?P<qty>[\d.]+)\s+(?P<rate>[\d.]+)$")
_LABELED = re.compile(r"^(?P<label>freight|other|total):\s*(?P<amount>[\d.]+)$", re.IGNORECASE)
_CORRECTION = re.compile(
    r"^line\s+(?P<line>\d+)\s+(?P<field>code|qty|rate)\s+(?P<value>.+)$", re.IGNORECASE
)
_CREATE_PRODUCT = re.compile(
    r"^create\s+product\s+(?P<code>[A-Za-z0-9_-]+)\s+(?P<description>.+)$", re.IGNORECASE
)

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


def render_preview(draft: Draft) -> str:
    lines = [
        f"✅ Purchase draft ready — {draft.supplier_name}, {draft.invoice_no}, "
        f"{fmt_date(draft.invoice_date)}"
    ]
    warnings: list[str] = []
    for index, line in enumerate(draft.lines, start=1):
        if line.product_id is None:
            lines.append(
                f"❓ {line.code}  {fmt_qty(line.qty)} × {fmt_money(line.rate)} — unknown product"
            )
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
        lines.append(
            f"{line.resolved_code or line.code}  {fmt_qty(line.qty)} {unit} × "
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
    if draft.supplier_id is None:
        warnings.append(
            f"Supplier '{draft.supplier_name}' not found — reply 'create supplier' to add them."
        )
    lines.extend(f"⚠️ {warning}" for warning in warnings)
    if draft.unresolved_codes or draft.supplier_id is None:
        lines.append("Resolve the items above, then reply CONFIRM to save.")
    else:
        lines.append("Reply CONFIRM to save, or send corrections (e.g. 'line 1 qty 90').")
    return "\n".join(lines)


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
                product = await service.resolve_product(org_id, line.code)
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
    return CommandResult(reply=render_preview(draft))


async def handle_purchase_session_reply(
    text: str, ctx: RequestContext, state: SessionState
) -> CommandResult:
    sessions = SessionService(ctx.session_factory)
    draft = Draft.from_context(state.context)
    lowered = text.strip().lower()

    if lowered in {"discard", "cancel"}:
        await sessions.clear(ctx.user.org_id, ctx.user.id)
        return CommandResult(reply="Draft discarded.")

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
        return CommandResult(reply=render_preview(draft))

    create_match = _CREATE_PRODUCT.match(text.strip())
    if create_match:
        code = create_match["code"].upper()
        if code not in draft.unresolved_codes:
            return CommandResult(reply=f"'{code}' isn't an unresolved item in this draft.")
        async with ctx.session_factory() as session:
            service = PurchaseService(session)
            async with session.begin():
                product = await service.create_product(
                    ctx.user, code, create_match["description"].strip()
                )
            unit_code = await service.resolve_product(ctx.user.org_id, code)
            for line in draft.lines:
                if line.code == code:
                    line.product_id = product.id
                    line.resolved_code = product.code
                    line.unit_code = unit_code.unit.code if unit_code else "KG"
        await sessions.set(
            ctx.user.org_id, ctx.user.id, AWAITING_PURCHASE_CONFIRMATION, draft.to_context()
        )
        return CommandResult(reply=render_preview(draft))

    correction = _CORRECTION.match(text.strip())
    if correction:
        index = int(correction["line"]) - 1
        if not 0 <= index < len(draft.lines):
            return CommandResult(reply=f"There's no line {correction['line']} in this draft.")
        field, value = correction["field"].lower(), correction["value"].strip()
        line = draft.lines[index]
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
        draft = await _resolve_draft(draft, ctx)
        await sessions.set(
            ctx.user.org_id, ctx.user.id, AWAITING_PURCHASE_CONFIRMATION, draft.to_context()
        )
        return CommandResult(reply=render_preview(draft))

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
