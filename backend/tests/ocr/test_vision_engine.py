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
