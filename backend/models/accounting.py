"""expenses, income, cash_ledger, bank_ledger, partner_capital, journal,
journal_lines -- docs/02_Database.md §3.15-3.17.

cash_ledger and bank_ledger are append-only and partitioned by month (§9),
same as inventory_movements -- composite (id, created_at) PK because the
partition key must be part of the primary key.
"""

from __future__ import annotations

import datetime
import decimal
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, declared_attr, mapped_column, relationship

from backend.models.base import MONEY, Base
from backend.models.enums import (
    CapitalEntryType,
    LedgerEntryType,
    capital_entry_type_enum,
    ledger_entry_type_enum,
)
from backend.models.mixins import OrgScopedMixin, UUIDPkMixin


class Expense(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "expenses"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("paid_via IN ('cash','bank')", name="paid_via_valid"),
    )

    category: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[decimal.Decimal] = mapped_column(MONEY, nullable=False)
    paid_via: Mapped[str] = mapped_column(String, nullable=False)
    paid_by_partner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("partners.id")
    )
    expense_date: Mapped[datetime.date] = mapped_column(
        nullable=False, server_default=text("CURRENT_DATE")
    )
    description: Mapped[str | None] = mapped_column(String)
    attachment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("attachments.id")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    deleted_at: Mapped[datetime.datetime | None]


class Income(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "income"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("received_via IN ('cash','bank')", name="received_via_valid"),
    )

    category: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[decimal.Decimal] = mapped_column(MONEY, nullable=False)
    received_via: Mapped[str] = mapped_column(String, nullable=False)
    income_date: Mapped[datetime.date] = mapped_column(
        nullable=False, server_default=text("CURRENT_DATE")
    )
    description: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    deleted_at: Mapped[datetime.datetime | None]


class _LedgerColumns(UUIDPkMixin, OrgScopedMixin):
    """Shared shape of cash_ledger / bank_ledger (§3.15) -- append-only,
    signed amount, running balance snapshot."""

    entry_type: Mapped[LedgerEntryType] = mapped_column(ledger_entry_type_enum, nullable=False)
    amount: Mapped[decimal.Decimal] = mapped_column(MONEY, nullable=False)
    resulting_balance: Mapped[decimal.Decimal] = mapped_column(MONEY, nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    entry_date: Mapped[datetime.date] = mapped_column(
        nullable=False, server_default=text("CURRENT_DATE")
    )
    notes: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(
        primary_key=True, server_default=text("now()")
    )

    @declared_attr
    def created_by(cls) -> Mapped[uuid.UUID]:  # noqa: N805 -- SQLAlchemy declared_attr convention
        return mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)


class CashLedger(_LedgerColumns, Base):
    __tablename__ = "cash_ledger"
    __table_args__ = (
        Index("idx_cash_ledger_org_date", "org_id", "entry_date"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )


class BankLedger(_LedgerColumns, Base):
    __tablename__ = "bank_ledger"
    __table_args__ = (
        Index("idx_bank_ledger_org_date", "org_id", "entry_date"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )


class PartnerCapital(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "partner_capital"
    __table_args__ = (CheckConstraint("settled_via IN ('cash','bank')", name="settled_via_valid"),)

    partner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("partners.id"), nullable=False
    )
    entry_type: Mapped[CapitalEntryType] = mapped_column(capital_entry_type_enum, nullable=False)
    amount: Mapped[decimal.Decimal] = mapped_column(MONEY, nullable=False)
    resulting_balance: Mapped[decimal.Decimal] = mapped_column(MONEY, nullable=False)
    settled_via: Mapped[str | None] = mapped_column(String)
    entry_date: Mapped[datetime.date] = mapped_column(
        nullable=False, server_default=text("CURRENT_DATE")
    )
    notes: Mapped[str | None] = mapped_column(String)
    # docs/06_Accounting.md#dual-approval-withdrawals
    approved_by_partner_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )


class Journal(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "journal"

    entry_date: Mapped[datetime.date] = mapped_column(nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )

    lines: Mapped[list[JournalLine]] = relationship(back_populates="journal")


class JournalLine(UUIDPkMixin, Base):
    """Pure child of journal -- deliberately no org_id (§3.17)."""

    __tablename__ = "journal_lines"
    __table_args__ = (
        CheckConstraint("debit >= 0", name="debit_non_negative"),
        CheckConstraint("credit >= 0", name="credit_non_negative"),
        CheckConstraint("(debit = 0) <> (credit = 0)", name="exactly_one_side"),
    )

    journal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("journal.id"), nullable=False
    )
    account_code: Mapped[str] = mapped_column(String, nullable=False)
    debit: Mapped[decimal.Decimal] = mapped_column(MONEY, nullable=False, server_default=text("0"))
    credit: Mapped[decimal.Decimal] = mapped_column(MONEY, nullable=False, server_default=text("0"))

    journal: Mapped[Journal] = relationship(back_populates="lines")
