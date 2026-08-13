"""Python enums mirroring the Postgres ENUM types in docs/02_Database.md.

Each is mapped via SQLAlchemy's `Enum(..., native_enum=True)`, which
creates (and, in later migrations, alters via `ALTER TYPE ... ADD
VALUE`) a real Postgres enum type -- not a CHECK-constrained VARCHAR.
"""

from __future__ import annotations

import enum

from sqlalchemy.dialects import postgresql


def pg_enum[E: enum.Enum](py_enum: type[E], type_name: str) -> postgresql.ENUM:
    """One Postgres ENUM type per Python enum, storing *values* not names.

    Defined here (single module) so types shared across tables -- e.g.
    `ledger_entry_type` used by both cash_ledger and bank_ledger -- exist
    exactly once in the metadata.
    """
    return postgresql.ENUM(py_enum, name=type_name, values_callable=lambda e: [m.value for m in e])


class UserRole(enum.StrEnum):
    OWNER = "owner"
    STAFF = "staff"
    VIEWER = "viewer"


class UnitKind(enum.StrEnum):
    WEIGHT = "weight"
    COUNT = "count"
    LENGTH = "length"
    VOLUME = "volume"


class PurchaseStatus(enum.StrEnum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class SalePaymentType(enum.StrEnum):
    CASH = "cash"
    BANK = "bank"
    CREDIT = "credit"


class MovementType(enum.StrEnum):
    PURCHASE = "purchase"
    PURCHASE_RETURN = "purchase_return"
    SALE = "sale"
    SALE_RETURN = "sale_return"
    ADJUSTMENT_INCREASE = "adjustment_increase"
    ADJUSTMENT_DECREASE = "adjustment_decrease"
    DAMAGE = "damage"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"


class LedgerEntryType(enum.StrEnum):
    PURCHASE_PAYMENT = "purchase_payment"
    SALE_RECEIPT = "sale_receipt"
    EXPENSE = "expense"
    INCOME = "income"
    CAPITAL_IN = "capital_in"
    CAPITAL_OUT = "capital_out"
    TRANSFER_TO_BANK = "transfer_to_bank"
    TRANSFER_TO_CASH = "transfer_to_cash"
    OPENING_BALANCE = "opening_balance"


class CapitalEntryType(enum.StrEnum):
    CONTRIBUTION = "contribution"
    WITHDRAWAL = "withdrawal"
    PROFIT_ALLOCATION = "profit_allocation"


class AccountCode(enum.StrEnum):
    """Chart of accounts v1 -- docs/06_Accounting.md §2. Deliberately a
    fixed Python enum, not config (unlike product types)."""

    CASH = "cash"
    BANK = "bank"
    INVENTORY = "inventory"
    ACCOUNTS_RECEIVABLE = "accounts_receivable"
    ACCOUNTS_PAYABLE = "accounts_payable"
    PARTNER_CAPITAL = "partner_capital"
    SALES_REVENUE = "sales_revenue"
    COGS = "cogs"
    FREIGHT_EXPENSE = "freight_expense"
    OPERATING_EXPENSES = "operating_expenses"
    OTHER_INCOME = "other_income"
    DAMAGE_LOSS = "damage_loss"


user_role_enum = pg_enum(UserRole, "user_role")
unit_kind_enum = pg_enum(UnitKind, "unit_kind")
purchase_status_enum = pg_enum(PurchaseStatus, "purchase_status")
sale_payment_type_enum = pg_enum(SalePaymentType, "sale_payment_type")
movement_type_enum = pg_enum(MovementType, "movement_type")
ledger_entry_type_enum = pg_enum(LedgerEntryType, "ledger_entry_type")
capital_entry_type_enum = pg_enum(CapitalEntryType, "capital_entry_type")
