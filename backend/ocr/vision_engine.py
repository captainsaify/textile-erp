"""Claude vision sheet reader -- an alternative front end to the local
OCR pipeline (docs/07_OCR.md §6 names PaddleOCR/Tesseract; this reads the
sheet with a vision model instead).

Why it exists: the local pipeline's accuracy is bounded by grid detection
on a photographed sheet -- a merged column boundary silently shifts every
field. A vision model reads the table as a table, so unnamed columns,
uneven lighting and a slight angle stop being failure modes.

It deliberately emits the same ExtractedRow shape the local pipeline
produces, so everything downstream -- noise-row rejection, the
qty x kg cross-check, product matching, the learning dictionary -- runs
unchanged and the two engines stay interchangeable.

Numbers come back as strings and are parsed with Decimal: a float would
violate the project's money/quantity rule before the value ever reaches
the draft.
"""

from __future__ import annotations

import base64
import dataclasses
from typing import Any

import cv2
import numpy as np

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.ocr.extract import ExtractedField, ExtractedRow

logger = get_logger(__name__)

# Opus-tier high-resolution vision tops out here; larger images are
# downscaled server-side anyway, so do it locally to bound token cost.
MAX_EDGE_PX = 2576
VISION_CONFIDENCE = 0.97  # what the model returns is not cell-scored
UNREADABLE = "?"

SHEET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "description": "One entry per item row, in sheet order.",
            "items": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "Product/item code exactly as printed. "
                            f"Use '{UNREADABLE}' if unreadable."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "Item description exactly as printed; '' if none.",
                    },
                    "qty": {
                        "type": "string",
                        "description": (
                            "Quantity column as a plain number string (pieces/rolls/bags). "
                            f"'' if absent, '{UNREADABLE}' if unreadable."
                        ),
                    },
                    "weight_per_unit": {
                        "type": "string",
                        "description": (
                            "Per-unit weight (the KG column) as a number string; '' if absent."
                        ),
                    },
                    "total_weight": {
                        "type": "string",
                        "description": (
                            "Total weight (the T.KG column) as a number string; '' if absent."
                        ),
                    },
                },
                "required": ["code", "description", "qty", "weight_per_unit", "total_weight"],
                "additionalProperties": False,
            },
        },
        "supplier_name": {"type": "string", "description": "Supplier name if printed, else ''."},
        "invoice_no": {"type": "string", "description": "Invoice number if printed, else ''."},
        "invoice_date": {
            "type": "string",
            "description": "Invoice date as printed (any format), else ''.",
        },
        "unreadable_note": {
            "type": "string",
            "description": "Brief note if parts were unreadable, else ''.",
        },
    },
    "required": ["rows", "supplier_name", "invoice_no", "invoice_date", "unreadable_note"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You transcribe purchase/inventory sheets into structured data. You are \
transcribing, not interpreting: copy what is printed.

Rules:
- One entry per ITEM row only. Never emit the header row, a blank filler \
row, a totals/grand-total row, or a running subtotal.
- Copy codes and descriptions character for character, including case, \
digits, and punctuation such as hyphens. Do not expand abbreviations, \
correct spelling, or tidy wording.
- Do not map a value into the wrong column. Some sheets carry columns \
with no header, or a repeated label like FOLD or TOP; those are not the \
code and not the description.
- Numbers must be plain number strings with no units, thousands \
separators, or currency symbols.
- If a cell is genuinely unreadable, put '?' rather than guessing. A \
visible blank is '', which is different from unreadable.
- Preserve sheet order.
"""

USER_PROMPT = """\
Transcribe every item row of this purchase sheet.

Columns you may see, under varying headers:
- a quantity/count column (Qty, Quantity, Pcs)
- a description column (Description, Item, Particulars)
- a code column (Code, Item Code, Design)
- a per-unit weight column (KG, Wt, Weight)
- a total weight column (T.KG, Total KG, Total Weight)

Ignore serial-number columns, label columns, and any column whose values \
repeat identically down the sheet. Also capture the supplier, invoice \
number and invoice date if they appear anywhere on the sheet."""


@dataclasses.dataclass(frozen=True)
class VisionSheet:
    rows: list[ExtractedRow]
    supplier_name: str
    invoice_no: str
    invoice_date: str
    unreadable_note: str
    model: str


class VisionUnavailableError(Exception):
    """No API key, SDK, or the call failed -- caller falls back to local OCR."""


def downscale(data: bytes, max_edge: int = MAX_EDGE_PX) -> tuple[bytes, str]:
    """Shrink to the vision tier's long edge. Returns (bytes, media_type);
    passes the original through unchanged if it can't be decoded."""
    buffer = np.frombuffer(data, dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None:
        return data, "image/jpeg"
    height, width = image.shape[:2]
    longest = max(height, width)
    if longest > max_edge:
        scale = max_edge / longest
        image = cv2.resize(
            image, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA
        )
    ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    if not ok:
        return data, "image/jpeg"
    return bytes(encoded), "image/jpeg"


def _field(name: str, value: str) -> ExtractedField:
    text = "" if value.strip() in {"", UNREADABLE} else value.strip()
    # an explicitly unreadable cell scores below the review threshold so it
    # surfaces as a question rather than a silent blank (docs/07_OCR.md §7)
    confidence = 0.0 if value.strip() == UNREADABLE else VISION_CONFIDENCE
    return ExtractedField(field=name, text=text, confidence=confidence, engine="claude-vision")


class VisionSheetReader:
    """Reads a sheet image/PDF with Claude vision into ExtractedRows."""

    def __init__(self, client: Any = None, model: str | None = None) -> None:
        settings = get_settings()
        self._api_key = settings.anthropic_api_key
        self._model = model or settings.vision_model
        self._client = client

    def available(self) -> bool:
        return bool(self._api_key) or self._client is not None

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def read_sheet(self, data: bytes, mime_type: str = "image/jpeg") -> VisionSheet:
        if not self.available():
            raise VisionUnavailableError("no ANTHROPIC_API_KEY configured")

        if mime_type == "application/pdf":
            source: dict[str, Any] = {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(data).decode(),
            }
            block: dict[str, Any] = {"type": "document", "source": source}
        else:
            image_bytes, media_type = downscale(data)
            block = {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.standard_b64encode(image_bytes).decode(),
                },
            }

        try:
            response = self._get_client().messages.create(
                model=self._model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                output_config={"format": {"type": "json_schema", "schema": SHEET_SCHEMA}},
                messages=[
                    {"role": "user", "content": [block, {"type": "text", "text": USER_PROMPT}]}
                ],
            )
        except Exception as exc:  # noqa: BLE001 -- any failure falls back to local OCR
            raise VisionUnavailableError(str(exc)) from exc

        if getattr(response, "stop_reason", None) == "refusal":
            raise VisionUnavailableError("model declined to transcribe this image")

        import json

        text = next((b.text for b in response.content if b.type == "text"), "")
        if not text:
            raise VisionUnavailableError("empty response")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise VisionUnavailableError(f"unparseable response: {exc}") from exc

        rows: list[ExtractedRow] = []
        for index, row in enumerate(payload.get("rows", [])):
            rows.append(
                ExtractedRow(
                    row_index=index,
                    fields={
                        "code": _field("code", str(row.get("code", ""))),
                        "description": _field("description", str(row.get("description", ""))),
                        "qty": _field("qty", str(row.get("qty", ""))),
                        "weight_kg": _field("weight_kg", str(row.get("weight_per_unit", ""))),
                        "total_weight_kg": _field(
                            "total_weight_kg", str(row.get("total_weight", ""))
                        ),
                    },
                )
            )

        usage = getattr(response, "usage", None)
        logger.info(
            "vision_sheet_read",
            rows=len(rows),
            model=self._model,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )
        return VisionSheet(
            rows=rows,
            supplier_name=str(payload.get("supplier_name", "")),
            invoice_no=str(payload.get("invoice_no", "")),
            invoice_date=str(payload.get("invoice_date", "")),
            unreadable_note=str(payload.get("unreadable_note", "")),
            model=self._model,
        )
