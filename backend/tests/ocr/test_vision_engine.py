# --------------------------------------------------------------------
# the sheet's LABEL column is the brand
# --------------------------------------------------------------------


def test_the_label_column_becomes_the_brand_instead_of_being_discarded() -> None:
    """The partners' sheet has a LABEL column reading TOP on every row.
    The prompt used to name it as noise to ignore, so every product came
    out brandless -- and a code is unique only *within* a brand, so
    discarding it discards the thing that tells two codes apart.
    """
    from backend.ocr import vision_engine

    assert "label" in vision_engine.SHEET_SCHEMA["properties"]["rows"]["items"]["properties"]
    assert "label" in vision_engine.SHEET_SCHEMA["properties"]["rows"]["items"]["required"]
    # and the prompt no longer tells the model to throw it away
    assert "label columns" not in vision_engine.USER_PROMPT
    assert "label/brand column" in vision_engine.USER_PROMPT


def test_the_brand_is_the_most_common_label_not_the_first() -> None:
    """One misread cell must not rename the whole purchase."""
    from backend.ocr.extract import ExtractedField, ExtractedRow
    from backend.services.ocr_service import OcrService

    def row(index: int, label: str) -> ExtractedRow:
        return ExtractedRow(
            row_index=index,
            fields={
                "label": ExtractedField(
                    field="label", text=label, confidence=0.9, engine="claude-vision"
                )
            },
        )

    rows = [row(0, "T0P"), row(1, "TOP"), row(2, "TOP"), row(3, "")]
    assert OcrService._dominant_label(rows) == "TOP"
    assert OcrService._dominant_label([row(0, "")]) == ""


def test_a_sales_note_has_its_own_schema_with_rate_and_a_stated_total() -> None:
    """A purchase sheet carries weights where a sales note carries a rate.
    One prompt covering both is how a rate lands in a weight column."""
    from backend.ocr import vision_engine

    properties = vision_engine.SALE_SCHEMA["properties"]["rows"]["items"]["properties"]
    assert {"code", "qty", "rate", "line_total"} <= set(properties)
    # the model must copy the written total, never compute one
    assert "Never compute a value" in vision_engine.SALE_SYSTEM_PROMPT


def test_the_rate_column_is_captured_when_the_sheet_has_one() -> None:
    """A bill arrived with a Unit Price column and the wizard asked for
    the rate anyway. `rate=ZERO` was hard-coded with a note saying rate
    is never on the sheet -- true of the first sheet this was built
    against, wrong the moment a supplier printed one."""
    from backend.ocr import vision_engine

    properties = vision_engine.SHEET_SCHEMA["properties"]["rows"]["items"]["properties"]
    assert "rate" in properties
    assert "rate" in vision_engine.SHEET_SCHEMA["properties"]["rows"]["items"]["required"]
    # and the prompt explains the "22 x 80 x 75" shorthand these bills use
    assert "22 x 80 x 75" in vision_engine.USER_PROMPT


def test_a_sheet_rate_reaches_the_draft_so_nothing_is_asked() -> None:
    import decimal

    from backend.ocr.extract import ExtractedField, ExtractedRow
    from backend.services.ocr_service import OcrService

    def row(rate: str) -> ExtractedRow:
        return ExtractedRow(
            row_index=0,
            fields={
                "rate": ExtractedField(
                    field="rate", text=rate, confidence=0.9, engine="claude-vision"
                )
            },
        )

    assert OcrService._sheet_rate(row("75")) == decimal.Decimal("75")
    assert OcrService._sheet_rate(row("1,200")) == decimal.Decimal("1200")
    assert OcrService._sheet_rate(row("₹140/-")) == decimal.Decimal("140")
    # zero means "ask me", which is what an absent or unreadable rate is
    assert OcrService._sheet_rate(row("")) == decimal.Decimal("0")
    assert OcrService._sheet_rate(row("?")) == decimal.Decimal("0")


def test_the_brand_is_always_asked_when_the_sheet_has_no_label() -> None:
    """This bill has no brand column at all, and a code is unique only
    within a brand -- so a brandless purchase makes every later code
    ambiguous."""
    from backend.api.commands.intake_commands import SLOT_ORDER

    assert "brand" in SLOT_ORDER
    assert SLOT_ORDER.index("brand") == 1, "asked right after the supplier"
