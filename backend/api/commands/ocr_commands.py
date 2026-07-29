"""The OCR purchase path -- docs/07_OCR.md §1, §10-§12 and
docs/04_Purchases.md §2.

A photo becomes the same purchase Draft the typed `purchase` command
produces, so both entry paths share one confirmation flow. What the
sheet can't tell us (supplier, invoice no/date, rate, freight) is asked
for via `details ...`, the template's required_manual_fields.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import re
import uuid
from pathlib import Path
from typing import Any

from backend.api.command_types import CommandResult, RequestContext
from backend.api.commands.intake_commands import ask_intent, begin_slots, missing_slots
from backend.api.commands.purchase_commands import preview_result
from backend.api.formatting import fmt_date, fmt_qty
from backend.core.exceptions import DomainError, ValidationError
from backend.core.logging import get_logger
from backend.models import Attachment
from backend.ocr.engines import DualEngine, PaddleEngine, TesseractEngine
from backend.ocr.pipeline import OcrFailure, ParsedSheet, parse_sheet
from backend.ocr.vision_engine import VisionSheetReader, VisionUnavailableError
from backend.services.ocr_service import OcrService
from backend.services.purchase_service import Draft, PurchaseService
from backend.services.session_service import (
    AWAITING_INTENT,
    AWAITING_PURCHASE_CONFIRMATION,
    SessionService,
)

logger = get_logger(__name__)

_DETAILS = re.compile(
    r"Supplier:\s*(?P<supplier>.+?)"
    r"(?:\s+Invoice:\s*(?P<invoice>\S+))?"
    r"(?:\s+Date:\s*(?P<date>\d{2}-\d{2}-\d{4}))?"
    r"(?:\s+Rate:\s*(?P<rate>[\d.]+))?"
    r"(?:\s+Brand:\s*(?P<brand>.+?))?"
    r"(?:\s+Freight:\s*(?P<freight>[\d.]+))?"
    r"(?:\s+Other:\s*(?P<other>[\d.]+))?"
    r"(?:\s+Total:\s*(?P<total>[\d.]+))?\s*$",
    re.IGNORECASE,
)

DETAILS_PROMPT = (
    "Now send the invoice details:\n"
    "details Supplier: <name> Invoice: <no> Date: DD-MM-YYYY Rate: <rate per unit> "
    "[Brand: <name>] [Freight: <amt>] [Other: <amt>] [Total: <amt>]"
)


def build_engine(primary_name: str) -> DualEngine:
    """Primary per settings, the other engine as fallback (§6)."""
    paddle, tesseract = PaddleEngine(), TesseractEngine()
    if primary_name == "tesseract":
        return DualEngine(primary=tesseract, fallback=paddle)
    return DualEngine(primary=paddle, fallback=tesseract)


def render_ocr_result(
    draft: Draft,
    *,
    low_confidence: list[str],
    auto_corrections: list[str],
    unmapped_headers: list[str],
    hard_to_read: bool,
    page_count: int,
) -> str:
    lines = [
        f"📸 Read {len(draft.lines)} item{'s' if len(draft.lines) != 1 else ''} from your sheet:"
    ]
    for index, line in enumerate(draft.lines, start=1):
        unit = line.unit_code or "KG"
        flag = "" if line.product_id else " ⚠️ unknown product"
        code = line.code or "?"
        lines.append(f"{index}. {code}  {fmt_qty(line.qty)} {unit}{flag}")
    lines.extend(auto_corrections)
    if low_confidence:
        lines.append("Couldn't read clearly:")
        lines.extend(f"• {note}" for note in low_confidence)
    if unmapped_headers:
        lines.append(
            "I saw column"
            + ("s " if len(unmapped_headers) > 1 else " ")
            + ", ".join(f"'{header}'" for header in unmapped_headers)
            + " I don't recognize — ignoring for now."
        )
    unknown = [line.code for line in draft.lines if not line.product_id and line.code]
    if unknown:
        from backend.api.commands.purchase_commands import unresolved_help

        lines.append(unresolved_help(unknown))
    if hard_to_read:
        lines.append(
            "⚠️ This photo is hard to read — you may want to retake it, or keep correcting manually."
        )
    if page_count > 1:
        lines.append(
            f"ℹ️ That PDF has {page_count} pages; I read page 1. Send the other pages separately."
        )
    return "\n".join(lines)


#: Date formats a sheet actually prints, most specific first.
_DATE_HINT_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%y", "%d/%m/%y", "%d.%m.%Y")


def apply_date_hint(draft: Draft, hint: str) -> bool:
    """Set the draft's date from what the sheet printed. Returns whether
    the sheet actually stated a date -- an unreadable or absent one
    becomes a question rather than today's date silently standing in."""
    text = hint.strip()
    if not text:
        return False
    for fmt in _DATE_HINT_FORMATS:
        try:
            draft.invoice_date = datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        return True
    logger.info("ocr_invoice_date_unparsed", value=text)
    return False


def apply_details(draft: Draft, args: str) -> Draft:
    match = _DETAILS.match(args.strip())
    if match is None:
        raise ValidationError(DETAILS_PROMPT)
    draft.supplier_name = match["supplier"].strip()
    draft.supplier_id = None  # re-resolved against the new name
    if match["invoice"]:
        draft.invoice_no = match["invoice"].strip()
    if match["date"]:
        try:
            draft.invoice_date = datetime.datetime.strptime(match["date"], "%d-%m-%Y").date()
        except ValueError:
            raise ValidationError(f"'{match['date']}' is not a valid DD-MM-YYYY date.") from None
    if match["brand"]:
        draft.brand_name = match["brand"].strip()
    if match["rate"]:
        rate = decimal.Decimal(match["rate"])
        for line in draft.lines:
            if line.rate == 0:
                line.rate = rate
    if match["freight"]:
        draft.freight = decimal.Decimal(match["freight"])
    if match["other"]:
        draft.other_charges = decimal.Decimal(match["other"])
    if match["total"]:
        draft.declared_total = decimal.Decimal(match["total"])
    if not draft.invoice_no:
        raise ValidationError("I still need an invoice number — include 'Invoice: <no>'.")
    return draft


async def handle_details(args: str, ctx: RequestContext) -> CommandResult:
    """`details ...` -- fills the required_manual_fields on an
    OCR-derived draft, then shows the normal purchase preview."""
    sessions = SessionService(ctx.session_factory)
    state = await sessions.get(ctx.user.org_id, ctx.user.id)
    if state.state != AWAITING_PURCHASE_CONFIRMATION:
        return CommandResult(
            reply="There's no purchase draft waiting for details. Send a photo of a "
            "purchase sheet, or use the 'purchase' command."
        )
    draft = Draft.from_context(state.context)
    try:
        draft = apply_details(draft, args)
    except DomainError as exc:
        return CommandResult(reply=exc.message)

    draft = await resolve_after_details(draft, ctx)
    await sessions.set(
        ctx.user.org_id, ctx.user.id, AWAITING_PURCHASE_CONFIRMATION, draft.to_context()
    )
    return preview_result(draft)


async def resolve_after_details(draft: Draft, ctx: RequestContext) -> Draft:
    """Match the draft against the catalogue once supplier/brand are
    known. Shared by `details` and the slot machine so both produce an
    identically-resolved draft (docs/20 §10)."""
    # The transaction opens before the first read. Resolving the supplier
    # autobegins one, so entering session.begin() afterwards raises
    # "A transaction is already begun" (HANDOFF.md §5) -- and that inner
    # begin() only ever runs when the draft carries a brand, which never
    # happened until the sheet's LABEL column started supplying one. The
    # branch was wrong the day it was written and simply never executed.
    async with ctx.session_factory() as session, session.begin():
        service = PurchaseService(session)
        supplier = await service.resolve_supplier(ctx.user.org_id, draft.supplier_name)
        if supplier is not None:
            draft.supplier_id = supplier.id
            draft.supplier_name = supplier.name
        if draft.brand_name and draft.brand_id is None:
            brand = await service.resolve_or_create_brand(ctx.user.org_id, draft.brand_name)
            draft.brand_id = brand.id
        # Re-resolved now, not only when unresolved: the brand arrives with
        # this command, and codes are only unique within a brand, so a
        # match made before the brand was known may point at the wrong
        # brand's product.
        for line in draft.lines:
            if not line.code:
                continue
            product = await service.resolve_product(ctx.user.org_id, line.code, draft.brand_id)
            if product is not None:
                line.product_id = product.id
                line.resolved_code = product.code
                line.unit_code = product.unit.code
            elif draft.brand_id is not None:
                # ambiguous or foreign-brand match -- let it be created
                # under this brand rather than silently reusing another's
                line.product_id = None
                line.resolved_code = None
    return draft



async def _cached_vision(
    attachment_id: uuid.UUID, ctx: RequestContext
) -> tuple[list[Any], dict[str, str]] | None:
    """The rows a previous vision call already returned for this photo.

    Cached on the attachment rather than in Redis: the photo and its
    transcription belong together and should survive a restart, and the
    column was already there holding a summary of the same read.
    """
    from backend.ocr.vision_engine import rows_from_json

    async with ctx.session_factory() as session:
        attachment = await session.get(Attachment, attachment_id)
        payload = (attachment.ocr_result or {}) if attachment is not None else {}
    rows = payload.get("vision_rows")
    if not rows:
        return None
    try:
        return rows_from_json(rows), dict(payload.get("vision_hints") or {})
    except (KeyError, TypeError, ValueError) as exc:
        # a malformed cache is a miss, never an error -- worst case we
        # pay for the read again
        logger.warning("vision_cache_unreadable", error=str(exc))
        return None


async def _cache_vision(attachment_id: uuid.UUID, vision: Any, ctx: RequestContext) -> None:
    from backend.ocr.vision_engine import rows_to_json

    async with ctx.session_factory() as session, session.begin():
        attachment = await session.get(Attachment, attachment_id)
        if attachment is None:
            return
        attachment.ocr_result = {
            **(attachment.ocr_result or {}),
            "vision_rows": rows_to_json(vision.rows),
            "vision_hints": {
                "supplier": vision.supplier_name,
                "invoice": vision.invoice_no,
                "date": vision.invoice_date,
            },
        }


async def process_purchase_photo(
    data: bytes, mime_type: str, media_id: str | None, ctx: RequestContext
) -> CommandResult:
    """Store the photo and ask what it is. OCR does *not* run yet.

    Asking first means a mis-sent picture never spends a vision call,
    and the extraction can be told what it is reading
    (docs/20_ConversationalIntake.md §2). The bytes are already safely
    on disk as an attachment, so the answer can arrive minutes later.
    """
    org_id = ctx.user.org_id
    async with ctx.session_factory() as session:
        service = OcrService(session)
        sha256_hash = service.hash_bytes(data)
        async with session.begin():
            duplicate = await service.find_duplicate_photo(org_id, sha256_hash)
            if duplicate is not None:
                return CommandResult(
                    reply=f"📎 You already sent this exact photo on "
                    f"{fmt_date(duplicate.created_at.date())}. Send a different photo, "
                    "or use the 'purchase' command to enter it manually."
                )
            attachment = await service.store_attachment(
                ctx.user, data=data, mime_type=mime_type, whatsapp_media_id=media_id
            )
            attachment_id = attachment.id

    await SessionService(ctx.session_factory).set(
        org_id, ctx.user.id, AWAITING_INTENT, {"attachment_id": str(attachment_id)}
    )
    return ask_intent()


async def read_stored_sheet(attachment_id_text: str, ctx: RequestContext) -> CommandResult:
    """Now that we know it's a purchase, read it -- then ask only for
    what the sheet didn't carry."""
    org_id = ctx.user.org_id
    try:
        attachment_id = uuid.UUID(attachment_id_text)
    except ValueError:
        return CommandResult(reply="That photo has expired — please send it again.")

    async with ctx.session_factory() as session:
        service = OcrService(session)
        attachment = await session.get(Attachment, attachment_id)
        if attachment is None or attachment.org_id != org_id:
            return CommandResult(reply="That photo has expired — please send it again.")
        mappings = await service.resolve_template(org_id)
        file_path, mime_type = attachment.file_path, attachment.mime_type
    if not mappings:
        return CommandResult(
            reply="No OCR template is configured for this business yet — "
            "use the 'purchase' command to enter this manually."
        )
    try:
        data = Path(file_path).read_bytes()
    except OSError:
        return CommandResult(reply="I can't find that photo any more — please send it again.")

    await ctx.say("📸 Reading your sheet, one moment…")

    import asyncio

    from backend.core.config import get_settings
    from backend.ocr.extract import ExtractionResult

    settings = get_settings()
    sheet: ParsedSheet | None = None
    engine_used = "local"
    supplier_hint = ""
    invoice_hint = ""
    date_hint = ""

    # A photo already read is never read again. Tapping "A purchase" a
    # second time, or re-answering after a crash, used to bill a fresh
    # vision call for a sheet whose rows were already known -- which is
    # where an unexplained bill mostly comes from.
    cached = await _cached_vision(attachment_id, ctx)
    if cached is not None:
        rows, hints = cached
        sheet = ParsedSheet(
            extraction=ExtractionResult(
                columns=[],
                rows=rows,
                unmapped_headers=[],
                grid_confidence=1.0,
                engines=["claude-vision:cached"],
            ),
            deskew_angle=0.0,
            cropped=False,
            page_count=1,
        )
        engine_used = "vision-cached"
        supplier_hint = hints.get("supplier", "")
        invoice_hint = hints.get("invoice", "")
        date_hint = hints.get("date", "")
        logger.info("vision_cache_hit", attachment_id=str(attachment_id), rows=len(rows))

    # Vision first: it reads the table as a table, so an unnamed column or a
    # slightly angled photo isn't a silent field shift. Any failure -- no
    # key, refusal, transport -- falls through to the local pipeline.
    if sheet is None and settings.ocr_use_vision:
        reader = VisionSheetReader()
        if reader.available():
            try:
                vision = await asyncio.to_thread(reader.read_sheet, data, mime_type)
                if vision.rows:
                    await _cache_vision(attachment_id, vision, ctx)
                    sheet = ParsedSheet(
                        extraction=ExtractionResult(
                            columns=[],
                            rows=vision.rows,
                            unmapped_headers=[],
                            grid_confidence=1.0,
                            engines=[f"claude-vision:{vision.model}"],
                        ),
                        deskew_angle=0.0,
                        cropped=False,
                        page_count=1,
                    )
                    engine_used = "vision"
                    supplier_hint = vision.supplier_name
                    invoice_hint = vision.invoice_no
                    date_hint = vision.invoice_date
            except VisionUnavailableError as exc:
                logger.warning("vision_read_failed_falling_back", error=str(exc))

    try:
        if sheet is None:
            sheet = await asyncio.to_thread(
                parse_sheet, data, mappings, build_engine(settings.ocr_primary_engine)
            )
    except OcrFailure as exc:
        async with ctx.session_factory() as session, session.begin():
            await OcrService(session).mark_attachment(attachment_id, "failed")
        return CommandResult(
            reply=f"❌ {exc}. Want to enter it manually? Use the 'purchase' command "
            "(send 'help purchase' for the format)."
        )
    except Exception as exc:  # noqa: BLE001 -- a crash must not leave the user waiting
        logger.error("ocr_pipeline_crashed", error=str(exc), attachment_id=str(attachment_id))
        async with ctx.session_factory() as session, session.begin():
            await OcrService(session).mark_attachment(attachment_id, "failed")
        return CommandResult(
            reply="❌ Something went wrong reading that image. Use the 'purchase' "
            "command to enter it manually."
        )

    assert sheet is not None  # set by vision or parse_sheet, else we returned above
    async with ctx.session_factory() as session:
        service = OcrService(session)
        async with session.begin():
            build = await service.build_draft(
                org_id,
                sheet,
                supplier_name=supplier_hint,
                invoice_no=invoice_hint,
            )
            build.draft.source_attachment_id = attachment_id
            await service.mark_attachment(
                attachment_id,
                "processed",
                ocr_result={
                    "rows": len(sheet.extraction.rows),
                    "columns": [c.field for c in sheet.extraction.columns],
                    "grid_confidence": sheet.extraction.grid_confidence,
                    "engines": sheet.extraction.engines,
                    "engine_used": engine_used,
                },
            )

    # A Draft always carries *some* date (it defaults to today), so
    # "did the sheet state one?" can only be answered here, from the hint.
    date_known = apply_date_hint(build.draft, date_hint)

    # gap analysis, then one question per gap -- instead of a template
    # the partner has to memorise (docs/20 §2)
    queue = missing_slots(build.draft, date_known=date_known)
    detail = render_ocr_result(
        build.draft,
        low_confidence=build.low_confidence_notes,
        auto_corrections=build.auto_corrections,
        unmapped_headers=build.unmapped_headers,
        hard_to_read=build.hard_to_read,
        page_count=sheet.page_count,
    )
    started = await begin_slots(build.draft, queue, ctx)
    return dataclasses.replace(started, reply=f"{detail}\n\n{started.reply}")
