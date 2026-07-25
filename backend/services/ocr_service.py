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

        for index, row in enumerate(sheet.extraction.rows, start=1):
            code, product_id, unit_code, note = await self._resolve_code(org_id, row, supplier_id)
            if note:
                auto_corrections.append(f"Line {index}: {note}")

            qty_field = row.fields.get("qty")
            qty = self._decimal(qty_field.text) if qty_field else None
            if qty is None:
                weight_field = row.fields.get("total_weight_kg")
                qty = self._decimal(weight_field.text) if weight_field else None
            if qty is None:
                low_confidence.append(f"Line {index}, Qty: couldn't read this — what should it be?")
                qty = ZERO

            if not code:
                low_confidence.append(
                    f"Line {index}, Code: couldn't read this clearly — what should it be?"
                )
            elif qty_field is not None and qty_field.needs_review:
                low_confidence.append(f"Line {index} ({code}): quantity is unclear, please check")

            lines.append(
                DraftLine(
                    code=code.upper(),
                    qty=qty,
                    rate=ZERO,  # rate is a required_manual_field -- not on the sheet
                    product_id=product_id,
                    resolved_code=code.upper() if product_id else None,
                    unit_code=unit_code,
                )
            )

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
