"""`edit` and `delete` -- docs/08_WhatsApp.md #edit / #delete,
docs/04_Purchases.md §8, docs/02_Database.md §4.

The line this module exists to hold: **confirmed financial history is
never edited or deleted in place.** Inventory movements and the
weighted average have already been derived from a confirmed purchase,
so changing it after the fact would leave the books describing
something that never happened. Corrections go through `undo` plus
re-entry, and `delete` on a confirmed transaction is routed there
rather than being refused with a dead end.

Master data (products, suppliers, customers, brands) *is* editable,
because nothing is derived from its fields -- and deletable, as a soft
delete that leaves history intact.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import NotFoundError, ValidationError
from backend.models import Brand, Customer, Product, PurchaseHeader, SalesHeader, Supplier, User
from backend.services.audit_service import AuditService

ZERO = decimal.Decimal("0")


class RoutedToUndo(ValidationError):
    """`delete`/`edit` on a confirmed transaction. Not a failure -- the
    caller turns this into the undo suggestion."""

    code = "routed_to_undo"


#: What the user may type to mean "no brand at all".
_UNBRANDED = {"none", "no", "-", "unbranded", "not branded", "no brand", "blank"}


@dataclasses.dataclass(frozen=True)
class FieldSpec:
    name: str  # the column on the model
    kind: str  # 'text' | 'number' | 'money' | 'code' | 'brand'
    label: str = ""  # what the user calls it, when that differs

    @property
    def title(self) -> str:
        return self.label or self.name

    def parse(self, raw: str) -> object:
        text = raw.strip()
        if self.kind == "code":
            # Codes are compared case-insensitively everywhere (the
            # uniqueness index is on upper(code)), so store them the way
            # they are matched rather than the way they were typed.
            code = " ".join(text.split()).upper()
            if not code:
                raise ValidationError("A product code can't be empty.")
            return code
        if self.kind in {"text", "brand"}:
            if not text:
                raise ValidationError(f"'{self.title}' can't be empty.")
            return text
        try:
            value = decimal.Decimal(text)
        except decimal.InvalidOperation:
            raise ValidationError(f"'{self.title}' expects a number.") from None
        if value < ZERO:
            raise ValidationError(f"'{self.title}' can't be negative.")
        return value


#: What may be changed, per entity. Deliberately a short allow-list
#: rather than "any column".
#:
#: `code` and `brand` are here despite renaming being the riskier kind
#: of edit, because a code read wrongly off a sheet (44P for 44D) is
#: exactly the mistake this system exists to let people fix. Nothing
#: derived keys off the *string*: inventory, movements and purchase
#: lines all hold `product_id`. The one thing that does is the OCR
#: learning dictionary, which stores the code it learned -- so a rename
#: carries those entries with it (see `_remap_learned_codes`).
EDITABLE: dict[str, dict[str, FieldSpec]] = {
    "product": {
        "code": FieldSpec("code", "code"),
        "description": FieldSpec("description", "text"),
        "reorder_level": FieldSpec("reorder_level", "number"),
        "brand": FieldSpec("brand_id", "brand", label="brand"),
    },
    "supplier": {
        "name": FieldSpec("name", "text"),
        "phone": FieldSpec("phone", "text"),
        "address": FieldSpec("address", "text"),
        "gst_number": FieldSpec("gst_number", "text"),
    },
    "customer": {
        "name": FieldSpec("name", "text"),
        "phone": FieldSpec("phone", "text"),
        "address": FieldSpec("address", "text"),
        "gst_number": FieldSpec("gst_number", "text"),
        "credit_limit": FieldSpec("credit_limit", "money"),
    },
    "brand": {"name": FieldSpec("name", "text")},
}

_MODELS: dict[str, type[Product] | type[Supplier] | type[Customer] | type[Brand]] = {
    "product": Product,
    "supplier": Supplier,
    "customer": Customer,
    "brand": Brand,
}

#: Entities whose "edit"/"delete" means undo instead.
TRANSACTION_ENTITIES = {"purchase", "sale"}


@dataclasses.dataclass(frozen=True)
class EditResult:
    entity: str
    reference: str
    field: str
    before: str
    after: str


@dataclasses.dataclass(frozen=True)
class DeleteResult:
    entity: str
    reference: str
    name: str


class EditService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def _find(self, org_id: uuid.UUID, entity: str, reference: str) -> Any:
        if entity == "product":
            return await self._find_product(org_id, reference)
        model = _MODELS[entity]
        column = model.name  # type: ignore[union-attr]
        row = (
            (
                await self._session.execute(
                    select(model).where(
                        model.org_id == org_id,
                        column.ilike(reference),
                        model.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            raise NotFoundError(entity, reference)
        return row

    async def _find_product(self, org_id: uuid.UUID, reference: str) -> Product:
        """A code is only unique *within* a brand, so "VVP" may name two
        products. Rather than editing whichever one came back first --
        which would silently rename the wrong brand's product -- an
        ambiguous code asks, and accepts "VVP TOP" as the answer."""
        reference = " ".join(reference.split())
        matches = await self._products_by_code(org_id, reference)
        if not matches:
            # "44P TOP" -- the code, then the brand that disambiguates it.
            code, _, brand_name = reference.partition(" ")
            if brand_name:
                matches = [
                    product
                    for product in await self._products_by_code(org_id, code)
                    if product.brand is not None
                    and product.brand.name.lower() == brand_name.strip().lower()
                ]
        if not matches:
            raise NotFoundError("product", reference)
        if len(matches) > 1:
            brands = ", ".join(
                sorted(p.brand.name if p.brand is not None else "no brand" for p in matches)
            )
            raise ValidationError(
                f"{matches[0].code} exists under {len(matches)} brands ({brands}). "
                f"Say which one — e.g. '{matches[0].code} {brands.split(', ')[0]}'."
            )
        return matches[0]

    async def _products_by_code(self, org_id: uuid.UUID, code: str) -> list[Product]:
        from sqlalchemy import func
        from sqlalchemy.orm import selectinload

        return list(
            (
                await self._session.execute(
                    select(Product)
                    .options(selectinload(Product.brand))
                    .where(
                        Product.org_id == org_id,
                        func.upper(Product.code) == code.upper(),
                        Product.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )

    async def _guard_code_free(
        self, product: Product, code: str, brand_id: uuid.UUID | None
    ) -> None:
        """`products_org_code_active_uq` would refuse this anyway, but as
        an IntegrityError with no explanation of which record clashed."""
        clash = next(
            (
                other
                for other in await self._products_by_code(product.org_id, code)
                if other.id != product.id and other.brand_id == brand_id
            ),
            None,
        )
        if clash is None:
            return
        where = clash.brand.name if clash.brand is not None else "no brand"
        raise ValidationError(
            f"{code} is already used by '{clash.description}' under {where}. "
            "Two products can't share a code within one brand."
        )

    async def _remap_learned_codes(self, org_id: uuid.UUID, old: str, new: str) -> None:
        """A learned OCR correction stores the code it resolved to, so a
        rename would leave the dictionary pointing at a code that no
        longer exists. Only remap when *no* product still answers to the
        old code — if another brand still uses it, those entries are
        still right for that product."""
        from sqlalchemy import func, update

        from backend.models import OcrLearningDictionary

        if await self._products_by_code(org_id, old):
            return
        await self._session.execute(
            update(OcrLearningDictionary)
            .where(
                OcrLearningDictionary.org_id == org_id,
                OcrLearningDictionary.field == "code",
                func.upper(OcrLearningDictionary.corrected_value) == old.upper(),
            )
            .values(corrected_value=new, updated_at=func.now())
        )

    async def _resolve_brand(self, org_id: uuid.UUID, name: str) -> Brand | None:
        from sqlalchemy import func

        if name.strip().lower() in _UNBRANDED:
            return None
        brand = (
            (
                await self._session.execute(
                    select(Brand).where(
                        Brand.org_id == org_id,
                        func.lower(Brand.name) == name.strip().lower(),
                        Brand.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .first()
        )
        if brand is None:
            raise NotFoundError("brand", name)
        return brand

    def _guard_transaction(self, entity: str) -> None:
        """Confirmed history is corrected, never rewritten.

        The refusal names the cheaper routes first. Told only to undo and
        re-enter, someone with one wrong price on a twelve-line bill will
        retype the whole thing -- and `rate` and `receive` exist precisely
        so they don't have to. Undo is the answer for a bill that was
        wrong when it was entered, not for the two things that change
        *after* a bill is correct.
        """
        if entity not in TRANSACTION_ENTITIES:
            return
        opening = (
            f"A confirmed {entity} is never changed in place — stock and the average "
            "cost were already worked out from it."
        )
        if entity == "purchase":
            raise RoutedToUndo(
                f"{opening}\n"
                "• Price agreed later or billed wrong — *rate <invoice> <new rate>*\n"
                "• Fewer arrived than billed — *receive <invoice> <CODE> <bales>*\n"
                f"• Anything else — *undo purchase <ref>*, then enter it again."
            )
        raise RoutedToUndo(f"{opening}\nUse *undo {entity} <ref>* and enter it again.")

    async def edit(
        self,
        actor: User,
        *,
        entity: str,
        reference: str,
        field: str,
        value: str,
        whatsapp_message_id: str | None = None,
    ) -> EditResult:
        entity = entity.strip().lower()
        self._guard_transaction(entity)
        if entity not in EDITABLE:
            raise ValidationError(
                f"'{entity}' can't be edited. Editable: {', '.join(sorted(EDITABLE))}."
            )
        fields = EDITABLE[entity]
        spec = fields.get(field.strip().lower())
        if spec is None:
            raise ValidationError(
                f"'{field}' isn't editable on a {entity}. Try: {', '.join(sorted(fields))}."
            )
        parsed = spec.parse(value)

        async with self._session.begin():
            row = await self._find(actor.org_id, entity, reference)
            if spec.kind == "code":
                before, shown = row.code, str(parsed)
                await self._guard_code_free(row, shown, row.brand_id)
                row.code = shown
                await self._remap_learned_codes(actor.org_id, str(before), shown)
            elif spec.kind == "brand":
                brand = await self._resolve_brand(actor.org_id, str(parsed))
                before = row.brand.name if row.brand is not None else None
                shown = brand.name if brand is not None else "no brand"
                await self._guard_code_free(row, row.code, brand.id if brand is not None else None)
                row.brand_id = brand.id if brand is not None else None
            else:
                before = getattr(row, spec.name)
                shown = str(parsed)
                setattr(row, spec.name, parsed)
            await self._audit.record(
                actor.org_id,
                actor.id,
                action=f"{entity}.edited",
                entity_type=f"{entity}s",
                entity_id=row.id,
                before_state={spec.title: str(before) if before is not None else None},
                after_state={spec.title: shown},
                whatsapp_message_id=whatsapp_message_id,
            )
        return EditResult(
            entity=entity,
            reference=reference,
            field=spec.title,
            before=str(before) if before is not None else "(unset)",
            after=shown,
        )

    async def delete(
        self,
        actor: User,
        *,
        entity: str,
        reference: str,
        whatsapp_message_id: str | None = None,
    ) -> DeleteResult:
        entity = entity.strip().lower()
        self._guard_transaction(entity)
        if entity not in _MODELS:
            raise ValidationError(
                f"'{entity}' can't be deleted. Deletable: {', '.join(sorted(_MODELS))}."
            )

        async with self._session.begin():
            row = await self._find(actor.org_id, entity, reference)
            await self._check_no_history(entity, row)
            # soft delete only -- docs/02_Database.md §4
            row.deleted_at = datetime.datetime.now(datetime.UTC)
            label = row.code if entity == "product" else row.name
            await self._audit.record(
                actor.org_id,
                actor.id,
                action=f"{entity}.deleted",
                entity_type=f"{entity}s",
                entity_id=row.id,
                before_state={"name": label, "deleted_at": None},
                after_state={"deleted_at": row.deleted_at.isoformat()},
                whatsapp_message_id=whatsapp_message_id,
            )
        return DeleteResult(entity=entity, reference=reference, name=label)

    async def _check_no_history(self, entity: str, row: Any) -> None:
        """A party with open money against them can't be filed away --
        the balance would vanish from `dashboard` while still being owed.
        Soft-deleting is reversible, but a silently-disappearing
        receivable is exactly the kind of quiet wrongness this project
        avoids elsewhere."""
        from backend.repositories.party_repository import CustomerRepository, SupplierRepository

        if entity == "customer":
            outstanding = await CustomerRepository(self._session).outstanding(row.org_id, row.id)
            if outstanding != ZERO:
                raise ValidationError(
                    f"{row.name} still has {outstanding} outstanding — settle it first, "
                    "or that balance would disappear from your books."
                )
        elif entity == "supplier":
            outstanding = await SupplierRepository(self._session).outstanding(row.org_id, row.id)
            if outstanding != ZERO:
                raise ValidationError(
                    f"{row.name} is still owed {outstanding} — settle it first, "
                    "or that balance would disappear from your books."
                )
        elif entity == "product":
            from backend.models import Inventory

            qty = (
                (
                    await self._session.execute(
                        select(Inventory.qty_on_hand).where(Inventory.product_id == row.id)
                    )
                )
                .scalars()
                .first()
            )
            if qty is not None and qty != ZERO:
                raise ValidationError(
                    f"{row.code} still has {qty} in stock — clear or adjust it first."
                )


async def transaction_exists(session: AsyncSession, org_id: uuid.UUID, entity: str) -> bool:
    """Used only by the command layer's phrasing, so `delete purchase X`
    can point at undo with a reference it knows resolves."""
    model = PurchaseHeader if entity == "purchase" else SalesHeader
    return (
        await session.execute(select(model.id).where(model.org_id == org_id).limit(1))
    ).scalars().first() is not None
