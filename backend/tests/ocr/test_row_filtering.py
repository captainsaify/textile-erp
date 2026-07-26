"""Noise-row rejection and weight-based costing quantity.

Driven by a real sheet: the header band and a grand-total line were being
returned as purchase lines, and quantity was taken as the piece count on
a sheet costed per KG.
"""

from __future__ import annotations

import decimal

from backend.ocr.extract import ExtractedField, ExtractedRow
from backend.services.ocr_service import OcrService

D = decimal.Decimal


def row(**cells: str) -> ExtractedRow:
    return ExtractedRow(
        row_index=0,
        fields={
            name: ExtractedField(field=name, text=value, confidence=0.95, engine="test")
            for name, value in cells.items()
        },
    )


def test_real_item_row_is_kept() -> None:
    assert not OcrService._is_noise_row(
        row(code="TRP", description="Trouser Poly", qty="10", weight_kg="8.2", total_weight_kg="82")
    )


def test_header_band_read_as_data_is_dropped() -> None:
    assert OcrService._is_noise_row(
        row(code="Code", description="Description", qty="Qty", total_weight_kg="T.KG")
    )


def test_grand_total_row_is_dropped() -> None:
    assert OcrService._is_noise_row(row(code="", description="Total", total_weight_kg="322"))
    assert OcrService._is_noise_row(row(code="", description="Grand Total", qty="322"))
    # the real sheet's totals line came through as punctuation + a number
    assert OcrService._is_noise_row(row(code="|", description="", total_weight_kg="322"))


def test_blank_filler_rows_are_dropped() -> None:
    assert OcrService._is_noise_row(row(code="", description="", qty=""))
    assert OcrService._is_noise_row(row(code=".", description="", qty=""))


def test_costing_quantity_prefers_total_kg() -> None:
    qty, pieces, per_unit = OcrService._costing_quantity(
        row(qty="10", weight_kg="8.2", total_weight_kg="82")
    )
    assert qty == D("82")  # KG drives costing, not the 10 rolls
    assert pieces == D("10")
    assert per_unit == D("8.2")


def test_costing_quantity_multiplies_when_total_missing() -> None:
    qty, pieces, per_unit = OcrService._costing_quantity(row(qty="4", weight_kg="2.5"))
    assert qty == D("10.0")
    assert pieces == D("4")
    assert per_unit == D("2.5")


def test_costing_quantity_falls_back_to_pieces() -> None:
    qty, pieces, per_unit = OcrService._costing_quantity(row(qty="7"))
    assert qty == D("7")
    assert pieces == D("7")
    assert per_unit is None
