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


@dataclasses.dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: str  # 'text' | 'number' | 'money'

    def parse(self, raw: str) -> object:
        text = raw.strip()
        if self.kind == "text":
            if not text:
                raise ValidationError(f"'{self.name}' can't be empty.")
            return text
        try:
            value = decimal.Decimal(text)
        except decimal.InvalidOperation:
            raise ValidationError(f"'{self.name}' expects a number.") from None
        if value < ZERO:
            raise ValidationError(f"'{self.name}' can't be negative.")
        return value


#: What may be changed, per entity. Deliberately a short allow-list
#: rather than "any column": `code` is absent because inventory,
#: movements and OCR learning all key off it, and renaming it after the
#: fact would silently orphan them.
EDITABLE: dict[str, dict[str, FieldSpec]] = {
    "product": {
        "description": FieldSpec("description", "text"),
        "reorder_level": FieldSpec("reorder_level", "number"),
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
        model = _MODELS[entity]
        column = model.code if entity == "product" else model.name  # type: ignore[union-attr]
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

    def _guard_transaction(self, entity: str) -> None:
        if entity in TRANSACTION_ENTITIES:
            raise RoutedToUndo(
                f"A confirmed {entity} is never changed in place — the stock and books "
                f"were already derived from it. Use 'undo {entity} <ref>' and re-enter it."
            )

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
            before = getattr(row, spec.name)
            setattr(row, spec.name, parsed)
            await self._audit.record(
                actor.org_id,
                actor.id,
                action=f"{entity}.edited",
                entity_type=f"{entity}s",
                entity_id=row.id,
                before_state={spec.name: str(before) if before is not None else None},
                after_state={spec.name: str(parsed)},
                whatsapp_message_id=whatsapp_message_id,
            )
        return EditResult(
            entity=entity,
            reference=reference,
            field=spec.name,
            before=str(before) if before is not None else "(unset)",
            after=str(parsed),
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
