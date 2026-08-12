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
    q = OcrService._costing_quantity(row(qty="10", weight_kg="8.2", total_weight_kg="82"))
    assert q.costing == D("82")  # KG drives costing, not the 10 rolls
    assert q.pieces == D("10")
    assert q.per_unit == D("8.2")
    assert q.mismatch is None


def test_costing_quantity_multiplies_when_total_missing() -> None:
    q = OcrService._costing_quantity(row(qty="4", weight_kg="2.5"))
    assert q.costing == D("10.0")
    assert q.pieces == D("4")
    assert q.per_unit == D("2.5")


def test_costing_quantity_falls_back_to_pieces() -> None:
    q = OcrService._costing_quantity(row(qty="7"))
    assert q.costing == D("7")
    assert q.pieces == D("7")
    assert q.per_unit is None


def test_totals_row_of_bare_rules_is_dropped() -> None:
    # the real sheet's grand-total line OCR'd as pipes in every text cell
    assert OcrService._is_noise_row(row(code="|", description="||", total_weight_kg="27280"))


def test_disagreeing_total_is_flagged_not_overruled() -> None:
    """Preferring the computed value here silently replaced correct
    totals on a real sheet (a 1520 became 28800). Which cell misread is
    not decidable from the numbers, so the sheet's own figure stands and
    the user is asked."""
    q = OcrService._costing_quantity(row(qty="82", weight_kg="90", total_weight_kg="2"))
    assert q.costing == D("2")  # what the sheet states
    assert q.mismatch is not None
    assert "7380" in q.mismatch  # and what qty x kg would make it


def test_stated_total_kept_when_consistent() -> None:
    # rounding slack within tolerance leaves the sheet's own figure alone
    q = OcrService._costing_quantity(row(qty="3", weight_kg="10.1", total_weight_kg="30.3"))
    assert q.costing == D("30.3")
    assert q.mismatch is None


# --------------------------------------------------------------------
# which column is the brand
# --------------------------------------------------------------------


def test_the_brand_is_the_label_that_repeats() -> None:
    assert (
        OcrService._dominant_label(
            [row(code="35A", label="TOP"), row(code="22D", label="TOP"), row(code="CPK", label="")]
        )
        == "TOP"
    )


def test_a_fold_column_is_not_a_brand() -> None:
    """A real sheet had CODE | ITEM | FOLD | QTY | KG | WEIGHT, with an
    F on every row. Every row's brand came back "F", which made all 26
    codes collide with the same codes under their real brand and offered
    to create 26 duplicate products. A code is only unique within a
    brand, so this is not a cosmetic misread."""
    folded = [row(code="028", label="F"), row(code="55CT", label="F"), row(code="55M", label="F")]
    assert OcrService._dominant_label(folded) == ""

    for marker in ("FOLD", "fold", "Roll", "OPEN", "Tube"):
        assert OcrService._dominant_label([row(code="X", label=marker)]) == "", marker


def test_a_real_brand_still_wins_over_fold_noise() -> None:
    """Rejecting fold markers must not reject the brand beside them."""
    mixed = [
        row(code="35A", label="MKD"),
        row(code="22D", label="F"),
        row(code="CPK", label="MKD"),
    ]
    assert OcrService._dominant_label(mixed) == "MKD"


def test_charges_read_off_a_sheet_are_kept_and_cleaned() -> None:
    """GST and packing at the foot of a bill, or in their own columns.

    Iqbal Bhai's 1051 carried 'BPK+ 2100' and 'GST 2240' under the items
    and the intake never looked at them, so they were entered afterwards
    as operating expenses -- money owed to a supplier, filed where money
    leaves, and the goods costing less than they did.
    """
    booked = OcrService._charges_from_sheet(
        [
            ("gst", "2,240"),
            ("BPK", "2100"),
            ("packing", "0"),  # nothing charged: not a charge
            ("labour", "n/a"),  # not a number
            ("", "500"),  # no label to bill it under
        ]
    )
    assert booked == {"GST": D("2240"), "BPK": D("2100")}


def test_a_repeated_charge_column_is_counted_once() -> None:
    """A charge in a column repeats on every row and still belongs to the
    bill once. Summing the column would multiply the tax by the number of
    items -- on a two-line sheet, exactly double."""
    from backend.ocr.vision_engine import _charges_from

    assert _charges_from(
        [
            {"label": "GST", "amount": "2240"},
            {"label": "GST", "amount": "2240"},
            {"label": "BPK", "amount": "2100"},
        ]
    ) == [("GST", "2240"), ("BPK", "2100")]

    # A label with no usable number never becomes a charge of zero.
    assert _charges_from([{"label": "GST", "amount": ""}]) == []
    assert _charges_from([{"label": "GST", "amount": "-5"}]) == []
    assert _charges_from("not a list") == []
