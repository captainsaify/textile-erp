"""suppliers, customers -- docs/02_Database.md §3.10."""

from __future__ import annotations

import datetime
import decimal
import uuid

from sqlalchemy import ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import MONEY, Base
from backend.models.mixins import OrgScopedMixin, UUIDPkMixin


class Supplier(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "suppliers"
    __table_args__ = (
        Index(
            "suppliers_org_name_active_uq",
            "org_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "idx_suppliers_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )

    name: Mapped[str] = mapped_column(String, nullable=False)
    phone: Mapped[str | None] = mapped_column(String)
    address: Mapped[str | None] = mapped_column(String)
    gst_number: Mapped[str | None] = mapped_column(String)
    opening_balance: Mapped[decimal.Decimal] = mapped_column(
        MONEY, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    deleted_at: Mapped[datetime.datetime | None]


class Customer(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "customers"
    __table_args__ = (
        Index(
            "customers_org_name_active_uq",
            "org_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "idx_customers_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )

    name: Mapped[str] = mapped_column(String, nullable=False)
    phone: Mapped[str | None] = mapped_column(String)
    address: Mapped[str | None] = mapped_column(String)
    gst_number: Mapped[str | None] = mapped_column(String)
    credit_limit: Mapped[decimal.Decimal | None] = mapped_column(MONEY)
    opening_balance: Mapped[decimal.Decimal] = mapped_column(
        MONEY, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    deleted_at: Mapped[datetime.datetime | None]
