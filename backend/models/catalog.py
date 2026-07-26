"""units, product_categories, brands, product_types, products, warehouses
-- docs/02_Database.md §3.4-3.7, §3.11."""

from __future__ import annotations

import datetime
import decimal
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.base import QTY, Base
from backend.models.enums import UnitKind, unit_kind_enum
from backend.models.mixins import OrgScopedMixin, UUIDPkMixin


class Unit(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "units"
    __table_args__ = (UniqueConstraint("org_id", "code"),)

    code: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[UnitKind] = mapped_column(unit_kind_enum, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))


class ProductCategory(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "product_categories"
    __table_args__ = (
        # NULLS NOT DISTINCT so two root categories (parent_id NULL) with the
        # same name collide, which is what UNIQUE(org_id, name, parent_id)
        # intends -- default Postgres semantics would let them coexist.
        UniqueConstraint("org_id", "name", "parent_id", postgresql_nulls_not_distinct=True),
    )

    name: Mapped[str] = mapped_column(String, nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_categories.id")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))


class Brand(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "brands"
    __table_args__ = (
        Index(
            "brands_org_name_active_uq",
            "org_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    deleted_at: Mapped[datetime.datetime | None]


class ProductType(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "product_types"
    __table_args__ = (
        UniqueConstraint("org_id", "code"),
        CheckConstraint("costing_strategy IN ('weighted_average')", name="costing_strategy_valid"),
    )

    code: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    default_unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("units.id"), nullable=False
    )
    costing_strategy: Mapped[str] = mapped_column(
        String, nullable=False, server_default="weighted_average"
    )
    attribute_schema: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))

    default_unit: Mapped[Unit] = relationship()


class Product(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "products"
    __table_args__ = (
        # Case-insensitive, soft-delete-aware code uniqueness -- §3.7 + §4.
        # Scoped by brand: suppliers reuse short codes like VVP or MJP
        # across brands, so a code is only unique *within* a brand.
        # NULLS NOT DISTINCT keeps that honest for brandless products --
        # without it Postgres treats every NULL brand as unique and the
        # same unbranded code could be inserted twice.
        Index(
            "products_org_code_active_uq",
            "org_id",
            text("upper(code)"),
            "brand_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            postgresql_nulls_not_distinct=True,
        ),
        Index(
            "idx_products_org_type",
            "org_id",
            "product_type_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "idx_products_org_brand",
            "org_id",
            "brand_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "idx_products_code_trgm",
            "code",
            postgresql_using="gin",
            postgresql_ops={"code": "gin_trgm_ops"},
        ),
        Index(
            "idx_products_description_trgm",
            "description",
            postgresql_using="gin",
            postgresql_ops={"description": "gin_trgm_ops"},
        ),
    )

    product_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_types.id"), nullable=False
    )
    code: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    brand_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("brands.id"))
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_categories.id")
    )
    unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("units.id"), nullable=False
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    reorder_level: Mapped[decimal.Decimal | None] = mapped_column(QTY)
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    deleted_at: Mapped[datetime.datetime | None]

    product_type: Mapped[ProductType] = relationship()
    unit: Mapped[Unit] = relationship()
    brand: Mapped[Brand | None] = relationship()
    category: Mapped[ProductCategory | None] = relationship()


class Warehouse(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "warehouses"
    __table_args__ = (
        UniqueConstraint("org_id", "name"),
        Index(
            "warehouses_one_default_per_org",
            "org_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    name: Mapped[str] = mapped_column(String, nullable=False)
    is_default: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
