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
#: Anthropic's guidance is that images above ~1568px on the long edge
#: cost more without reading better. At 2576 a sheet was billing roughly
#: 6,600 image tokens; at 1568 it is about 2,500, for the same rows.
MAX_EDGE_PX = 1568
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
                    "label": {
                        "type": "string",
                        "description": (
                            "The LABEL / BRAND column -- the brand this item is sold under "
                            "(e.g. TOP, FOLD). Often repeats down the sheet. '' if absent."
                        ),
                    },
                    "rate": {
                        "type": "string",
                        "description": (
                            "Rate or unit price per kg if the sheet shows one (Rate, "
                            "Unit Price, Price). A line written as '22 x 80 x 75' means "
                            "22 bales x 80 kg at rate 75 -- the last factor is the rate. "
                            "'' if the sheet doesn't state one."
                        ),
                    },
                },
                "required": [
                    "code",
                    "description",
                    "qty",
                    "weight_per_unit",
                    "total_weight",
                    "label",
                    "rate",
                ],
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
with no header. A column of repeating words like FOLD or TOP is the \
LABEL/brand column -- put it in `label`, never in `code` or \
`description`.
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
- a label/brand column (Label, Brand, Mark) -- often the same word on \
every row
- a rate / unit price column, if the sheet has one

Some sheets write a line as "22 x 80 x 75 = 132000": that is bales x \
kg-per-bale x rate. Put 22 in qty, 80 in the per-unit weight, 75 in \
rate, and 1760 (22 x 80) in the total weight.

Ignore serial-number columns and running totals. Also capture the \
supplier, invoice number and invoice date if they appear anywhere on \
the sheet."""


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


#: What a model costs per million tokens, for the log line. Approximate
#: and only used to make spend visible -- billing is Anthropic's number,
#: not this one.
_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-opus-5": (5.0, 25.0),
}


def estimate_cost(model: str, input_tokens: int | None, output_tokens: int | None) -> float:
    """Roughly what that call cost, in dollars.

    Logged per read because a per-sheet price is the only way anyone
    notices the bill drifting before the month ends.
    """
    prices = _PRICES_PER_MTOK.get(model)
    if prices is None or input_tokens is None or output_tokens is None:
        return 0.0
    return round(input_tokens / 1e6 * prices[0] + output_tokens / 1e6 * prices[1], 5)


def rows_to_json(rows: list[ExtractedRow]) -> list[dict[str, Any]]:
    """Serialise an extraction so it can be cached on the attachment and
    never paid for twice."""
    return [
        {
            "row_index": row.row_index,
            "fields": {
                name: {"text": field.text, "confidence": field.confidence, "engine": field.engine}
                for name, field in row.fields.items()
            },
        }
        for row in rows
    ]


def rows_from_json(payload: list[Any]) -> list[ExtractedRow]:
    return [
        ExtractedRow(
            row_index=int(entry["row_index"]),
            fields={
                name: ExtractedField(
                    field=name,
                    text=str(spec.get("text", "")),
                    confidence=float(spec.get("confidence", 0.0)),
                    engine=str(spec.get("engine", "cache")),
                )
                for name, spec in entry.get("fields", {}).items()
            },
        )
        for entry in payload
    ]


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

    def read_sale_sheet(self, data: bytes, mime_type: str = "image/jpeg") -> VisionSaleSheet:
        """A sales note, read with the same engine as a purchase sheet.

        Separate schema and prompt, not a separate pipeline: a sales note
        carries a rate and a line total where a purchase sheet carries
        weights, and asking one prompt to cover both is how a rate ends
        up in a weight column.
        """
        payload = self._call(data, mime_type, SALE_SCHEMA, SALE_SYSTEM_PROMPT, SALE_USER_PROMPT)
        rows = [
            VisionSaleRow(
                code=str(row.get("code", "")).strip(),
                description=str(row.get("description", "")).strip(),
                qty=str(row.get("qty", "")).strip(),
                rate=str(row.get("rate", "")).strip(),
                line_total=str(row.get("line_total", "")).strip(),
            )
            for row in payload.get("rows", [])
        ]
        logger.info("vision_sale_sheet_read", rows=len(rows), model=self._model)
        return VisionSaleSheet(
            rows=rows,
            customer_name=str(payload.get("customer_name", "")),
            sale_date=str(payload.get("sale_date", "")),
            declared_total=str(payload.get("declared_total", "")),
            unreadable_note=str(payload.get("unreadable_note", "")),
            model=self._model,
        )

    def _call(
        self,
        data: bytes,
        mime_type: str,
        schema: dict[str, Any],
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        """One request/response path for both sheet kinds."""
        import json

        if not self.available():
            raise VisionUnavailableError("no ANTHROPIC_API_KEY configured")
        block = self._content_block(data, mime_type)
        try:
            response = self._get_client().messages.create(
                model=self._model,
                max_tokens=16000,
                system=system_prompt,
                output_config={"format": {"type": "json_schema", "schema": schema}},
                messages=[
                    {"role": "user", "content": [block, {"type": "text", "text": user_prompt}]}
                ],
            )
        except Exception as exc:  # noqa: BLE001 -- any failure falls back
            raise VisionUnavailableError(str(exc)) from exc
        if getattr(response, "stop_reason", None) == "refusal":
            raise VisionUnavailableError("model declined to transcribe this image")
        text = next((b.text for b in response.content if b.type == "text"), "")
        if not text:
            raise VisionUnavailableError("empty response")
        try:
            parsed: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError as exc:
            raise VisionUnavailableError(f"unparseable response: {exc}") from exc
        return parsed

    @staticmethod
    def _content_block(data: bytes, mime_type: str) -> dict[str, Any]:
        if mime_type == "application/pdf":
            return {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.standard_b64encode(data).decode(),
                },
            }
        image_bytes, media_type = downscale(data)
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(image_bytes).decode(),
            },
        }

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
                        "label": _field("label", str(row.get("label", ""))),
                        "rate": _field("rate", str(row.get("rate", ""))),
                    },
                )
            )

        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)
        logger.info(
            "vision_sheet_read",
            rows=len(rows),
            model=self._model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_usd=estimate_cost(self._model, input_tokens, output_tokens),
        )
        return VisionSheet(
            rows=rows,
            supplier_name=str(payload.get("supplier_name", "")),
            invoice_no=str(payload.get("invoice_no", "")),
            invoice_date=str(payload.get("invoice_date", "")),
            unreadable_note=str(payload.get("unreadable_note", "")),
            model=self._model,
        )


# --------------------------------------------------------------------
# sales sheets
# --------------------------------------------------------------------

SALE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "Item code exactly as written; '' if absent, "
                            f"'{UNREADABLE}' if unreadable."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "Item description if present, else ''.",
                    },
                    "qty": {
                        "type": "string",
                        "description": (
                            "Quantity sold as a plain number string -- the KG or Qty column."
                        ),
                    },
                    "rate": {
                        "type": "string",
                        "description": (
                            "Rate per unit as a plain number string. Strip any trailing "
                            "'/-' or currency mark."
                        ),
                    },
                    "line_total": {
                        "type": "string",
                        "description": (
                            "The line's total **as written on the sheet**. Do not compute "
                            "it; '' if the sheet doesn't show one."
                        ),
                    },
                },
                "required": ["code", "description", "qty", "rate", "line_total"],
                "additionalProperties": False,
            },
        },
        "customer_name": {
            "type": "string",
            "description": "The party this sale is to, if written anywhere, else ''.",
        },
        "sale_date": {"type": "string", "description": "Date as written (any format), else ''."},
        "declared_total": {
            "type": "string",
            "description": "The sheet's own grand total as written, else ''.",
        },
        "unreadable_note": {
            "type": "string",
            "description": "Brief note if parts were unreadable, else ''.",
        },
    },
    "required": ["rows", "customer_name", "sale_date", "declared_total", "unreadable_note"],
    "additionalProperties": False,
}

SALE_SYSTEM_PROMPT = """\
You transcribe handwritten and printed sales notes into structured data. \
You are transcribing, not interpreting: copy what is written.

Rules:
- One entry per ITEM row. Never emit a header row or the grand-total row.
- The page may be rotated, photographed at an angle, on ruled or squared \
paper, and written by hand. Read it anyway.
- Copy codes character for character, including case, digits and hyphens.
- Numbers must be plain number strings: no units, no thousands \
separators, no currency symbols, no trailing '/-'.
- **Never compute a value.** Copy the line total the sheet shows; if it \
shows none, return ''. If the arithmetic looks wrong, still copy what is \
written -- a disagreement is something the user must see, not something \
you resolve.
- If a cell is genuinely unreadable, put '?' rather than guessing.
- Preserve the order the rows appear in.
"""

SALE_USER_PROMPT = """\
Transcribe every item row of this sales note.

Columns you may see, under varying headers and in any order:
- an item code column (Item Code, Code, Design)
- a quantity column (KG, Qty, Pcs)
- a rate column (Rate, KG Rate, Price) -- often written like '200/-'
- a line total column (Total, Amount)

Also capture the customer or party name, the date, and the sheet's own \
grand total if written."""


@dataclasses.dataclass(frozen=True)
class VisionSaleRow:
    code: str
    description: str
    qty: str
    rate: str
    line_total: str


@dataclasses.dataclass(frozen=True)
class VisionSaleSheet:
    rows: list[VisionSaleRow]
    customer_name: str
    sale_date: str
    declared_total: str
    unreadable_note: str
    model: str
