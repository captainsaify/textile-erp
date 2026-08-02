"""OCR purchase path end to end: photo -> draft -> details -> CONFIRM,
photo-hash duplicate detection, and the learning dictionary
(docs/07_OCR.md §8, §11)."""

from __future__ import annotations

import datetime
import decimal
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.command_types import CommandResult, RequestContext
from backend.api.commands.intake_commands import handle_intent_reply, handle_slot_reply
from backend.api.commands.ocr_commands import (
    apply_details,
    process_purchase_photo,
)
from backend.api.commands.purchase_commands import handle_purchase_session_reply
from backend.core.exceptions import ValidationError
from backend.models import User
from backend.ocr.engines import TesseractEngine
from backend.services.ocr_service import OcrService
from backend.services.purchase_service import Draft, DraftLine
from backend.services.session_service import SessionService
from backend.tests.conftest import SEEDED_ORG_ID, purge_business_rows
from backend.tests.ocr.fixtures import sheet_bytes

D = decimal.Decimal
ORG = uuid.UUID(SEEDED_ORG_ID)


@pytest.fixture(autouse=True)
async def clean(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    if not TesseractEngine().available():
        pytest.skip("tesseract not installed")
    yield
    await purge_business_rows(session_factory)


@pytest.fixture
def ctx(staff_user: User, session_factory: async_sessionmaker[AsyncSession]) -> RequestContext:
    return RequestContext(user=staff_user, session_factory=session_factory, message_id="m1")


@pytest.fixture(autouse=True)
def tesseract_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic and fast; the Paddle path shares the DualEngine
    contract covered in backend/tests/ocr."""
    from backend.core.config import get_settings

    monkeypatch.setenv("OCR_PRIMARY_ENGINE", "tesseract")
    get_settings.cache_clear()


@pytest.fixture
def attachments_dir(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.core.config import get_settings

    monkeypatch.setenv("ATTACHMENTS_DIR", str(tmp_path))
    get_settings.cache_clear()


async def _photo(data: bytes, media_id: str, ctx: RequestContext) -> CommandResult:
    """Send a photo and answer the intent question with 'a purchase' --
    OCR only runs once that is answered (docs/20 §2)."""
    stored = await process_purchase_photo(data, "image/jpeg", media_id, ctx)
    assert "What is it?" in stored.reply
    state = await SessionService(ctx.session_factory).get(ctx.user.org_id, ctx.user.id)
    return await handle_intent_reply("intake purchase", ctx, state)


async def _slot_reply(text: str, ctx: RequestContext) -> CommandResult:
    state = await SessionService(ctx.session_factory).get(ctx.user.org_id, ctx.user.id)
    return await handle_slot_reply(text, ctx, state)


async def _session_reply(text: str, ctx: RequestContext) -> CommandResult:
    state = await SessionService(ctx.session_factory).get(ctx.user.org_id, ctx.user.id)
    return await handle_purchase_session_reply(text, ctx, state)


def test_apply_details_parses_manual_fields() -> None:
    draft = Draft(
        supplier_id=None,
        supplier_name="",
        invoice_no="",
        invoice_date=__import__("datetime").date(2026, 1, 1),
        brand_id=None,
        brand_name=None,
        lines=[
            DraftLine(
                code="TRP",
                qty=D("100"),
                rate=D("0"),
                product_id=None,
                resolved_code=None,
                unit_code=None,
            )
        ],
        freight=D("0"),
        other_charges=D("0"),
        declared_total=None,
    )
    updated = apply_details(
        draft,
        "Supplier: Shree Textiles Invoice: INV-9 Date: 24-07-2026 Rate: 150 "
        "Freight: 500 Other: 100 Total: 15600",
    )
    assert updated.supplier_name == "Shree Textiles"
    assert updated.invoice_no == "INV-9"
    assert updated.invoice_date.isoformat() == "2026-07-24"
    assert updated.lines[0].rate == D("150")
    assert updated.freight == D("500")
    assert updated.other_charges == D("100")
    assert updated.declared_total == D("15600")

    with pytest.raises(ValidationError):
        apply_details(draft, "nonsense")


async def test_photo_to_confirmed_purchase(
    ctx: RequestContext,
    attachments_dir: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    result = await _photo(sheet_bytes(), "wamid.1", ctx)
    assert "📸 Read 3 items from your sheet:" in result.reply
    assert "TRP" in result.reply and "MJP" in result.reply
    assert "100.0" in result.reply
    # the wizard asks rather than printing a template to memorise
    assert "details Supplier:" not in result.reply
    assert "Which supplier is this from?" in result.reply

    async with session_factory() as session:
        status, mime = (
            await session.execute(sa.text("SELECT status, mime_type FROM attachments"))
        ).one()
        assert status == "processed"
        assert mime == "image/jpeg"

    # the sheet has no supplier/invoice/rate -- `details` answers every
    # remaining slot in one message instead of one at a time
    result = await _slot_reply(
        "details Supplier: Shree Textiles Invoice: INV-4521 Date: 24-07-2026 "
        "Rate: 150 Freight: 500",
        ctx,
    )
    assert "Purchase draft ready" in result.reply
    assert "Supplier 'Shree Textiles' isn't in your list yet" in result.reply

    await _session_reply("create supplier", ctx)
    for code, description in (
        ("TRP", "Trouser Poly"),
        ("MJP", "Jogging Fabric"),
        ("CTW", "Cotton Twill"),
    ):
        await _session_reply(f"create product {code} {description}", ctx)

    result = await _session_reply("confirm", ctx)
    assert "✅ Purchase confirmed — Shree Textiles, INV-4521" in result.reply

    async with session_factory() as session:
        lines = (
            await session.execute(
                sa.text(
                    "SELECT p.code, l.qty, l.rate FROM purchase_lines l "
                    "JOIN products p ON p.id = l.product_id ORDER BY l.line_no"
                )
            )
        ).all()
        # quantities are the sheet's TOTAL KG, not its piece count:
        # CTW is 25 rolls x 2 kg = 50 kg (docs/04_Purchases.md §12)
        assert [(r.code, r.qty, r.rate) for r in lines] == [
            ("TRP", D("100.000"), D("150.0000")),
            ("MJP", D("40.000"), D("150.0000")),
            ("CTW", D("50.000"), D("150.0000")),
        ]
        movements = (
            await session.execute(sa.text("SELECT count(*) FROM inventory_movements"))
        ).scalar_one()
        assert movements == 3


async def test_unreadable_image_offers_manual_entry(
    ctx: RequestContext, attachments_dir: None
) -> None:
    import cv2
    import numpy as np

    ok, buffer = cv2.imencode(".png", np.full((300, 400), 255, dtype=np.uint8))
    assert ok
    result = await _photo(bytes(buffer), "wamid.3", ctx)
    assert "couldn't find a purchase table" in result.reply
    assert "'purchase' command" in result.reply


async def test_learning_dictionary_records_and_applies_corrections(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        await OcrService(session).record_correction(
            ORG, field="code", raw_ocr_text="TRD", corrected_value="TRP"
        )

    async with session_factory() as session:
        service = OcrService(session)
        assert await service.lookup_correction(ORG, "code", "TRD", None) == "TRP"
        # case-insensitive raw lookup
        assert await service.lookup_correction(ORG, "code", "trd", None) == "TRP"
        assert await service.lookup_correction(ORG, "code", "XYZ", None) is None

    async with session_factory() as session, session.begin():
        await OcrService(session).record_correction(
            ORG, field="code", raw_ocr_text="TRD", corrected_value="TRP"
        )

    async with session_factory() as session:
        hits = (
            await session.execute(
                sa.text("SELECT hit_count FROM ocr_learning_dictionary WHERE raw_ocr_text = 'TRD'")
            )
        ).scalar_one()
        assert hits == 2  # incremented, not duplicated


async def test_template_resolution_returns_seeded_textile_mapping(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        mappings = await OcrService(session).resolve_template(ORG)
    fields = {mapping.field for mapping in mappings}
    assert {"qty", "description", "code", "weight_kg", "total_weight_kg", "ignore"} <= fields


async def test_abandoned_photo_can_be_resent(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A photo read into a draft that was never confirmed entered nothing,
    so re-sending it must work. Blocking it left the user with no way
    forward but typing the whole invoice by hand — which is exactly what
    happened in the field."""
    if not TesseractEngine().available():
        pytest.skip("tesseract not installed")
    data = sheet_bytes()

    first = await process_purchase_photo(data, "image/jpeg", "m-1", ctx)
    assert "already sent" not in first.reply

    # no purchase was confirmed, so the same bytes are still fair game
    second = await process_purchase_photo(data, "image/jpeg", "m-2", ctx)
    assert "already sent" not in second.reply, "an abandoned draft must not block a retry"


async def test_photo_is_blocked_once_it_became_a_purchase(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The check still does its real job: stopping the same invoice being
    entered twice."""
    import sqlalchemy as sa

    from backend.models import Attachment, PurchaseHeader, Supplier
    from backend.models.enums import PurchaseStatus
    from backend.tests.conftest import SEEDED_MAIN_WAREHOUSE_ID

    if not TesseractEngine().available():
        pytest.skip("tesseract not installed")
    data = sheet_bytes()
    await process_purchase_photo(data, "image/jpeg", "m-1", ctx)

    async with session_factory() as session, session.begin():
        attachment_id = (await session.execute(sa.select(Attachment.id).limit(1))).scalar_one()
        supplier = Supplier(org_id=ORG, name="Dup Test Co", created_by=ctx.user.id)
        session.add(supplier)
        await session.flush()
        session.add(
            PurchaseHeader(
                org_id=ORG,
                supplier_id=supplier.id,
                warehouse_id=uuid.UUID(SEEDED_MAIN_WAREHOUSE_ID),
                invoice_no="DUP-1",
                invoice_date=datetime.date.today(),
                grand_total=decimal.Decimal("100"),
                status=PurchaseStatus.CONFIRMED,
                ocr_source_attachment_id=attachment_id,
                created_by=ctx.user.id,
            )
        )

    again = await process_purchase_photo(data, "image/jpeg", "m-3", ctx)
    assert "already sent this exact photo" in again.reply
