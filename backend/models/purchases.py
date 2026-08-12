"""purchase_headers, purchase_lines -- docs/02_Database.md §3.12."""

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

from backend.models.base import CONFIDENCE, MONEY, QTY, RATE, Base
from backend.models.enums import PurchaseStatus, purchase_status_enum
from backend.models.mixins import OrgScopedMixin, UUIDPkMixin


class PurchaseHeader(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "purchase_headers"
    __table_args__ = (
        # Exact-duplicate guard, soft-delete-aware (§4); the fuzzy guard is
        # application-level -- docs/04_Purchases.md#duplicate-detection.
        #
        # Only *confirmed* bills reserve their number. Without the status
        # clause a cancelled bill held its invoice number for ever, and
        # since an exact duplicate cannot be overridden, the correction
        # this system recommends -- undo it and enter it again -- was
        # impossible on any bill that had been entered once. A number
        # can legitimately be cancelled several times before it goes in
        # correctly.
        Index(
            "purchase_headers_org_supplier_invoice_active_uq",
            "org_id",
            "supplier_id",
            "invoice_no",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND status = 'confirmed'"),
        ),
        CheckConstraint(
            "freight_allocation_method IN ('by_weight','by_value','by_qty','manual')",
            name="freight_allocation_method_valid",
        ),
        CheckConstraint(
            "payment_status IN ('unpaid','partial','paid')", name="payment_status_valid"
        ),
        Index(
            "idx_purchase_headers_org_supplier",
            "org_id",
            "supplier_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "idx_purchase_headers_org_date",
            "org_id",
            "invoice_date",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "idx_purchase_headers_dup_check",
            "org_id",
            "supplier_id",
            "invoice_date",
            "grand_total",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    supplier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("suppliers.id"), nullable=False
    )
    brand_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("brands.id"))
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("warehouses.id"), nullable=False
    )
    invoice_no: Mapped[str] = mapped_column(String, nullable=False)
    invoice_date: Mapped[datetime.date] = mapped_column(nullable=False)
    purchase_rate: Mapped[decimal.Decimal | None] = mapped_column(RATE)
    freight: Mapped[decimal.Decimal] = mapped_column(
        MONEY, nullable=False, server_default=text("0")
    )
    other_charges: Mapped[decimal.Decimal] = mapped_column(
        MONEY, nullable=False, server_default=text("0")
    )
    freight_allocation_method: Mapped[str] = mapped_column(
        String, nullable=False, server_default="by_weight"
    )
    subtotal: Mapped[decimal.Decimal] = mapped_column(
        MONEY, nullable=False, server_default=text("0")
    )
    grand_total: Mapped[decimal.Decimal] = mapped_column(
        MONEY, nullable=False, server_default=text("0")
    )
    declared_total: Mapped[decimal.Decimal | None] = mapped_column(MONEY)
    status: Mapped[PurchaseStatus] = mapped_column(
        purchase_status_enum, nullable=False, server_default=PurchaseStatus.DRAFT.value
    )
    payment_status: Mapped[str] = mapped_column(String, nullable=False, server_default="unpaid")
    amount_paid: Mapped[decimal.Decimal] = mapped_column(
        MONEY, nullable=False, server_default=text("0")
    )
    ocr_source_attachment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("attachments.id")
    )
    notes: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    deleted_at: Mapped[datetime.datetime | None]
    #: Set alongside `deleted_at` by `erp purge` (docs/31_AdminCLI.md).
    #: `deleted_at` is what hides the row -- every query already filters
    #: on it. This only distinguishes "purged, should never have been
    #: entered" from "soft-deleted, was cancelled", so `restore-purged`
    #: cannot resurrect something that was merely cancelled.
    purged_at: Mapped[datetime.datetime | None]

    lines: Mapped[list[PurchaseLine]] = relationship(
        back_populates="header", order_by="PurchaseLine.line_no"
    )


class PurchaseLine(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "purchase_lines"
    __table_args__ = (
        UniqueConstraint("purchase_header_id", "line_no"),
        CheckConstraint("qty > 0", name="qty_positive"),
        CheckConstraint("rate >= 0", name="rate_non_negative"),
        CheckConstraint(
            "returned_qty >= 0 AND returned_qty <= qty", name="returned_qty_within_qty"
        ),
        Index("idx_purchase_lines_header", "purchase_header_id"),
        Index("idx_purchase_lines_product", "product_id"),
    )

    purchase_header_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("purchase_headers.id"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id"), nullable=False
    )
    # The description exactly as it appeared on this invoice. The
    # canonical name lives on products.description; this records what the
    # supplier called it that day, which drifts and is worth keeping for
    # audit against the original sheet.
    description: Mapped[str | None] = mapped_column(String)
    qty: Mapped[decimal.Decimal] = mapped_column(QTY, nullable=False)
    weight_kg: Mapped[decimal.Decimal | None] = mapped_column(QTY)
    total_weight_kg: Mapped[decimal.Decimal | None] = mapped_column(QTY)
    rate: Mapped[decimal.Decimal] = mapped_column(RATE, nullable=False)
    line_total: Mapped[decimal.Decimal] = mapped_column(MONEY, nullable=False)
    freight_allocated: Mapped[decimal.Decimal] = mapped_column(
        MONEY, nullable=False, server_default=text("0")
    )
    landed_cost_per_unit: Mapped[decimal.Decimal | None] = mapped_column(RATE)
    # Mirrors sales_lines.returned_qty (docs/02_Database.md §3.13): what
    # has already gone back to the supplier, so a second return can't
    # take more than was bought.
    returned_qty: Mapped[decimal.Decimal] = mapped_column(
        QTY, nullable=False, server_default=text("0")
    )
    ocr_confidence: Mapped[decimal.Decimal | None] = mapped_column(CONFIDENCE)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))

    header: Mapped[PurchaseHeader] = relationship(back_populates="lines")
