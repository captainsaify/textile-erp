"""End-to-end against the real supplier sheet's layout.

The field failure this guards: an unnamed column between DESCRIPTION and
CODE shifted every field one place right, so codes became "FOLD" and the
real descriptions were lost.
"""

from __future__ import annotations

import pytest

from backend.ocr.engines import DualEngine, TesseractEngine
from backend.ocr.pipeline import parse_sheet
from backend.tests.ocr.fixtures import wagdia_sheet_bytes
from backend.tests.ocr.test_pipeline import TEXTILE_MAPPINGS


@pytest.fixture(scope="module")
def engine() -> DualEngine:
    tesseract = TesseractEngine()
    if not tesseract.available():
        pytest.skip("tesseract not installed")
    return DualEngine(primary=tesseract, fallback=None)


def test_unnamed_column_does_not_shift_the_mapping(engine: DualEngine) -> None:
    sheet = parse_sheet(wagdia_sheet_bytes(), TEXTILE_MAPPINGS, engine)
    fields = {column.field for column in sheet.extraction.columns}
    assert {"qty", "description", "code", "weight_kg", "total_weight_kg"} <= fields

    codes = [r.fields["code"].text.upper() for r in sheet.extraction.rows if r.fields.get("code")]
    assert "FOLD" not in codes, f"unnamed column leaked into code: {codes}"
    assert any("35A" in c for c in codes), codes
    assert any("TRP" in c for c in codes), codes

    descriptions = [
        r.fields["description"].text for r in sheet.extraction.rows if r.fields.get("description")
    ]
    assert not any(d.strip().upper() == "FOLD" for d in descriptions), descriptions
    assert any("Zipper" in d for d in descriptions), descriptions
