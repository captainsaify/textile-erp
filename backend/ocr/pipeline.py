"""Pipeline orchestration -- docs/07_OCR.md §1. Bytes in, parsed sheet
out; no DB, no framework. Failures are reported as typed outcomes so
the caller can route to manual entry (docs/04_Purchases.md §2)."""

from __future__ import annotations

import dataclasses

from backend.core.logging import get_logger
from backend.ocr import preprocess
from backend.ocr.engines import DualEngine
from backend.ocr.extract import ColumnMapping, ExtractionResult, extract
from backend.ocr.table_detect import TableDetectionError, detect

logger = get_logger(__name__)

PDF_MAGIC = b"%PDF-"


@dataclasses.dataclass(frozen=True)
class ParsedSheet:
    extraction: ExtractionResult
    deskew_angle: float
    cropped: bool
    page_count: int


class OcrFailure(Exception):
    """No purchase table found -- offer manual entry, don't force it
    (docs/07_OCR.md §12)."""


def parse_sheet(
    data: bytes,
    mappings: list[ColumnMapping],
    engine: DualEngine | None = None,
) -> ParsedSheet:
    engine = engine or DualEngine()

    page_count = 1
    if data[: len(PDF_MAGIC)] == PDF_MAGIC:
        pages = preprocess.render_pdf_pages(data)
        if not pages:
            raise OcrFailure("that PDF has no pages I can read")
        page_count = len(pages)
        data = pages[0]  # §12: page 1 becomes this draft; others are separate

    prepared = preprocess.prepare(data)
    try:
        grid = detect(prepared.binary)
    except TableDetectionError as exc:
        raise OcrFailure("I couldn't find a purchase table in this image") from exc

    extraction = extract(grid, prepared.gray, mappings, engine)
    if not extraction.rows:
        raise OcrFailure("I found a table but no item rows in it")

    logger.info(
        "ocr_sheet_parsed",
        rows=len(extraction.rows),
        columns=len(extraction.columns),
        strategy=grid.strategy,
        grid_confidence=grid.confidence,
        manual_ratio=round(extraction.manual_field_ratio, 3),
        engines=extraction.engines,
    )
    return ParsedSheet(
        extraction=extraction,
        deskew_angle=prepared.deskew_angle,
        cropped=prepared.cropped,
        page_count=page_count,
    )
