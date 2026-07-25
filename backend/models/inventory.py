"""inventory, inventory_movements -- docs/02_Database.md §3.14.

`inventory` is a materialized balance cache; `inventory_movements` is the
append-only source of truth, partitioned by month from day one (§9) --
hence its composite (id, created_at) PK: Postgres requires the partition
key inside the primary key.
"""

from __future__ import annotations

import datetime
import decimal
import uuid

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import QTY, RATE, Base
from backend.models.enums import MovementType, movement_type_enum
from backend.models.mixins import OrgScopedMixin, UUIDPkMixin


class Inventory(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "inventory"
    __table_args__ = (UniqueConstraint("org_id", "product_id", "warehouse_id"),)

    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id"), nullable=False
    )
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("warehouses.id"), nullable=False
    )
    qty_on_hand: Mapped[decimal.Decimal] = mapped_column(
        QTY, nullable=False, server_default=text("0")
    )
    weighted_avg_cost: Mapped[decimal.Decimal] = mapped_column(
        RATE, nullable=False, server_default=text("0")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))


class InventoryMovement(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "inventory_movements"
    __table_args__ = (
        Index(
            "idx_inventory_movements_product_warehouse",
            "org_id",
            "product_id",
            "warehouse_id",
            "created_at",
        ),
        Index("idx_inventory_movements_source", "source_type", "source_id"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id"), nullable=False
    )
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("warehouses.id"), nullable=False
    )
    movement_type: Mapped[MovementType] = mapped_column(movement_type_enum, nullable=False)
    qty_delta: Mapped[decimal.Decimal] = mapped_column(QTY, nullable=False)
    unit_cost: Mapped[decimal.Decimal] = mapped_column(RATE, nullable=False)
    resulting_qty_on_hand: Mapped[decimal.Decimal] = mapped_column(QTY, nullable=False)
    resulting_avg_cost: Mapped[decimal.Decimal] = mapped_column(RATE, nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    # Deliberately not a DB-level FK: target table depends on source_type.
    # Integrity enforced in the repository layer -- §3.14 note.
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(
        primary_key=True, server_default=text("now()")
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
