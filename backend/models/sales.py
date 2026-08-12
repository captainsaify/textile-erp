"""sales_headers, sales_lines -- docs/02_Database.md §3.13."""

from __future__ import annotations

import datetime
import decimal
import uuid

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.base import MONEY, QTY, RATE, Base
from backend.models.enums import SalePaymentType, sale_payment_type_enum
from backend.models.mixins import OrgScopedMixin, UUIDPkMixin


class SalesHeader(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "sales_headers"
    __table_args__ = (
        # Idempotency guard -- docs/05_Sales.md#duplicate-sale-detection.
        # Partial: NULL keys never conflict, soft-deleted sales don't block
        # re-entry after an undo.
        Index(
            "sales_headers_org_idempotency_active_uq",
            "org_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL AND deleted_at IS NULL"),
        ),
        CheckConstraint(
            "payment_status IN ('unpaid','partial','paid')", name="payment_status_valid"
        ),
        CheckConstraint(
            "status IN ('confirmed','cancelled','returned','partially_returned')",
            name="status_valid",
        ),
        Index(
            "idx_sales_headers_org_customer",
            "org_id",
            "customer_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "idx_sales_headers_org_date",
            "org_id",
            "sale_date",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False
    )
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("warehouses.id"), nullable=False
    )
    sale_date: Mapped[datetime.date] = mapped_column(
        nullable=False, server_default=text("CURRENT_DATE")
    )
    payment_type: Mapped[SalePaymentType] = mapped_column(sale_payment_type_enum, nullable=False)
    subtotal: Mapped[decimal.Decimal] = mapped_column(
        MONEY, nullable=False, server_default=text("0")
    )
    #: Charges recovered from the customer on top of the goods -- GST,
    #: packing, delivery. They are *not* revenue: revenue is what the
    #: goods sold for, and folding a tax into it overstates both the
    #: revenue line and the gross margin. They post to OTHER_INCOME, so
    #: gross profit stays honest while net profit still counts them.
    freight: Mapped[decimal.Decimal] = mapped_column(
        MONEY, nullable=False, server_default=text("0")
    )
    other_charges: Mapped[decimal.Decimal] = mapped_column(
        MONEY, nullable=False, server_default=text("0")
    )
    grand_total: Mapped[decimal.Decimal] = mapped_column(
        MONEY, nullable=False, server_default=text("0")
    )
    amount_paid: Mapped[decimal.Decimal] = mapped_column(
        MONEY, nullable=False, server_default=text("0")
    )
    payment_status: Mapped[str] = mapped_column(String, nullable=False, server_default="unpaid")
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="confirmed")
    idempotency_key: Mapped[str | None] = mapped_column(String)
    notes: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    deleted_at: Mapped[datetime.datetime | None]

    lines: Mapped[list[SalesLine]] = relationship(
        back_populates="header", order_by="SalesLine.line_no"
    )


class SalesLine(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "sales_lines"
    __table_args__ = (
        UniqueConstraint("sales_header_id", "line_no"),
        CheckConstraint("qty > 0", name="qty_positive"),
        CheckConstraint("rate >= 0", name="rate_non_negative"),
        Index("idx_sales_lines_header", "sales_header_id"),
        Index("idx_sales_lines_product", "product_id"),
    )

    sales_header_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sales_headers.id"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id"), nullable=False
    )
    qty: Mapped[decimal.Decimal] = mapped_column(QTY, nullable=False)
    rate: Mapped[decimal.Decimal] = mapped_column(RATE, nullable=False)
    line_total: Mapped[decimal.Decimal] = mapped_column(MONEY, nullable=False)
    avg_cost_at_sale_time: Mapped[decimal.Decimal] = mapped_column(RATE, nullable=False)
    returned_qty: Mapped[decimal.Decimal] = mapped_column(
        QTY, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))

    header: Mapped[SalesHeader] = relationship(back_populates="lines")
