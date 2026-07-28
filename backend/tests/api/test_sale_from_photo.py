"""Reading a sales note from a photo -- docs/20_ConversationalIntake.md.

Tapping "A sale" used to answer "I can't read a sales sheet yet". That
was a missing feature, not a limit of the reader: the same vision model
handles a handwritten, rotated note perfectly well -- it had simply only
ever been pointed at purchase sheets.

These tests use a stub reader. What they pin is everything after the
read: that the transcription becomes the same SaleDraft the typed
command produces, and that arithmetic which disagrees is surfaced rather
than quietly resolved.
"""

from __future__ import annotations

import decimal
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.command_types import RequestContext
from backend.models import Attachment, User
from backend.ocr.vision_engine import VisionSaleRow, VisionSaleSheet
from backend.services.session_service import AWAITING_SALE_CONFIRMATION, SessionService
from backend.tests.conftest import SEEDED_ORG_ID, purge_business_rows

D = decimal.Decimal
ORG = uuid.UUID(SEEDED_ORG_ID)


@pytest.fixture(autouse=True)
async def clean(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    yield
    await purge_business_rows(session_factory)


@pytest.fixture
def ctx(staff_user: User, session_factory: async_sessionmaker[AsyncSession]) -> RequestContext:
    return RequestContext(user=staff_user, session_factory=session_factory, message_id="m1")


class StubReader:
    """Stands in for the vision call; the network is not under test."""

    def __init__(self, sheet: VisionSaleSheet) -> None:
        self.sheet = sheet

    def available(self) -> bool:
        return True

    def read_sale_sheet(self, data: bytes, mime_type: str = "image/jpeg") -> VisionSaleSheet:
        return self.sheet


async def _stored_photo(
    session_factory: async_sessionmaker[AsyncSession], user: User, tmp_path: Path
) -> str:
    image = tmp_path / "note.jpg"
    image.write_bytes(b"not really a jpeg")
    async with session_factory() as session:
        attachment = Attachment(
            org_id=ORG,
            file_path=str(image),
            mime_type="image/jpeg",
            sha256_hash=uuid.uuid4().hex,
            file_size_bytes=image.stat().st_size,
            created_by=user.id,
        )
        session.add(attachment)
        await session.commit()
        return str(attachment.id)


def _sheet(rows: list[VisionSaleRow], **kwargs: Any) -> VisionSaleSheet:
    return VisionSaleSheet(
        rows=rows,
        customer_name=kwargs.get("customer_name", "Wagdia Textile"),
        sale_date=kwargs.get("sale_date", "29/07/26"),
        declared_total=kwargs.get("declared_total", ""),
        unreadable_note=kwargs.get("unreadable_note", ""),
        model="stub",
    )


async def _read(
    ctx: RequestContext,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    sheet: VisionSaleSheet,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    from backend.api.commands import sale_commands

    attachment_id = await _stored_photo(session_factory, ctx.user, tmp_path)
    monkeypatch.setattr(
        "backend.ocr.vision_engine.VisionSheetReader", lambda *a, **k: StubReader(sheet)
    )
    return await sale_commands.read_stored_sale_sheet(attachment_id, ctx)


async def test_a_photographed_note_becomes_a_sale_draft(
    ctx: RequestContext,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real sheet: two handwritten lines, a party name and a date."""
    result = await _read(
        ctx,
        session_factory,
        tmp_path,
        _sheet(
            [
                VisionSaleRow(code="35A", description="", qty="800", rate="200", line_total=""),
                VisionSaleRow(code="22D", description="", qty="1000", rate="225", line_total=""),
            ],
            declared_total="265000",
        ),
        monkeypatch,
    )

    assert "Read 2 item(s)" in result.reply
    assert "Wagdia Textile" in result.reply

    state = await SessionService(session_factory).get(ORG, ctx.user.id)
    assert state.state == AWAITING_SALE_CONFIRMATION

    from backend.services.sales_service import SaleDraft

    draft = SaleDraft.from_context(state.context)
    assert [(line.code, line.qty, line.rate) for line in draft.lines] == [
        ("35A", D("800"), D("200")),
        ("22D", D("1000"), D("225")),
    ]


async def test_a_line_whose_arithmetic_disagrees_is_surfaced_not_resolved(
    ctx: RequestContext,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """800 x 200 is 160000, but the sheet says 40000. Which is wrong is
    not something to guess at -- the whole thesis of this system is that
    a disagreement between two sources gets shown (CLAUDE.md)."""
    result = await _read(
        ctx,
        session_factory,
        tmp_path,
        _sheet(
            [VisionSaleRow(code="35A", description="", qty="800", rate="200", line_total="40000")]
        ),
        monkeypatch,
    )

    assert "which is right?" in result.reply.lower()
    assert "40,000" in result.reply
    assert "1,60,000" in result.reply


async def test_rates_written_with_a_trailing_dash_are_read(
    ctx: RequestContext,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """People write '200/-'. Refusing that would be the system insisting
    on its own notation over the one on the paper."""
    await _read(
        ctx,
        session_factory,
        tmp_path,
        _sheet([VisionSaleRow(code="35A", description="", qty="800", rate="200/-", line_total="")]),
        monkeypatch,
    )

    from backend.services.sales_service import SaleDraft

    state = await SessionService(session_factory).get(ORG, ctx.user.id)
    draft = SaleDraft.from_context(state.context)
    assert draft.lines[0].rate == D("200")


async def test_a_note_with_no_readable_rows_says_so(
    ctx: RequestContext,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _read(ctx, session_factory, tmp_path, _sheet([]), monkeypatch)

    assert "couldn't find any item rows" in result.reply
    assert "sale Customer:" in result.reply
