"""One person on both sides of the book -- docs/32_LinkedParties.md.

Afzal sells us fabric and buys fabric from us. A purchase must name a
supplier and a sale must name a customer, so he is necessarily two rows;
what was missing was any statement that the two rows are one man. The
consequence was not cosmetic: the books carried a payable of 5,00,000
and a receivable of 98,000 as unrelated balances, so "what is our
position with Afzal" -- the only number either partner asks about him --
could not be answered by the system at all.

Two operations live here.

**Linking** records the fact. It changes no money; it makes the net
position computable and the combined statement possible.

**Setting off** acts on it: the smaller side is cleared against the
larger, invoice by invoice, with a contra journal entry. No cash moves,
which is exactly why it needs a journal entry -- a payable that shrank
with no payment behind it is otherwise unexplainable at year end.

Set-off is deliberately an explicit act rather than something that
happens quietly inside `pay`. Netting a receivable against a payable is
an agreement between two businesses, not an arithmetic convenience, and
the day it is agreed is the day it belongs in the books.
"""

from __future__ import annotations

import dataclasses
import decimal
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.db import joined_transaction
from backend.core.exceptions import NotFoundError, ValidationError
from backend.models import Customer, PurchaseHeader, SalesHeader, Supplier, User
from backend.models.enums import AccountCode
from backend.repositories.accounting_repository import entry_day
from backend.repositories.party_repository import (
    OPEN_SALE_STATUSES,
    CustomerRepository,
    SupplierRepository,
)
from backend.services.audit_service import AuditService
from backend.services.journal_service import JournalService

ZERO = decimal.Decimal("0")
TWO = decimal.Decimal("0.01")


@dataclasses.dataclass(frozen=True)
class NetPosition:
    """Where we stand with someone who trades both ways."""

    supplier_id: uuid.UUID
    customer_id: uuid.UUID | None
    name: str
    #: What we owe them on purchases.
    payable: decimal.Decimal
    #: What they owe us on sales.
    receivable: decimal.Decimal

    @property
    def net(self) -> decimal.Decimal:
        """Positive: we owe them. Negative: they owe us."""
        return (self.payable - self.receivable).quantize(TWO)

    @property
    def offsettable(self) -> decimal.Decimal:
        """The most that could be cleared against itself right now."""
        return min(self.payable, self.receivable).quantize(TWO)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "payable": str(self.payable),
            "receivable": str(self.receivable),
            "net": str(self.net),
            "we_owe": self.net > ZERO,
            "offsettable": str(self.offsettable),
        }


@dataclasses.dataclass(frozen=True)
class SetOffResult:
    party_name: str
    amount: decimal.Decimal
    purchases: list[tuple[str, decimal.Decimal]]
    sales: list[tuple[str, decimal.Decimal]]
    payable_after: decimal.Decimal
    receivable_after: decimal.Decimal
    reference: str


class PartyLinkService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._suppliers = SupplierRepository(session)
        self._customers = CustomerRepository(session)
        self._journal = JournalService(session)
        self._audit = AuditService(session)

    # -- resolving ------------------------------------------------------

    async def _supplier(self, org_id: uuid.UUID, name: str) -> Supplier:
        matches = await self._suppliers.search(org_id, name, limit=1)
        if not matches:
            raise NotFoundError("supplier", name)
        return matches[0]

    async def _customer(self, org_id: uuid.UUID, name: str) -> Customer:
        matches = await self._customers.search(org_id, name, limit=1)
        if not matches:
            raise NotFoundError("customer", name)
        return matches[0]

    async def linked_customer_id(
        self, org_id: uuid.UUID, supplier_id: uuid.UUID
    ) -> uuid.UUID | None:
        return (
            await self._session.execute(
                select(Supplier.linked_customer_id).where(
                    Supplier.org_id == org_id,
                    Supplier.id == supplier_id,
                    Supplier.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()

    async def linked_supplier_id(
        self, org_id: uuid.UUID, customer_id: uuid.UUID
    ) -> uuid.UUID | None:
        return (
            await self._session.execute(
                select(Supplier.id).where(
                    Supplier.org_id == org_id,
                    Supplier.linked_customer_id == customer_id,
                    Supplier.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()

    # -- linking --------------------------------------------------------

    async def link(self, actor: User, *, supplier_name: str, customer_name: str) -> NetPosition:
        """Declare that a supplier row and a customer row are one person."""
        org_id = actor.org_id
        async with joined_transaction(self._session):
            supplier = await self._supplier(org_id, supplier_name)
            customer = await self._customer(org_id, customer_name)

            if supplier.linked_customer_id == customer.id:
                return await self.position(org_id, supplier.id)
            if supplier.linked_customer_id is not None:
                other = await self._session.get(Customer, supplier.linked_customer_id)
                raise ValidationError(
                    f"{supplier.name} is already linked to "
                    f"{other.name if other else 'another customer'}. Unlink first."
                )
            taken = await self.linked_supplier_id(org_id, customer.id)
            if taken is not None:
                held = await self._session.get(Supplier, taken)
                raise ValidationError(
                    f"{customer.name} is already linked to "
                    f"{held.name if held else 'another supplier'}. Unlink first."
                )

            before = supplier.linked_customer_id
            supplier.linked_customer_id = customer.id
            await self._audit.record(
                org_id,
                actor.id,
                action="party.linked",
                entity_type="suppliers",
                entity_id=supplier.id,
                before_state={"linked_customer_id": str(before) if before else None},
                after_state={
                    "linked_customer_id": str(customer.id),
                    "customer_name": customer.name,
                },
            )
            return await self.position(org_id, supplier.id)

    async def unlink(self, actor: User, *, supplier_name: str) -> str:
        org_id = actor.org_id
        async with joined_transaction(self._session):
            supplier = await self._supplier(org_id, supplier_name)
            if supplier.linked_customer_id is None:
                raise ValidationError(f"{supplier.name} is not linked to a customer.")
            before = supplier.linked_customer_id
            supplier.linked_customer_id = None
            await self._audit.record(
                org_id,
                actor.id,
                action="party.unlinked",
                entity_type="suppliers",
                entity_id=supplier.id,
                before_state={"linked_customer_id": str(before)},
                after_state={"linked_customer_id": None},
            )
            return supplier.name

    # -- where we stand -------------------------------------------------

    async def position(self, org_id: uuid.UUID, supplier_id: uuid.UUID) -> NetPosition:
        supplier = await self._session.get(Supplier, supplier_id)
        if supplier is None:
            raise NotFoundError("supplier", str(supplier_id))
        payable = await self._suppliers.outstanding(org_id, supplier_id)
        receivable = ZERO
        if supplier.linked_customer_id is not None:
            receivable = await self._customers.outstanding(org_id, supplier.linked_customer_id)
        return NetPosition(
            supplier_id=supplier_id,
            customer_id=supplier.linked_customer_id,
            name=supplier.name,
            payable=payable,
            receivable=receivable,
        )

    async def all_links(self, org_id: uuid.UUID) -> list[NetPosition]:
        rows = list(
            (
                await self._session.execute(
                    select(Supplier).where(
                        Supplier.org_id == org_id,
                        Supplier.linked_customer_id.is_not(None),
                        Supplier.deleted_at.is_(None),
                    )
                )
            ).scalars()
        )
        return [await self.position(org_id, row.id) for row in rows]

    # -- setting one side off against the other -------------------------

    async def set_off(
        self,
        actor: User,
        *,
        supplier_name: str,
        amount: decimal.Decimal | None = None,
        on: str | None = None,
        note: str | None = None,
    ) -> SetOffResult:
        """Clear what they owe us against what we owe them.

        `amount` left out means the whole overlap -- the usual case,
        since a set-off that leaves a sliver behind is a set-off someone
        has to remember to finish.
        """
        org_id = actor.org_id
        async with joined_transaction(self._session):
            supplier = await self._supplier(org_id, supplier_name)
            if supplier.linked_customer_id is None:
                raise ValidationError(
                    f"{supplier.name} is not linked to a customer, so there is nothing "
                    "to set off. Link the two names first."
                )
            customer_id = supplier.linked_customer_id
            when = await entry_day(self._session, org_id, on)

            position = await self.position(org_id, supplier.id)
            available = position.offsettable
            if available <= ZERO:
                raise ValidationError(
                    f"Nothing to set off: {supplier.name} is owed "
                    f"₹{position.payable} and owes ₹{position.receivable}."
                )
            if amount is None:
                amount = available
            amount = amount.quantize(TWO)
            if amount <= ZERO:
                raise ValidationError("Amount must be greater than zero.")
            if amount > available:
                raise ValidationError(
                    f"Only ₹{available} can be set off — that is the smaller of the "
                    f"₹{position.payable} we owe and the ₹{position.receivable} owed to us."
                )

            purchases = await self._open_purchases(org_id, supplier.id)
            sales = await self._open_sales(org_id, customer_id)
            # Both sides move by the same amount: that is what makes it
            # a set-off rather than two unrelated write-offs.
            applied_purchases = self._apply(purchases, amount)
            applied_sales = self._apply(sales, amount)

            await self._journal.post(
                org_id,
                entry_date=when,
                description=f"set-off against {supplier.name}" + (f" — {note}" if note else ""),
                source_type="party_setoff",
                source_id=supplier.id,
                created_by=actor.id,
                # The payable falls and the receivable falls with it. No
                # cash account is touched, because no cash moved.
                debits=[(AccountCode.ACCOUNTS_PAYABLE, amount)],
                credits=[(AccountCode.ACCOUNTS_RECEIVABLE, amount)],
            )
            entry = await self._audit.record(
                org_id,
                actor.id,
                action="party.setoff",
                entity_type="suppliers",
                entity_id=supplier.id,
                after_state={
                    "amount": str(amount),
                    "entry_date": when.isoformat(),
                    "customer_id": str(customer_id),
                    "note": note,
                    "purchases": [
                        {"reference": ref, "applied": str(value)}
                        for ref, value in applied_purchases
                    ],
                    "sales": [
                        {"reference": ref, "applied": str(value)} for ref, value in applied_sales
                    ],
                },
            )
            payable_after = await self._suppliers.outstanding(org_id, supplier.id)
            receivable_after = await self._customers.outstanding(org_id, customer_id)

        return SetOffResult(
            party_name=supplier.name,
            amount=amount,
            purchases=applied_purchases,
            sales=applied_sales,
            payable_after=payable_after,
            receivable_after=receivable_after,
            reference=str(entry.id)[:8],
        )

    async def _open_purchases(
        self, org_id: uuid.UUID, supplier_id: uuid.UUID
    ) -> list[PurchaseHeader]:
        return list(
            (
                await self._session.execute(
                    select(PurchaseHeader)
                    .where(
                        PurchaseHeader.org_id == org_id,
                        PurchaseHeader.supplier_id == supplier_id,
                        PurchaseHeader.deleted_at.is_(None),
                        PurchaseHeader.status == "confirmed",
                        PurchaseHeader.grand_total > PurchaseHeader.amount_paid,
                    )
                    .order_by(PurchaseHeader.invoice_date, PurchaseHeader.created_at)
                )
            ).scalars()
        )

    async def _open_sales(self, org_id: uuid.UUID, customer_id: uuid.UUID) -> list[SalesHeader]:
        return list(
            (
                await self._session.execute(
                    select(SalesHeader)
                    .where(
                        SalesHeader.org_id == org_id,
                        SalesHeader.customer_id == customer_id,
                        SalesHeader.deleted_at.is_(None),
                        SalesHeader.status.in_(OPEN_SALE_STATUSES),
                        SalesHeader.grand_total > SalesHeader.amount_paid,
                    )
                    .order_by(SalesHeader.sale_date, SalesHeader.created_at)
                )
            ).scalars()
        )

    @staticmethod
    def _apply(
        headers: list[PurchaseHeader] | list[SalesHeader], amount: decimal.Decimal
    ) -> list[tuple[str, decimal.Decimal]]:
        """Oldest first, same rule a payment follows."""
        remaining = amount
        applied: list[tuple[str, decimal.Decimal]] = []
        for header in headers:
            if remaining <= ZERO:
                break
            open_amount = header.grand_total - header.amount_paid
            take = min(open_amount, remaining)
            header.amount_paid = (header.amount_paid + take).quantize(TWO)
            header.payment_status = (
                "paid" if header.amount_paid >= header.grand_total else "partial"
            )
            remaining -= take
            reference = getattr(header, "invoice_no", None) or str(header.id)[:8]
            applied.append((reference, take))
        return applied


@dataclasses.dataclass(frozen=True)
class NettedBalance:
    """A party's balance after the other side of the book is taken off.

    `gross` is kept beside `outstanding` because "we owe Afzal 4,02,000"
    and "we owe Afzal 5,00,000, less the 98,000 he owes us" are answers
    to different questions, and a screen that shows only the net invites
    the reconciliation call that starts "but your own bill says".
    """

    party_id: uuid.UUID
    name: str
    outstanding: decimal.Decimal
    oldest_date: Any
    gross: decimal.Decimal
    #: How much of the other side was taken off. Zero for the ordinary
    #: party who only ever trades one way.
    offset: decimal.Decimal


async def net_against_links(
    session: AsyncSession,
    org_id: uuid.UUID,
    rows: list[Any],
    *,
    role: str,
) -> list[NettedBalance]:
    """Take the other side of the book off a receivables/payables list.

    A linked party appears on both lists gross, which double-counts the
    same person in two directions and makes the totals read as more
    money in play than there is. Netted, they appear once, on whichever
    side they are actually on, and drop off entirely when the two sides
    cancel.
    """
    links = list(
        (
            await session.execute(
                select(Supplier.id, Supplier.linked_customer_id).where(
                    Supplier.org_id == org_id,
                    Supplier.linked_customer_id.is_not(None),
                    Supplier.deleted_at.is_(None),
                )
            )
        ).all()
    )
    if not links:
        return [
            NettedBalance(r.party_id, r.name, r.outstanding, r.oldest_date, r.outstanding, ZERO)
            for r in rows
        ]

    suppliers = SupplierRepository(session)
    customers = CustomerRepository(session)
    #: party on this list -> the same person's id on the other list
    other: dict[uuid.UUID, uuid.UUID] = {
        (supplier_id if role == "supplier" else customer_id): (
            customer_id if role == "supplier" else supplier_id
        )
        for supplier_id, customer_id in links
    }

    netted: list[NettedBalance] = []
    for row in rows:
        counterpart = other.get(row.party_id)
        if counterpart is None:
            netted.append(
                NettedBalance(
                    row.party_id, row.name, row.outstanding, row.oldest_date, row.outstanding, ZERO
                )
            )
            continue
        opposite = (
            await customers.outstanding(org_id, counterpart)
            if role == "supplier"
            else await suppliers.outstanding(org_id, counterpart)
        )
        net = (row.outstanding - opposite).quantize(TWO)
        if net <= ZERO:
            # They are on the other side of the book now, or square.
            # Listing them here at zero reads as a party we are still
            # chasing.
            continue
        netted.append(
            NettedBalance(
                row.party_id,
                row.name,
                net,
                row.oldest_date,
                row.outstanding,
                min(opposite, row.outstanding).quantize(TWO),
            )
        )
    netted.sort(key=lambda b: b.outstanding, reverse=True)
    return netted
