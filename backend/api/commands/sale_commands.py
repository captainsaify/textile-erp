"""`sale` command -- docs/05_Sales.md §2 grammar, §4-§8 warnings,
docs/08_WhatsApp.md #sale.

Sales auto-confirm when nothing looks wrong. When a warning fires
(insufficient stock, below cost, credit limit, near-duplicate) the draft
parks in the session and one reply resolves it: `confirm`, `override`
(stock), corrections, or `cancel`.
"""

from __future__ import annotations

import asyncio
import decimal
import re

from backend.api.command_types import CommandResult, RequestContext
from backend.api.commands.documents import attach_document
from backend.api.formatting import fmt_money, fmt_qty
from backend.api.interactive import (
    MAX_BUTTONS,
    MAX_LIST_ROWS,
    Buttons,
    Choice,
    Interactive,
    ListMenu,
    Section,
    is_abandon,
)
from backend.core.exceptions import (
    DomainError,
    DuplicateSaleError,
    InsufficientStockError,
    ValidationError,
)
from backend.core.logging import get_logger
from backend.core.security import role_at_least
from backend.models.enums import SalePaymentType, UserRole
from backend.services.sales_service import (
    ConfirmedSale,
    SaleDraft,
    SaleDraftLine,
    SalesService,
    SaleWarnings,
    idempotency_key,
)
from backend.services.session_service import (
    AWAITING_SALE_CONFIRMATION,
    SessionService,
    SessionState,
)

_HEADER = re.compile(
    r"Customer:\s*(?P<customer>.+?)(?:\s+(?P<payment>cash|bank|credit))?\s*$", re.IGNORECASE
)
#: Codes off a supplier's sheet are not tidy identifiers -- this catalog
#: alone holds VVP-1, C-ANG, MJP-H and L.P.P. A code class of
#: [A-Za-z0-9_-] silently refused to read the line for any of the dotted
#: ones, so those products could be bought but never sold.
_ITEM = re.compile(r"^(?P<code>[A-Za-z0-9][\w.\-/&]*)\s+(?P<qty>[\d.]+)\s+(?P<rate>[\d.]+)$")
_CORRECTION = re.compile(
    r"^line\s+(?P<line>\d+)\s+(?P<field>qty|rate|code)\s+(?P<value>.+)$", re.IGNORECASE
)
logger = get_logger(__name__)

ZERO = decimal.Decimal("0")
TWO = decimal.Decimal("0.01")

_PICK = re.compile(r"^[1-9]\d*$")

CONFIRM_VOCAB = {"confirm", "yes", "ok", "save"}

USAGE = (
    "Usage:\n"
    "sale Customer: <name> [cash|bank|credit]\n"
    "<CODE> <qty> <rate>   (one line per item)\n"
    "Payment type defaults to credit."
)


def parse_sale_command(args: str) -> SaleDraft:
    lines = [line.strip() for line in args.strip().splitlines() if line.strip()]
    if not lines:
        raise ValidationError(USAGE)
    header = _HEADER.match(lines[0])
    if header is None:
        raise ValidationError(f"Couldn't read the first line. {USAGE}")

    payment = (
        SalePaymentType(header["payment"].lower())
        if header["payment"]
        else SalePaymentType.CREDIT  # §2 default
    )
    items: list[SaleDraftLine] = []
    for raw in lines[1:]:
        item = _ITEM.match(raw)
        if item is None:
            raise ValidationError(
                f"Couldn't read item line '{raw}' — expected: <CODE> <qty> <rate>"
            )
        items.append(
            SaleDraftLine(
                code=item["code"].upper(),
                qty=decimal.Decimal(item["qty"]),
                rate=decimal.Decimal(item["rate"]),
            )
        )
    if not items:
        raise ValidationError("Send at least one item line.")

    return SaleDraft(
        customer_id=None,
        customer_name=header["customer"].strip(),
        payment_type=payment,
        lines=items,
    )


def render_warnings(draft: SaleDraft, warnings: SaleWarnings, *, is_owner: bool) -> str:
    lines: list[str] = []
    for line in warnings.insufficient_stock:
        unit = line.unit_code or ""
        lines.append(
            f"⚠️ Can't complete this sale — {line.resolved_code or line.code} has "
            f"{fmt_qty(line.qty_on_hand)} {unit} in stock, this sale needs "
            f"{fmt_qty(line.qty)} {unit}.".replace("  ", " ")
        )
    for line in warnings.below_cost:
        loss = line.avg_cost - line.rate
        margin = (-loss / line.avg_cost * 100) if line.avg_cost else decimal.Decimal("0")
        unit = line.unit_code or "unit"
        lines.append(
            f"⚠️ {line.resolved_code or line.code} is being sold at "
            f"{fmt_money(line.rate)}/{unit} but average cost is "
            f"{fmt_money(line.avg_cost)}/{unit} "
            f"(loss of {fmt_money(loss)}/{unit}, {margin:.1f}% margin)."
        )
    if warnings.credit_limit is not None:
        limit, projected = warnings.credit_limit
        lines.append(
            f"⚠️ {draft.customer_name}'s credit limit is {fmt_money(limit)}; this sale "
            f"would bring their outstanding to {fmt_money(projected)}."
        )
    if warnings.near_duplicate is not None:
        lines.append(
            f"⚠️ This looks similar to a sale to {draft.customer_name} you recorded a "
            f"few minutes ago ({fmt_money(warnings.near_duplicate.grand_total)})."
        )

    needs_owner = (warnings.below_cost or warnings.credit_limit) and not is_owner
    if warnings.insufficient_stock:
        lines.append(
            'Reply "override" to sell anyway (stock will go negative), '
            "or correct the quantity (e.g. 'line 1 qty 10')."
        )
    elif needs_owner:
        lines.append(
            "Only an owner can confirm past this — please forward it to a partner, "
            "or send a corrected rate."
        )
    else:
        lines.append('Reply "confirm" to proceed anyway, or "cancel".')
    return "\n".join(lines)


def _sale_preview(draft: SaleDraft) -> CommandResult:
    lines = [f"🧾 Sale draft — {draft.customer_name} ({draft.payment_type.value})"]
    for line in draft.lines:
        unit = line.unit_code or ""
        lines.append(
            f"{line.resolved_code or line.code}  {fmt_qty(line.qty)} {unit} × "
            f"{fmt_money(line.rate)} = {fmt_money(line.line_total)}".replace("  ", " ")
        )
    lines.append(f"Total: {fmt_money(draft.grand_total)}")
    lines.append("Reply CONFIRM to save, 'sheet' to see it as a spreadsheet, or 'discard'.")
    return CommandResult(
        reply="\n".join(lines),
        interactive=Buttons(
            body=f"Save this sale? {fmt_money(draft.grand_total)} to {draft.customer_name}.",
            choices=(
                Choice(id="confirm", title="Confirm"),
                Choice(id="sheet", title="See as sheet"),
                Choice(id="discard", title="Discard"),
            ),
        ),
    )


def render_sale(sale: ConfirmedSale) -> str:
    lines = [f"✅ Sale recorded — {sale.customer_name} ({sale.payment_type.value})"]
    for line in sale.lines:
        lines.append(
            f"{line.code}  {fmt_qty(line.qty)} {line.unit_code} × {fmt_money(line.rate)} "
            f"= {fmt_money(line.line_total)}"
        )
    lines.append(f"Total: {fmt_money(sale.grand_total)}")
    if sale.payment_type is SalePaymentType.CREDIT:
        lines.append(
            f"{sale.customer_name} now owes: {fmt_money(sale.outstanding_after)} "
            f"(was {fmt_money(sale.outstanding_before)})"
        )
    elif sale.ledger_balance is not None:
        label = "Cash" if sale.payment_type is SalePaymentType.CASH else "Bank"
        lines.append(f"{label} balance now {fmt_money(sale.ledger_balance)}")
    stock = " · ".join(
        f"{line.code} {fmt_qty(line.resulting_qty)} {line.unit_code}" for line in sale.lines
    )
    lines.append(f"Stock after: {stock}")
    return "\n".join(lines)


def _render_unresolved(draft: SaleDraft, candidates: list[str]) -> str:
    lines = []
    if candidates:
        lines.append(f"Which customer did you mean for '{draft.customer_name}'?")
        lines.extend(f"{index}. {name}" for index, name in enumerate(candidates, start=1))
        lines.append("Reply with the number.")
    else:
        lines.append(
            f"Customer '{draft.customer_name}' not found — reply 'create customer' to add them."
        )
    if draft.unresolved_codes:
        lines.append("Unknown products: " + ", ".join(draft.unresolved_codes))
        lines.append("Correct them with 'line N code <CODE>', or add them via a purchase first.")
    return "\n".join(lines)


def _brand_question(line: SaleDraftLine) -> CommandResult:
    """Which brand's product this line means.

    A code is unique only within a brand, so VVP names a golden velvet
    pant under TOP and a velvet sport pant under MKD. Until this is
    answered the line has no product -- but it is not an unknown code,
    and telling someone to "add it via a purchase first" sent them to
    fix a catalogue that was already right.
    """
    body = f"Which brand is {line.code}?"
    choices = tuple(
        Choice(id=f"brand {name}", title=name[:20]) for name in line.brand_choices[:MAX_BUTTONS]
    )
    if len(line.brand_choices) <= MAX_BUTTONS:
        interactive: Interactive = Buttons(body=body, choices=choices)
    else:
        interactive = ListMenu(
            body=body,
            menu_label="Pick brand",
            sections=(
                Section(
                    title="Brands",
                    rows=tuple(
                        Choice(id=f"brand {name}", title=name[:24])
                        for name in line.brand_choices[:MAX_LIST_ROWS]
                    ),
                ),
            ),
        )
    listed = ", ".join(line.brand_choices)
    return CommandResult(
        reply=f"{line.code} is sold under {len(line.brand_choices)} brands ({listed}). "
        "Which one is this?",
        interactive=interactive,
    )


async def _resolve_brand_choice(line: SaleDraftLine, answer: str, ctx: RequestContext) -> bool:
    """Point the line at that brand's product directly, rather than
    storing the brand and re-deriving it: "no brand" is a real answer
    and a null brand_id cannot express it."""
    from backend.repositories.product_repository import ProductRepository

    wanted = answer.strip().lower()
    async with ctx.session_factory() as session:
        carriers = await ProductRepository(session).list_by_code(ctx.user.org_id, line.code)
    chosen = next(
        (
            product
            for product in carriers
            if (product.brand.name if product.brand else "no brand").lower() == wanted
        ),
        None,
    )
    if chosen is None:
        return False
    line.product_id = chosen.id
    line.brand_id = chosen.brand_id
    line.resolved_code = chosen.code
    line.unit_code = chosen.unit.code
    line.brand_choices = []
    return True


async def _try_record(
    draft: SaleDraft, ctx: RequestContext, *, below_cost_confirmed: bool = False
) -> CommandResult:
    sessions = SessionService(ctx.session_factory)
    try:
        async with ctx.session_factory() as session:
            sale = await SalesService(session).record(
                ctx.user,
                draft,
                below_cost_confirmed=below_cost_confirmed,
                whatsapp_message_id=ctx.message_id,
            )
    except DuplicateSaleError:
        await sessions.clear(ctx.user.org_id, ctx.user.id)
        return CommandResult(
            reply="↩️ This looks identical to a sale you just sent — not recorded again. "
            "If it really is a second, separate sale, add a note to the message and resend."
        )
    except InsufficientStockError as exc:
        return CommandResult(reply=exc.message)
    except DomainError as exc:
        return CommandResult(reply=exc.message)
    await sessions.clear(ctx.user.org_id, ctx.user.id)
    return await attach_document(
        CommandResult(reply=render_sale(sale)), ctx, kind="sale", reference=str(sale.sale_id)
    )


async def _prepare(draft: SaleDraft, ctx: RequestContext) -> tuple[SaleDraft, list[str]]:
    """Resolve customer + products; returns candidate names when ambiguous."""
    async with ctx.session_factory() as session:
        service = SalesService(session)
        candidates: list[str] = []
        if draft.customer_id is None:
            matches = await service.resolve_customer(ctx.user.org_id, draft.customer_name)
            if len(matches) == 1:
                draft.customer_id = matches[0].id
                draft.customer_name = matches[0].name
            elif matches:
                candidates = [customer.name for customer in matches]
        draft = await service.hydrate(ctx.user.org_id, draft)
    return draft, candidates


async def handle_sale(args: str, ctx: RequestContext) -> CommandResult:
    sessions = SessionService(ctx.session_factory)
    current = await sessions.get(ctx.user.org_id, ctx.user.id)
    if not current.is_idle:
        return CommandResult(
            reply="Finish the draft you already have first — reply CONFIRM, "
            "'cancel', or answer the question above."
        )
    try:
        draft = parse_sale_command(args)
    except DomainError as exc:
        return CommandResult(reply=exc.message)

    draft.idempotency_key = idempotency_key(ctx.user.whatsapp_number or "", args)
    draft, candidates = await _prepare(draft, ctx)

    if draft.customer_id is None or draft.unresolved_codes or draft.needs_brand:
        await sessions.set(
            ctx.user.org_id, ctx.user.id, AWAITING_SALE_CONFIRMATION, draft.to_context()
        )
        # The customer question comes first when both are open: the
        # brand answer is per line, and answering it into a draft whose
        # customer is still unresolved leaves two questions in flight.
        if draft.customer_id is not None and draft.needs_brand is not None:
            return _brand_question(draft.needs_brand)
        return CommandResult(reply=_render_unresolved(draft, candidates))

    async with ctx.session_factory() as session:
        warnings = await SalesService(session).check_warnings(ctx.user.org_id, draft)
    await sessions.set(ctx.user.org_id, ctx.user.id, AWAITING_SALE_CONFIRMATION, draft.to_context())
    if warnings.any:
        return CommandResult(
            reply=render_warnings(
                draft, warnings, is_owner=role_at_least(ctx.user.role, UserRole.OWNER)
            ),
            interactive=Buttons(
                body=f"Save this sale? {fmt_money(draft.grand_total)} to {draft.customer_name}.",
                choices=(
                    Choice(id="confirm", title="Confirm anyway"),
                    Choice(id="sheet", title="See as sheet"),
                    Choice(id="discard", title="Discard"),
                ),
            ),
        )
    # Previewed, not auto-recorded. Sales used to confirm themselves on
    # the grounds that `undo` is cheap -- but a sale of the wrong brand's
    # stock at the wrong rate is cheap to reverse and expensive to
    # notice, and the same CONFIRM step already guards every purchase.
    return _sale_preview(draft)


async def handle_sale_session_reply(
    text: str, ctx: RequestContext, state: SessionState
) -> CommandResult:
    sessions = SessionService(ctx.session_factory)
    draft = SaleDraft.from_context(state.context)
    lowered = text.strip().lower()

    if is_abandon(lowered):
        await sessions.clear(ctx.user.org_id, ctx.user.id)
        return CommandResult(reply="Sale discarded.")

    pending = draft.needs_brand
    if pending is not None and lowered not in CONFIRM_VOCAB:
        # While a brand question is open it owns the next message --
        # otherwise a bare "TOP" would fall through to the correction
        # parser and end up as "I don't understand".
        answer = text.strip()
        if lowered.startswith("brand "):
            answer = answer[len("brand ") :].strip()
        resolved = await _resolve_brand_choice(pending, answer, ctx)
        if not resolved:
            return CommandResult(
                reply=f"'{answer}' isn't one of {pending.code}'s brands.",
                interactive=_brand_question(pending).interactive,
            )
        # Re-hydrated, not just pointed at a product: stock and average
        # cost are snapshotted per line, and the snapshot for a line
        # that had no product yet is zero -- which would have reported
        # "0 KG in stock" for a product with plenty.
        draft, _ = await _prepare(draft, ctx)
        # Parked before continuing, never after: a draft that records
        # clears the session itself, and re-saving it afterwards would
        # leave a finished sale sitting in the state machine.
        await sessions.set(
            ctx.user.org_id, ctx.user.id, AWAITING_SALE_CONFIRMATION, draft.to_context()
        )
        return await _continue_after_resolution(draft, ctx)

    if lowered == "create customer":
        if draft.customer_id is not None:
            return CommandResult(reply="Customer is already set.")
        async with ctx.session_factory() as session:
            async with session.begin():
                customer = await SalesService(session).create_customer(
                    ctx.user, draft.customer_name
                )
            draft.customer_id = customer.id
        draft, _ = await _prepare(draft, ctx)
        await sessions.set(
            ctx.user.org_id, ctx.user.id, AWAITING_SALE_CONFIRMATION, draft.to_context()
        )
        return await _continue_after_resolution(draft, ctx)

    if _PICK.match(lowered) and draft.customer_id is None:
        async with ctx.session_factory() as session:
            matches = await SalesService(session).resolve_customer(
                ctx.user.org_id, draft.customer_name
            )
        index = int(lowered) - 1
        if not 0 <= index < len(matches):
            return CommandResult(reply=f"There's no option {lowered}.")
        draft.customer_id = matches[index].id
        draft.customer_name = matches[index].name
        draft, _ = await _prepare(draft, ctx)
        await sessions.set(
            ctx.user.org_id, ctx.user.id, AWAITING_SALE_CONFIRMATION, draft.to_context()
        )
        return await _continue_after_resolution(draft, ctx)

    correction = _CORRECTION.match(text.strip())
    if correction:
        index = int(correction["line"]) - 1
        if not 0 <= index < len(draft.lines):
            return CommandResult(reply=f"There's no line {correction['line']} in this sale.")
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
        # a corrected message is a different sale: re-key idempotency
        draft.idempotency_key = idempotency_key(
            ctx.user.whatsapp_number or "", f"{draft.customer_name}|{text}"
        )
        draft, _ = await _prepare(draft, ctx)
        await sessions.set(
            ctx.user.org_id, ctx.user.id, AWAITING_SALE_CONFIRMATION, draft.to_context()
        )
        return await _continue_after_resolution(draft, ctx)

    if lowered == "override":
        draft.allow_negative_stock = True
        await sessions.set(
            ctx.user.org_id, ctx.user.id, AWAITING_SALE_CONFIRMATION, draft.to_context()
        )
        return await _try_record(draft, ctx)

    if lowered in CONFIRM_VOCAB:
        async with ctx.session_factory() as session:
            warnings = await SalesService(session).check_warnings(ctx.user.org_id, draft)
        if warnings.insufficient_stock and not draft.allow_negative_stock:
            return CommandResult(
                reply=render_warnings(
                    draft, warnings, is_owner=role_at_least(ctx.user.role, UserRole.OWNER)
                )
            )
        if (warnings.below_cost or warnings.credit_limit) and not role_at_least(
            ctx.user.role, UserRole.OWNER
        ):
            return CommandResult(
                reply="Only an owner can confirm past a below-cost or credit-limit "
                "warning — please forward this to a partner."
            )
        return await _try_record(draft, ctx, below_cost_confirmed=bool(warnings.below_cost))

    return CommandResult(
        reply="Reply 'confirm' to record this sale, 'override' if it's a stock warning, "
        "a correction like 'line 1 qty 10', or 'cancel'."
    )


async def _continue_after_resolution(draft: SaleDraft, ctx: RequestContext) -> CommandResult:
    """After a customer/product/correction answer: ask again if still
    unresolved, warn if warnings remain, otherwise record."""
    if draft.customer_id is not None and draft.needs_brand is not None:
        return _brand_question(draft.needs_brand)
    if draft.customer_id is None or draft.unresolved_codes:
        return CommandResult(reply=_render_unresolved(draft, []))
    async with ctx.session_factory() as session:
        warnings = await SalesService(session).check_warnings(ctx.user.org_id, draft)
    if warnings.any:
        return CommandResult(
            reply=render_warnings(
                draft, warnings, is_owner=role_at_least(ctx.user.role, UserRole.OWNER)
            )
        )
    return _sale_preview(draft)


# --------------------------------------------------------------------
# reading a sales note from a photo
# --------------------------------------------------------------------


async def read_stored_sale_sheet(attachment_id_text: str, ctx: RequestContext) -> CommandResult:
    """A photographed sales note -> the same SaleDraft the typed `sale`
    command produces (docs/20_ConversationalIntake.md §2).

    Everything after the read is shared with the typed path: customer
    resolution, stock checks, the below-cost warning and CONFIRM. This
    only replaces the typing.
    """
    import uuid as uuid_module
    from pathlib import Path

    from backend.models import Attachment
    from backend.ocr.vision_engine import VisionSheetReader, VisionUnavailableError

    org_id = ctx.user.org_id
    try:
        attachment_id = uuid_module.UUID(attachment_id_text)
    except ValueError:
        return CommandResult(reply="That photo has expired — please send it again.")

    async with ctx.session_factory() as session:
        attachment = await session.get(Attachment, attachment_id)
        if attachment is None or attachment.org_id != org_id:
            return CommandResult(reply="That photo has expired — please send it again.")
        file_path, mime_type = attachment.file_path, attachment.mime_type

    reader = VisionSheetReader()
    if not reader.available():
        # There is no local fallback for sales notes: the grid-detection
        # pipeline is built around the purchase column template. Say so
        # rather than returning an empty draft.
        return CommandResult(
            reply="I can't read sheets right now — vision isn't configured.\n"
            "Record it with:\n*sale Customer: <name>*\n*<CODE> <qty> <rate>*"
        )
    try:
        data = Path(file_path).read_bytes()
    except OSError:
        return CommandResult(reply="I can't find that photo any more — please send it again.")

    await ctx.say("📸 Reading your sales note, one moment…")
    try:
        sheet = await asyncio.to_thread(reader.read_sale_sheet, data, mime_type)
    except VisionUnavailableError as exc:
        logger.warning("vision_sale_read_failed", error=str(exc))
        return CommandResult(
            reply="❌ I couldn't read that one. Record it with:\n"
            "*sale Customer: <name>*\n*<CODE> <qty> <rate>*"
        )

    lines: list[SaleDraftLine] = []
    notes: list[str] = []
    for index, row in enumerate(sheet.rows, start=1):
        qty = _sheet_number(row.qty)
        rate = _sheet_number(row.rate)
        if not row.code or qty is None or rate is None:
            notes.append(f"Line {index}: couldn't read this row fully — check it before confirming")
            if qty is None or rate is None:
                continue
        lines.append(SaleDraftLine(code=row.code.upper(), qty=qty, rate=rate))

        # The sheet's own total is checked against qty x rate and any
        # disagreement is *surfaced*, never resolved. When two sources
        # disagree and you can't tell which is wrong, showing it is the
        # whole thesis of this system (CLAUDE.md).
        stated = _sheet_number(row.line_total)
        if stated is not None and (qty * rate).quantize(TWO) != stated.quantize(TWO):
            notes.append(
                f"Line {index} ({row.code}): the sheet says {fmt_money(stated)}, "
                f"but {fmt_qty(qty)} x {fmt_money(rate)} is {fmt_money((qty * rate).quantize(TWO))}"
                " — which is right?"
            )

    if not lines:
        return CommandResult(
            reply="❌ I couldn't find any item rows on that. Record it with:\n"
            "*sale Customer: <name>*\n*<CODE> <qty> <rate>*"
        )

    draft = SaleDraft(
        customer_id=None,
        customer_name=sheet.customer_name.strip(),
        payment_type=SalePaymentType.CREDIT,  # §2 default; changeable before CONFIRM
        lines=lines,
    )
    draft.idempotency_key = idempotency_key(ctx.user.whatsapp_number or "", attachment_id_text)
    draft, candidates = await _prepare(draft, ctx)

    header = [f"📸 Read {len(lines)} item(s) from your sales note."]
    if sheet.customer_name.strip():
        header.append(f"Customer: {sheet.customer_name.strip()}")
    if sheet.declared_total.strip():
        header.append(f"Sheet total: {sheet.declared_total.strip()}")
    header.extend(f"⚠️ {note}" for note in notes)
    if sheet.unreadable_note.strip():
        header.append(f"⚠️ {sheet.unreadable_note.strip()}")

    sessions = SessionService(ctx.session_factory)
    await sessions.set(org_id, ctx.user.id, AWAITING_SALE_CONFIRMATION, draft.to_context())

    if draft.customer_id is None or draft.unresolved_codes:
        body = _render_unresolved(draft, candidates)
    else:
        resolved = await _continue_after_resolution(draft, ctx)
        body = resolved.reply
    return CommandResult(reply="\n".join([*header, "", body]))


def _sheet_number(raw: str) -> decimal.Decimal | None:
    """A handwritten figure -> Decimal, or None when it isn't one.

    Never a float: this becomes a rate and a quantity, and the whole
    system is Decimal from the database up.
    """
    cleaned = re.sub(r"[^\d.]", "", (raw or "").replace(",", ""))
    if not cleaned or cleaned == ".":
        return None
    try:
        value = decimal.Decimal(cleaned)
    except decimal.InvalidOperation:
        return None
    return value if value > ZERO else None
