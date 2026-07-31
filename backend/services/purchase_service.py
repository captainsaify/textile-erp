"""Purchase draft resolution and confirmation -- docs/04_Purchases.md.

Drafts live in the WhatsApp session context (mirrored to Postgres via
whatsapp_sessions) until CONFIRM; on confirmation everything -- header,
lines, inventory movements, weighted-average update, journal, audit --
is written in one transaction. A caught duplicate never touches
inventory (docs/03_Inventory.md §5) because nothing is written until
all checks pass.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import uuid
from typing import Any

from rapidfuzz import fuzz
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import (
    ExactDuplicateInvoiceError,
    FuzzyDuplicateInvoiceError,
    TotalMismatchWarning,
    ValidationError,
)
from backend.models import (
    Brand,
    Product,
    PurchaseHeader,
    PurchaseLine,
    Supplier,
    User,
    Warehouse,
)
from backend.models.enums import AccountCode, PurchaseStatus
from backend.repositories.accounting_repository import business_today
from backend.repositories.party_repository import SupplierRepository
from backend.repositories.product_repository import ProductRepository
from backend.repositories.purchase_repository import PurchaseRepository
from backend.repositories.settings_repository import SettingsRepository
from backend.services.audit_service import AuditService
from backend.services.inventory_service import InventoryService
from backend.services.journal_service import JournalService

TWO = decimal.Decimal("0.01")
FOUR = decimal.Decimal("0.0001")
ZERO = decimal.Decimal("0")

QTY_SANITY_CEILING = decimal.Decimal("100000")
SUPPLIER_MATCH_THRESHOLD = 80  # rapidfuzz ratio, 0-100 -- §7
PRODUCT_MATCH_THRESHOLD = 85
INVOICE_SIMILARITY_THRESHOLD = 85  # §6 layer 2
DUPLICATE_TOTAL_TOLERANCE = decimal.Decimal("0.01")  # 1%
LINE_OVERLAP_THRESHOLD = 0.7


def allocate(total: decimal.Decimal, weights: list[decimal.Decimal]) -> list[decimal.Decimal]:
    """Split `total` across lines proportional to `weights`, 2dp; any
    rounding remainder lands on the largest-weight line so the split
    always sums exactly (docs/04_Purchases.md §4)."""
    if total == ZERO or not weights:
        return [ZERO for _ in weights]
    weight_sum = sum(weights, ZERO)
    if weight_sum == ZERO:
        shares = [ZERO for _ in weights]
        shares[-1] = total
        return shares
    shares = [(total * w / weight_sum).quantize(TWO) for w in weights]
    remainder = total - sum(shares, ZERO)
    if remainder != ZERO:
        target = max(range(len(weights)), key=lambda i: (weights[i], i))
        shares[target] += remainder
    return shares


@dataclasses.dataclass
class DraftLine:
    code: str
    # The costing quantity, in the product's unit. For weight-costed
    # textile that is total KG, not the piece count -- docs/03_Inventory.md
    # §2 and docs/04_Purchases.md §12 (line_total = total_weight_kg * rate).
    qty: decimal.Decimal
    rate: decimal.Decimal
    product_id: uuid.UUID | None
    resolved_code: str | None  # actual product code when fuzzy-matched
    unit_code: str | None
    description: str | None = None  # from the sheet; used when creating
    pieces: decimal.Decimal | None = None  # sheet's Qty column (rolls/bags)
    weight_per_unit: decimal.Decimal | None = None  # sheet's KG column

    @property
    def line_total(self) -> decimal.Decimal:
        return (self.qty * self.rate).quantize(TWO)


@dataclasses.dataclass
class Draft:
    supplier_id: uuid.UUID | None
    supplier_name: str
    invoice_no: str
    invoice_date: datetime.date
    brand_id: uuid.UUID | None
    brand_name: str | None
    lines: list[DraftLine]
    freight: decimal.Decimal
    other_charges: decimal.Decimal
    declared_total: decimal.Decimal | None
    pending_override: bool = False
    total_resolution: str | None = None  # None | 'calculated' | 'invoice'
    #: the photo this draft was read from, so the confirmed purchase can
    #: point back at it -- that link is what lets duplicate-photo
    #: detection tell "already entered" from "tried and abandoned"
    source_attachment_id: uuid.UUID | None = None
    #: Codes on this sheet that already exist under a *different* brand.
    #: Not an error -- a code is unique only within a brand, so the same
    #: code under two brands is two products by design. It is surfaced
    #: because the likeliest cause is the brand being answered wrong, and
    #: confirming creates a second product that then diverges silently.
    brand_collisions: list[str] = dataclasses.field(default_factory=list)
    #: Codes that *did* resolve under this brand but exist under another
    #: one too -- VVP is a golden velvet pant under TOP and a velvet
    #: sport pant under MKD. Resolving to the answered brand is right,
    #: but it is a silent choice between two real products, so the
    #: preview says which ones were ambiguous before anything is saved.
    shared_codes: list[str] = dataclasses.field(default_factory=list)

    @property
    def subtotal(self) -> decimal.Decimal:
        return sum((line.line_total for line in self.lines), ZERO)

    @property
    def grand_total(self) -> decimal.Decimal:
        return self.subtotal + self.freight + self.other_charges

    @property
    def unresolved_codes(self) -> list[str]:
        return [line.code for line in self.lines if line.product_id is None]

    def to_context(self) -> dict[str, Any]:
        return {
            "supplier_id": str(self.supplier_id) if self.supplier_id else None,
            "supplier_name": self.supplier_name,
            "invoice_no": self.invoice_no,
            "invoice_date": self.invoice_date.isoformat(),
            "brand_id": str(self.brand_id) if self.brand_id else None,
            "brand_name": self.brand_name,
            "brand_collisions": list(self.brand_collisions),
            "shared_codes": list(self.shared_codes),
            "lines": [
                {
                    "code": line.code,
                    "qty": str(line.qty),
                    "rate": str(line.rate),
                    "product_id": str(line.product_id) if line.product_id else None,
                    "resolved_code": line.resolved_code,
                    "unit_code": line.unit_code,
                    "description": line.description,
                    "pieces": str(line.pieces) if line.pieces is not None else None,
                    "weight_per_unit": (
                        str(line.weight_per_unit) if line.weight_per_unit is not None else None
                    ),
                }
                for line in self.lines
            ],
            "freight": str(self.freight),
            "other_charges": str(self.other_charges),
            "declared_total": str(self.declared_total) if self.declared_total else None,
            "pending_override": self.pending_override,
            "total_resolution": self.total_resolution,
            "source_attachment_id": (
                str(self.source_attachment_id) if self.source_attachment_id else None
            ),
        }

    @classmethod
    def from_context(cls, context: dict[str, Any]) -> Draft:
        return cls(
            supplier_id=uuid.UUID(context["supplier_id"]) if context["supplier_id"] else None,
            supplier_name=context["supplier_name"],
            invoice_no=context["invoice_no"],
            invoice_date=datetime.date.fromisoformat(context["invoice_date"]),
            brand_id=uuid.UUID(context["brand_id"]) if context["brand_id"] else None,
            brand_name=context["brand_name"],
            brand_collisions=list(context.get("brand_collisions") or []),
            shared_codes=list(context.get("shared_codes") or []),
            lines=[
                DraftLine(
                    code=line["code"],
                    qty=decimal.Decimal(line["qty"]),
                    rate=decimal.Decimal(line["rate"]),
                    product_id=uuid.UUID(line["product_id"]) if line["product_id"] else None,
                    resolved_code=line["resolved_code"],
                    unit_code=line["unit_code"],
                    description=line.get("description"),
                    pieces=(
                        decimal.Decimal(line["pieces"]) if line.get("pieces") is not None else None
                    ),
                    weight_per_unit=(
                        decimal.Decimal(line["weight_per_unit"])
                        if line.get("weight_per_unit") is not None
                        else None
                    ),
                )
                for line in context["lines"]
            ],
            freight=decimal.Decimal(context["freight"]),
            other_charges=decimal.Decimal(context["other_charges"]),
            declared_total=(
                decimal.Decimal(context["declared_total"]) if context["declared_total"] else None
            ),
            pending_override=context.get("pending_override", False),
            total_resolution=context.get("total_resolution"),
            source_attachment_id=(
                uuid.UUID(context["source_attachment_id"])
                if context.get("source_attachment_id")
                else None
            ),
        )


@dataclasses.dataclass(frozen=True)
class ConfirmedLine:
    code: str
    qty: decimal.Decimal
    rate: decimal.Decimal
    line_total: decimal.Decimal
    landed_cost_per_unit: decimal.Decimal
    unit_code: str
    resulting_qty: decimal.Decimal
    resulting_avg_cost: decimal.Decimal


@dataclasses.dataclass(frozen=True)
class ConfirmedPurchase:
    #: The saved header. Carried so the confirmation can attach the
    #: bill's document, which is built from the row rather than from
    #: this snapshot -- one source for what the sheet says.
    header_id: uuid.UUID
    supplier_name: str
    invoice_no: str
    invoice_date: datetime.date
    lines: list[ConfirmedLine]
    subtotal: decimal.Decimal
    freight: decimal.Decimal
    other_charges: decimal.Decimal
    grand_total: decimal.Decimal


class PurchaseService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._suppliers = SupplierRepository(session)
        self._products = ProductRepository(session)
        self._purchases = PurchaseRepository(session)
        self._inventory = InventoryService(session)
        self._journal = JournalService(session)
        self._audit = AuditService(session)
        self._settings = SettingsRepository(session)

    async def resolve_supplier(self, org_id: uuid.UUID, name: str) -> Supplier | None:
        candidates = await self._suppliers.search(org_id, name, limit=1)
        if not candidates:
            return None
        best = candidates[0]
        if best.name.lower() == name.lower():
            return best
        if fuzz.ratio(best.name.lower(), name.lower()) >= SUPPLIER_MATCH_THRESHOLD:
            return best
        return None

    async def resolve_product(
        self, org_id: uuid.UUID, code: str, brand_id: uuid.UUID | None = None
    ) -> Product | None:
        exact = await self._products.get_by_code(org_id, code, brand_id)
        if exact is not None:
            return exact
        candidates = await self._products.search(org_id, code, limit=1, brand_id=brand_id)
        if (
            candidates
            and fuzz.ratio(candidates[0].code.lower(), code.lower()) >= PRODUCT_MATCH_THRESHOLD
        ):
            return candidates[0]
        return None

    async def resolve_or_create_brand(self, org_id: uuid.UUID, name: str) -> Brand:
        from sqlalchemy import func, select

        existing = (
            await self._session.execute(
                select(Brand).where(
                    Brand.org_id == org_id,
                    func.lower(Brand.name) == name.lower(),
                    Brand.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        brand = Brand(org_id=org_id, name=name)
        self._session.add(brand)
        await self._session.flush()
        return brand

    async def create_supplier(self, actor: User, name: str) -> Supplier:
        supplier = Supplier(org_id=actor.org_id, name=name, created_by=actor.id)
        self._session.add(supplier)
        await self._session.flush()
        await self._audit.record(
            actor.org_id,
            actor.id,
            action="supplier.created",
            entity_type="suppliers",
            entity_id=supplier.id,
            after_state={"name": name},
        )
        return supplier

    async def create_product(
        self, actor: User, code: str, description: str, brand_id: uuid.UUID | None = None
    ) -> Product:
        """New product on the org's (single, for now) product type with
        that type's default unit -- never auto-created without the user
        asking (docs/04_Purchases.md §10). The brand is part of the
        product's identity: the same code under a different brand is a
        different product, not a duplicate."""
        from sqlalchemy import select

        from backend.models import ProductType

        product_type = (
            await self._session.execute(
                select(ProductType).where(ProductType.org_id == actor.org_id).limit(1)
            )
        ).scalar_one()
        product = Product(
            org_id=actor.org_id,
            product_type_id=product_type.id,
            code=code.upper(),
            description=description,
            brand_id=brand_id,
            unit_id=product_type.default_unit_id,
            created_by=actor.id,
        )
        self._session.add(product)
        await self._session.flush()
        await self._audit.record(
            actor.org_id,
            actor.id,
            action="product.created",
            entity_type="products",
            entity_id=product.id,
            after_state={
                "code": product.code,
                "description": description,
                "brand_id": str(brand_id) if brand_id else None,
            },
        )
        return product

    async def _default_warehouse(self, org_id: uuid.UUID) -> Warehouse:
        from sqlalchemy import select

        return (
            await self._session.execute(
                select(Warehouse).where(Warehouse.org_id == org_id, Warehouse.is_default.is_(True))
            )
        ).scalar_one()

    def _validate(self, draft: Draft, today: datetime.date) -> None:
        if not draft.lines:
            raise ValidationError("Send at least one item line.")
        if draft.unresolved_codes:
            codes = draft.unresolved_codes
            raise ValidationError(
                f"{len(codes)} item(s) still aren't in your catalogue: "
                + ", ".join(codes)
                + "\nReply 'create all products' to add them, then CONFIRM."
            )
        if draft.supplier_id is None:
            raise ValidationError(f"Supplier '{draft.supplier_name}' is not resolved yet.")
        if not draft.invoice_no or len(draft.invoice_no) > 100:
            raise ValidationError("Invoice number must be 1-100 characters.")
        if draft.invoice_date > today:
            raise ValidationError("Invoice date can't be in the future.")
        for line in draft.lines:
            if line.qty <= ZERO:
                raise ValidationError(f"Quantity for {line.code} must be greater than zero.")
        if draft.freight < ZERO or draft.other_charges < ZERO:
            raise ValidationError("Freight and other charges can't be negative.")

    async def _check_total_mismatch(self, org_id: uuid.UUID, draft: Draft) -> None:
        if draft.declared_total is None or draft.total_resolution is not None:
            return
        tolerance = await self._settings.purchase_total_mismatch_tolerance(org_id)
        difference = abs(draft.declared_total - draft.grand_total)
        if difference > tolerance:
            raise TotalMismatchWarning(
                f"⚠️ The invoice shows a total of {draft.declared_total}, but the line "
                f"items + freight + other charges add up to {draft.grand_total} "
                f'(difference: {difference}). Reply "use invoice total", '
                f'"use calculated total", or correct a line.',
                details={
                    "declared": str(draft.declared_total),
                    "calculated": str(draft.grand_total),
                },
            )

    async def _check_duplicates(self, org_id: uuid.UUID, draft: Draft, override: bool) -> None:
        assert draft.supplier_id is not None
        exact = await self._purchases.get_confirmed_by_invoice(
            org_id, draft.supplier_id, draft.invoice_no
        )
        if exact is not None:
            raise ExactDuplicateInvoiceError(
                draft.invoice_no,
                draft.supplier_name,
                details={
                    "existing_purchase_id": str(exact.id),
                    "confirmed_date": exact.invoice_date.isoformat(),
                    "grand_total": str(exact.grand_total),
                },
            )
        if override:
            return
        candidates = await self._purchases.find_potential_duplicates(
            org_id,
            draft.supplier_id,
            draft.invoice_date,
            window_days=await self._settings.duplicate_invoice_window_days(org_id),
        )
        draft_products = {line.product_id for line in draft.lines}
        for candidate in candidates:
            signals = 0
            invoice_similarity = fuzz.ratio(candidate.invoice_no.lower(), draft.invoice_no.lower())
            if invoice_similarity >= INVOICE_SIMILARITY_THRESHOLD:
                signals += 1
            if (
                candidate.grand_total
                and abs(candidate.grand_total - draft.grand_total)
                <= candidate.grand_total * DUPLICATE_TOTAL_TOLERANCE
            ):
                signals += 1
            candidate_products = {line.product_id for line in candidate.lines}
            if draft_products and (
                len(draft_products & candidate_products) / len(draft_products)
                >= LINE_OVERLAP_THRESHOLD
            ):
                signals += 1
            if signals >= 2:
                raise FuzzyDuplicateInvoiceError(
                    "⚠️ This looks similar to a purchase already recorded:\n"
                    f"{draft.supplier_name}, {candidate.invoice_no} "
                    f"(confirmed {candidate.invoice_date.strftime('%d-%m-%Y')}, "
                    f"₹{candidate.grand_total}).\n"
                    'Reply "confirm anyway" or "cancel".',
                    details={"candidate_id": str(candidate.id)},
                )

    async def confirm(
        self,
        actor: User,
        draft: Draft,
        *,
        override_duplicate: bool = False,
        whatsapp_message_id: str | None = None,
    ) -> ConfirmedPurchase:
        org_id = actor.org_id
        async with self._session.begin():
            today = await business_today(self._session, org_id)
            self._validate(draft, today)
            await self._check_total_mismatch(org_id, draft)
            await self._check_duplicates(org_id, draft, override_duplicate)

            if draft.total_resolution == "invoice" and draft.declared_total is not None:
                # difference booked as other_charges, visibly -- §5
                draft.other_charges += draft.declared_total - draft.grand_total

            warehouse = await self._default_warehouse(org_id)
            freight_shares = allocate(draft.freight, [line.qty for line in draft.lines])
            other_shares = allocate(draft.other_charges, [line.line_total for line in draft.lines])

            header = PurchaseHeader(
                org_id=org_id,
                supplier_id=draft.supplier_id,
                brand_id=draft.brand_id,
                warehouse_id=warehouse.id,
                invoice_no=draft.invoice_no,
                invoice_date=draft.invoice_date,
                ocr_source_attachment_id=draft.source_attachment_id,
                freight=draft.freight,
                other_charges=draft.other_charges,
                subtotal=draft.subtotal,
                grand_total=draft.grand_total,
                declared_total=draft.declared_total,
                status=PurchaseStatus.CONFIRMED,
                notes=(
                    "reconciled against declared invoice total"
                    if draft.total_resolution == "invoice"
                    else None
                ),
                created_by=actor.id,
            )
            self._session.add(header)
            try:
                await self._session.flush()
            except IntegrityError as exc:
                # concurrent confirm race -- §10
                raise ExactDuplicateInvoiceError(
                    draft.invoice_no, draft.supplier_name, details={}
                ) from exc

            confirmed_lines: list[ConfirmedLine] = []
            for index, line in enumerate(draft.lines):
                assert line.product_id is not None
                landed = (
                    (line.line_total + freight_shares[index] + other_shares[index]) / line.qty
                ).quantize(FOUR)
                row = PurchaseLine(
                    org_id=org_id,
                    purchase_header_id=header.id,
                    line_no=index + 1,
                    product_id=line.product_id,
                    description=line.description,
                    qty=line.qty,
                    weight_kg=line.weight_per_unit,
                    total_weight_kg=line.qty if line.weight_per_unit is not None else None,
                    rate=line.rate,
                    line_total=line.line_total,
                    freight_allocated=freight_shares[index],
                    landed_cost_per_unit=landed,
                )
                self._session.add(row)
                await self._session.flush()
                movement = await self._inventory.record_purchase_movement(
                    org_id,
                    product_id=line.product_id,
                    warehouse_id=warehouse.id,
                    qty=line.qty,
                    landed_cost_per_unit=landed,
                    source_id=row.id,
                    created_by=actor.id,
                )
                confirmed_lines.append(
                    ConfirmedLine(
                        code=line.resolved_code or line.code,
                        qty=line.qty,
                        rate=line.rate,
                        line_total=line.line_total,
                        landed_cost_per_unit=landed,
                        unit_code=line.unit_code or "KG",
                        resulting_qty=movement.resulting_qty_on_hand,
                        resulting_avg_cost=movement.resulting_avg_cost,
                    )
                )

            await self._journal.post(
                org_id,
                entry_date=today,
                description=f"purchase {draft.invoice_no} from {draft.supplier_name}",
                source_type="purchase_header",
                source_id=header.id,
                created_by=actor.id,
                debits=[(AccountCode.INVENTORY, draft.grand_total)],
                credits=[(AccountCode.ACCOUNTS_PAYABLE, draft.grand_total)],
            )
            await self._audit.record(
                org_id,
                actor.id,
                action="purchase.confirmed",
                entity_type="purchase_headers",
                entity_id=header.id,
                after_state={
                    "invoice_no": draft.invoice_no,
                    "supplier_id": str(draft.supplier_id),
                    "grand_total": str(draft.grand_total),
                    "lines": len(draft.lines),
                },
                whatsapp_message_id=whatsapp_message_id,
            )

        return ConfirmedPurchase(
            header_id=header.id,
            supplier_name=draft.supplier_name,
            invoice_no=draft.invoice_no,
            invoice_date=draft.invoice_date,
            lines=confirmed_lines,
            subtotal=draft.subtotal,
            freight=draft.freight,
            other_charges=draft.other_charges,
            grand_total=draft.grand_total,
        )
