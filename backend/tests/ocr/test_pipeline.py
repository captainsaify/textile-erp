"""OCR pipeline against synthetic sheets -- docs/07_OCR.md §14 treats
accuracy as a testable contract, not a judgment call.

Runs on Tesseract (always available in CI); the Paddle path shares the
same DualEngine contract and is exercised by test_engines.
"""

from __future__ import annotations

import pytest

from backend.ocr.engines import DualEngine, TesseractEngine, normalize_numeric
from backend.ocr.extract import ColumnMapping, resolve_columns, score_cell
from backend.ocr.pipeline import OcrFailure, parse_sheet
from backend.ocr.preprocess import prepare
from backend.ocr.table_detect import TableDetectionError, detect
from backend.tests.ocr.fixtures import rotated_sheet_bytes, sheet_bytes

# mirrors the seeded textile template (docs/07_OCR.md §5)
TEXTILE_MAPPINGS = [
    ColumnMapping(field="ignore", header_aliases=["s.no", "sno", "sr.no", "#"]),
    ColumnMapping(field="qty", header_aliases=["qty", "quantity", "qnty"]),
    ColumnMapping(
        field="description", header_aliases=["description", "desc", "item", "particulars"]
    ),
    ColumnMapping(field="code", header_aliases=["code", "item code", "design"]),
    ColumnMapping(field="ignore", header_aliases=["label"]),
    ColumnMapping(field="weight_kg", header_aliases=["kg", "wt", "weight"]),
    ColumnMapping(
        field="total_weight_kg", header_aliases=["t.kg", "total kg", "tot kg", "total weight"]
    ),
    ColumnMapping(field="ignore", header_aliases=["total", "amount", "value"]),
]


@pytest.fixture(scope="module")
def engine() -> DualEngine:
    tesseract = TesseractEngine()
    if not tesseract.available():
        pytest.skip("tesseract not installed")
    return DualEngine(primary=tesseract, fallback=None)


def test_normalize_numeric_repairs_digit_confusions() -> None:
    assert normalize_numeric("l00") == "100"
    assert normalize_numeric("1O0") == "100"
    assert normalize_numeric("1,234.50") == "1234.50"
    assert normalize_numeric("12,5") == "12.5"
    assert normalize_numeric("100 KG") == "100"
    assert normalize_numeric("") == ""


def test_score_cell_uses_documented_weights() -> None:
    # numeric: 0.7 engine + 0.3 grid
    assert score_cell(1.0, 1.0, None) == 1.0
    assert score_cell(0.5, 1.0, None) == 0.65
    # text: 0.5 engine + 0.2 grid + 0.3 match
    assert score_cell(1.0, 1.0, 1.0) == 1.0
    assert score_cell(0.8, 0.9, 1.0) == pytest.approx(0.88, abs=1e-9)


def test_resolve_columns_maps_headers_and_surfaces_unknown() -> None:
    columns, unmapped = resolve_columns(
        ["S.No", "Qty", "Description", "Code", "KG", "T.KG", "Rate"], TEXTILE_MAPPINGS
    )
    by_index = {column.index: column.field for column in columns}
    assert by_index[0] == "ignore"
    assert by_index[1] == "qty"
    assert by_index[2] == "description"
    assert by_index[3] == "code"
    assert by_index[4] == "weight_kg"
    assert by_index[5] == "total_weight_kg"
    assert unmapped == ["Rate"]  # never silently dropped (§5)


def test_ruled_grid_detected() -> None:
    prepared = prepare(sheet_bytes(), denoise=False)
    grid = detect(prepared.binary)
    assert grid.strategy == "ruled"
    assert grid.row_count >= 4  # header + 3 items
    assert grid.column_count >= 5


def test_blank_image_reports_failure() -> None:
    import cv2
    import numpy as np

    blank = np.full((400, 600), 255, dtype=np.uint8)
    ok, buffer = cv2.imencode(".png", blank)
    assert ok
    prepared = prepare(bytes(buffer), denoise=False)
    with pytest.raises(TableDetectionError):
        detect(prepared.binary)
    with pytest.raises(OcrFailure, match="couldn't find a purchase table"):
        parse_sheet(bytes(buffer), TEXTILE_MAPPINGS)


def test_deskew_corrects_rotation() -> None:
    prepared = prepare(rotated_sheet_bytes(3.0), denoise=False)
    assert abs(prepared.deskew_angle) > 0.5  # detected and corrected something


def test_parse_sheet_reads_codes_and_quantities(engine: DualEngine) -> None:
    sheet = parse_sheet(sheet_bytes(), TEXTILE_MAPPINGS, engine)
    fields = {column.field for column in sheet.extraction.columns}
    assert {"qty", "code"} <= fields

    codes = [
        row.fields["code"].text.upper()
        for row in sheet.extraction.rows
        if "code" in row.fields and row.fields["code"].text
    ]
    assert "TRP" in codes, f"expected TRP among {codes}"
    assert "MJP" in codes, f"expected MJP among {codes}"

    quantities = [row.fields["qty"].text for row in sheet.extraction.rows if "qty" in row.fields]
    assert "100" in quantities, f"expected 100 among {quantities}"


def test_confidence_flags_are_populated(engine: DualEngine) -> None:
    sheet = parse_sheet(sheet_bytes(), TEXTILE_MAPPINGS, engine)
    for row in sheet.extraction.rows:
        for field in row.fields.values():
            assert 0.0 <= field.confidence <= 1.0
    # a clean synthetic sheet should not be classed hard-to-read
    assert not sheet.extraction.hard_to_read
