"""A photo is read once, and paid for once.

`read_stored_sheet` called the API unconditionally, so tapping "A
purchase" a second time -- or re-answering after a crash -- billed a
fresh vision call for a sheet whose rows were already known. Over a
debugging session that is most of an unexplained bill.
"""

from __future__ import annotations

import decimal

from backend.ocr.extract import ExtractedField, ExtractedRow
from backend.ocr.vision_engine import estimate_cost, rows_from_json, rows_to_json

D = decimal.Decimal


def _rows() -> list[ExtractedRow]:
    return [
        ExtractedRow(
            row_index=0,
            fields={
                "code": ExtractedField(
                    field="code", text="35A", confidence=0.95, engine="claude-vision"
                ),
                "qty": ExtractedField(
                    field="qty", text="800", confidence=0.9, engine="claude-vision"
                ),
                "label": ExtractedField(
                    field="label", text="TOP", confidence=0.9, engine="claude-vision"
                ),
            },
        )
    ]


def test_an_extraction_survives_the_round_trip_exactly() -> None:
    """A cache that returns something subtly different from what was
    read is worse than no cache -- the second read would post different
    numbers from the first."""
    original = _rows()

    restored = rows_from_json(rows_to_json(original))

    assert len(restored) == len(original)
    assert restored[0].row_index == 0
    assert {name: field.text for name, field in restored[0].fields.items()} == {
        "code": "35A",
        "qty": "800",
        "label": "TOP",
    }
    # confidence survives, because low-confidence cells are what get
    # flagged for review
    assert restored[0].fields["qty"].confidence == 0.9


def test_a_malformed_cache_is_a_miss_not_a_crash() -> None:
    """Worst case is paying for the read again, which is much better
    than a draft that can't be produced at all."""
    import pytest

    with pytest.raises((KeyError, TypeError, ValueError)):
        rows_from_json([{"nonsense": True}])


def test_the_cost_of_a_read_is_estimated_for_the_log() -> None:
    """A per-sheet price is the only way anyone notices the bill
    drifting before the month ends."""
    opus = estimate_cost("claude-opus-5", 7000, 3000)
    haiku = estimate_cost("claude-haiku-4-5", 7000, 3000)

    assert opus > haiku
    assert round(opus / haiku, 1) == 5.0, "haiku is a fifth the price for the same tokens"
    # an unknown model is reported as zero rather than guessed at
    assert estimate_cost("something-else", 7000, 3000) == 0.0
    assert estimate_cost("claude-opus-5", None, None) == 0.0
