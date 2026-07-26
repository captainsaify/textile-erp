"""DB-facing OCR glue -- docs/07_OCR.md §5, §8, §9, §11.

Owns everything the pure pipeline deliberately doesn't: attachment
storage and photo-hash duplicate detection, template resolution, the
learning dictionary, fuzzy matching against the product catalog, and
turning a ParsedSheet into the same purchase Draft the typed `purchase`
command produces (so both entry paths share one confirmation flow).
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import hashlib
import re
import uuid
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.models import Attachment, OcrLearningDictionary, OcrTemplate, ProductType, User
from backend.ocr.extract import ColumnMapping, ExtractedRow
from backend.ocr.pipeline import ParsedSheet
from backend.repositories.product_repository import ProductRepository
from backend.services.purchase_service import Draft, DraftLine

logger = get_logger(__name__)

AUTO_MATCH_THRESHOLD = 0.85  # §9 pg_trgm auto-accept
ZERO = decimal.Decimal("0")

_ALNUM = re.compile(r"[^A-Za-z0-9]")
# how far the sheet's stated total KG may drift from qty x kg before the
# computed value is preferred
_TOTAL_DRIFT_TOLERANCE = decimal.Decimal("0.02")
_TOTAL_WORD = re.compile(r"^\s*(grand\s*)?totals?\b", re.IGNORECASE)
# the template's own column labels, for spotting a header band read as data
_HEADER_WORDS = {
    "qty",
    "quantity",
    "qnty",
    "description",
    "desc",
    "item",
    "particulars",
    "code",
    "design",
    "label",
    "kg",
    "wt",
    "weight",
    "t.kg",
    "total kg",
    "tot kg",
    "total weight",
    "s.no",
    "sno",
    "sr.no",
    "amount",
    "value",
}


@dataclasses.dataclass(frozen=True)
class ExistingAttachment:
    attachment_id: uuid.UUID
    created_at: datetime.datetime


@dataclasses.dataclass(frozen=True)
class DraftBuild:
    draft: Draft
    low_confidence_notes: list[str]
    auto_corrections: list[str]
    unmapped_headers: list[str]
    hard_to_read: bool


class OcrService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._products = ProductRepository(session)

    # --- attachments ---------------------------------------------------

    @staticmethod
    def hash_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    async def find_duplicate_photo(
        self, org_id: uuid.UUID, sha256_hash: str
    ) -> ExistingAttachment | None:
        """Checked before OCR even runs -- docs/04_Purchases.md §6."""
        row = (
            await self._session.execute(
                select(Attachment.id, Attachment.created_at).where(
                    Attachment.org_id == org_id, Attachment.sha256_hash == sha256_hash
                )
            )
        ).first()
        if row is None:
            return None
        return ExistingAttachment(attachment_id=row[0], created_at=row[1])

    async def store_attachment(
        self,
        actor: User,
        *,
        data: bytes,
        mime_type: str,
        whatsapp_media_id: str | None,
    ) -> Attachment:
        settings = get_settings()
        sha256_hash = self.hash_bytes(data)
        directory = Path(settings.attachments_dir) / str(actor.org_id)
        directory.mkdir(parents=True, exist_ok=True)
        suffix = ".pdf" if mime_type == "application/pdf" else ".jpg"
        path = directory / f"{sha256_hash}{suffix}"
        if not path.exists():
            path.write_bytes(data)
        attachment = Attachment(
            org_id=actor.org_id,
            file_path=str(path),
            mime_type=mime_type,
            file_size_bytes=len(data),
            sha256_hash=sha256_hash,
            status="processing",
            whatsapp_media_id=whatsapp_media_id,
            created_by=actor.id,
        )
        self._session.add(attachment)
        await self._session.flush()
        return attachment

    async def mark_attachment(
        self, attachment_id: uuid.UUID, status: str, ocr_result: dict[str, object] | None = None
    ) -> None:
        attachment = await self._session.get(Attachment, attachment_id)
        if attachment is None:
            return
        attachment.status = status
        if ocr_result is not None:
            attachment.ocr_result = ocr_result

    # --- templates -----------------------------------------------------

    async def resolve_template(
        self, org_id: uuid.UUID, supplier_id: uuid.UUID | None = None
    ) -> list[ColumnMapping]:
        """(product_type, supplier) -> (product_type, NULL) -> the org's
        only product type's default -- §5 resolution order."""
        stmt = (
            select(OcrTemplate)
            .join(ProductType, ProductType.id == OcrTemplate.product_type_id)
            .where(OcrTemplate.org_id == org_id, OcrTemplate.is_active.is_(True))
        )
        templates = list((await self._session.execute(stmt)).scalars())
        if not templates:
            return []
        chosen = next(
            (t for t in templates if supplier_id is not None and t.supplier_id == supplier_id),
            None,
        ) or next((t for t in templates if t.supplier_id is None), templates[0])
        return [
            ColumnMapping(
                field=str(entry["field"]),
                header_aliases=[str(alias) for alias in entry.get("header_aliases", [])],
            )
            for entry in chosen.column_mapping
        ]

    # --- learning dictionary -------------------------------------------

    async def lookup_correction(
        self, org_id: uuid.UUID, field: str, raw_text: str, supplier_id: uuid.UUID | None
    ) -> str | None:
        """Supplier-specific entries beat org-wide ones -- §8."""
        stmt = select(OcrLearningDictionary).where(
            OcrLearningDictionary.org_id == org_id,
            OcrLearningDictionary.field == field,
            func.lower(OcrLearningDictionary.raw_ocr_text) == raw_text.lower(),
        )
        entries = list((await self._session.execute(stmt)).scalars())
        if not entries:
            return None
        entries.sort(key=lambda e: (e.supplier_id != supplier_id, e.supplier_id is None))
        return entries[0].corrected_value

    async def record_correction(
        self,
        org_id: uuid.UUID,
        *,
        field: str,
        raw_ocr_text: str,
        corrected_value: str,
        supplier_id: uuid.UUID | None = None,
    ) -> None:
        """Upsert with hit_count increment -- §8."""
        if not raw_ocr_text.strip() or raw_ocr_text.strip() == corrected_value.strip():
            return
        stmt = pg_insert(OcrLearningDictionary).values(
            org_id=org_id,
            supplier_id=supplier_id,
            field=field,
            raw_ocr_text=raw_ocr_text.strip(),
            corrected_value=corrected_value.strip(),
            hit_count=1,
        )
        await self._session.execute(
            stmt.on_conflict_do_update(
                index_elements=[
                    OcrLearningDictionary.org_id,
                    OcrLearningDictionary.supplier_id,
                    OcrLearningDictionary.field,
                    OcrLearningDictionary.raw_ocr_text,
                ],
                set_={
                    "corrected_value": corrected_value.strip(),
                    "hit_count": OcrLearningDictionary.hit_count + 1,
                    "updated_at": func.now(),
                },
            )
        )

    # --- draft construction --------------------------------------------

    @staticmethod
    def _decimal(text: str) -> decimal.Decimal | None:
        try:
            value = decimal.Decimal(text)
        except (decimal.InvalidOperation, ValueError):
            return None
        return value if value > ZERO else None

    @classmethod
    def _is_noise_row(cls, row: ExtractedRow) -> bool:
        """Real sheets bracket their items with a repeated header band and
        a grand-total line, and ruled grids yield blank filler rows. None
        of those are purchases -- dropping them here beats making the user
        delete them from every preview."""
        code = _ALNUM.sub("", (row.fields["code"].text if "code" in row.fields else "")).strip()
        raw_description = (
            row.fields["description"].text.strip() if "description" in row.fields else ""
        )
        # a totals line often reads as bare rules or pipes -- punctuation
        # with no letters or digits is not a description
        description = raw_description if _ALNUM.sub("", raw_description).strip() else ""
        numbers = [
            cls._decimal(row.fields[field].text)
            for field in ("qty", "weight_kg", "total_weight_kg")
            if field in row.fields
        ]
        has_number = any(value is not None for value in numbers)

        if not code and not description:
            return True  # blank filler, or a totals line with only a number
        if _TOTAL_WORD.match(description) or _TOTAL_WORD.match(code):
            return True
        if not code and not has_number:
            return True
        # header band read as data: the cells echo the template's own labels
        return description.lower() in _HEADER_WORDS or code.lower() in _HEADER_WORDS

    @classmethod
    def _costing_quantity(
        cls, row: ExtractedRow
    ) -> tuple[decimal.Decimal | None, decimal.Decimal | None, decimal.Decimal | None]:
        """(costing_qty, pieces, weight_per_unit).

        Textile is costed per KG, so the quantity that drives inventory
        and line_total is total KG -- the sheet's Qty column counts rolls
        (docs/04_Purchases.md §12). Falls back to pieces for product types
        that carry no weight columns at all.
        """
        pieces = cls._decimal(row.fields["qty"].text) if "qty" in row.fields else None
        per_unit = cls._decimal(row.fields["weight_kg"].text) if "weight_kg" in row.fields else None
        total = (
            cls._decimal(row.fields["total_weight_kg"].text)
            if "total_weight_kg" in row.fields
            else None
        )
        computed = pieces * per_unit if pieces is not None and per_unit is not None else None
        if total is None:
            total = computed
        elif computed is not None and computed > ZERO:
            # The sheet states qty, kg/unit and total kg, so the three are
            # checkable against each other. A misread digit in any one of
            # them shows up here; trust the product of the two simpler
            # cells over the wider total cell, and flag it (§7 -- a wrong
            # silent value is worse than a visible question).
            drift = abs(total - computed) / computed
            if drift > _TOTAL_DRIFT_TOLERANCE:
                logger.info(
                    "ocr_total_weight_mismatch",
                    stated=str(total),
                    computed=str(computed),
                )
                total = computed
        costing = total if total is not None else pieces
        return costing, pieces, per_unit

    async def _resolve_code(
        self, org_id: uuid.UUID, row: ExtractedRow, supplier_id: uuid.UUID | None
    ) -> tuple[str, uuid.UUID | None, str | None, str | None]:
        """Returns (code_for_draft, product_id, unit_code, auto_correction_note)."""
        field = row.fields.get("code")
        raw = field.text.strip() if field else ""
        note: str | None = None

        if raw:
            corrected = await self.lookup_correction(org_id, "code", raw, supplier_id)
            if corrected and corrected != raw:
                note = f'{corrected} ✓ (auto-corrected from "{raw}")'
                raw = corrected

        if not raw:
            description = row.fields.get("description")
            if description and description.text.strip():
                matches = await self._products.search(org_id, description.text.strip(), limit=1)
                if matches:
                    return matches[0].code, matches[0].id, matches[0].unit.code, note
            return "", None, None, note

        exact = await self._products.get_by_code(org_id, raw)
        if exact is not None:
            return exact.code, exact.id, exact.unit.code, note

        matches = await self._products.search(org_id, raw, limit=1)
        if matches:
            from rapidfuzz import fuzz

            score = fuzz.ratio(matches[0].code.lower(), raw.lower()) / 100
            if score >= AUTO_MATCH_THRESHOLD:
                return matches[0].code, matches[0].id, matches[0].unit.code, note
        return raw, None, None, note

    async def build_draft(
        self,
        org_id: uuid.UUID,
        sheet: ParsedSheet,
        *,
        supplier_id: uuid.UUID | None = None,
        supplier_name: str = "",
        invoice_no: str = "",
        invoice_date: datetime.date | None = None,
    ) -> DraftBuild:
        """ParsedSheet -> the same Draft the typed command produces, so
        both paths share one confirmation flow."""
        lines: list[DraftLine] = []
        low_confidence: list[str] = []
        auto_corrections: list[str] = []

        skipped = 0
        index = 0
        for row in sheet.extraction.rows:
            if self._is_noise_row(row):
                skipped += 1
                continue
            index += 1
            code, product_id, unit_code, note = await self._resolve_code(org_id, row, supplier_id)
            if note:
                auto_corrections.append(f"Line {index}: {note}")

            qty, pieces, per_unit = self._costing_quantity(row)
            if qty is None:
                low_confidence.append(f"Line {index}, Qty: couldn't read this — what should it be?")
                qty = ZERO

            description_field = row.fields.get("description")
            description = description_field.text.strip() if description_field else None

            if not code:
                low_confidence.append(
                    f"Line {index}, Code: couldn't read this clearly — what should it be?"
                )
            else:
                qty_field = row.fields.get("qty")
                if qty_field is not None and qty_field.needs_review:
                    low_confidence.append(
                        f"Line {index} ({code}): quantity is unclear, please check"
                    )

            lines.append(
                DraftLine(
                    code=code.upper(),
                    qty=qty,
                    rate=ZERO,  # rate is a required_manual_field -- not on the sheet
                    product_id=product_id,
                    resolved_code=code.upper() if product_id else None,
                    unit_code=unit_code,
                    description=description or None,
                    pieces=pieces,
                    weight_per_unit=per_unit,
                )
            )
        if skipped:
            logger.info("ocr_noise_rows_skipped", count=skipped)

        draft = Draft(
            supplier_id=supplier_id,
            supplier_name=supplier_name,
            invoice_no=invoice_no,
            invoice_date=invoice_date or datetime.date.today(),
            brand_id=None,
            brand_name=None,
            lines=lines,
            freight=ZERO,
            other_charges=ZERO,
            declared_total=None,
        )
        return DraftBuild(
            draft=draft,
            low_confidence_notes=low_confidence,
            auto_corrections=auto_corrections,
            unmapped_headers=sheet.extraction.unmapped_headers,
            hard_to_read=sheet.extraction.hard_to_read,
        )
