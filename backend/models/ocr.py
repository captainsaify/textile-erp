"""ocr_templates, ocr_learning_dictionary -- docs/02_Database.md §3.8-3.9."""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base
from backend.models.mixins import OrgScopedMixin, UUIDPkMixin


class OcrTemplate(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "ocr_templates"
    __table_args__ = (
        # NULLS NOT DISTINCT: supplier_id NULL means "the default template
        # for this product type", and there must be at most one of those --
        # default UNIQUE semantics would allow unlimited NULL rows.
        UniqueConstraint(
            "org_id", "product_type_id", "supplier_id", postgresql_nulls_not_distinct=True
        ),
    )

    product_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_types.id"), nullable=False
    )
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("suppliers.id")
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    column_mapping: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    ignore_columns: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text('\'["s.no","label","total"]\'::jsonb')
    )
    required_manual_fields: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text(
            '\'["supplier","brand","invoice_no","invoice_date",'
            '"purchase_rate","freight","other_charges"]\'::jsonb'
        ),
    )
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))


class OcrLearningDictionary(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "ocr_learning_dictionary"
    __table_args__ = (
        # NULLS NOT DISTINCT: supplier_id NULL = "applies to any supplier";
        # the same raw text must not get two conflicting global corrections.
        UniqueConstraint(
            "org_id", "supplier_id", "field", "raw_ocr_text", postgresql_nulls_not_distinct=True
        ),
        Index(
            "idx_ocr_learning_raw_trgm",
            "raw_ocr_text",
            postgresql_using="gin",
            postgresql_ops={"raw_ocr_text": "gin_trgm_ops"},
        ),
    )

    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("suppliers.id")
    )
    field: Mapped[str] = mapped_column(String, nullable=False)
    raw_ocr_text: Mapped[str] = mapped_column(String, nullable=False)
    corrected_value: Mapped[str] = mapped_column(String, nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
