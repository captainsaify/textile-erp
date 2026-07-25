"""Shared column mixins.

Per docs/02_Database.md §1, every table gets a pgcrypto-backed UUID
PK, and every *business* table (not pure config/lookup tables) has an
`org_id` FK. Those two are the only truly universal pieces -- the
doc's per-table DDL (§3.1-3.21) shows `created_at`/`updated_at`/
`created_by`/`deleted_at` are present on most, but not all, tables
(e.g. `units`, `warehouses`, `journal_lines` deliberately omit some of
these). To stay faithful to the spec, only `id` and `org_id` are
factored into mixins here; the timestamp/audit columns are declared
explicitly on each model to match docs/02_Database.md exactly.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, declared_attr, mapped_column


class UUIDPkMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


class OrgScopedMixin:
    @declared_attr
    def org_id(cls) -> Mapped[uuid.UUID]:  # noqa: N805 -- SQLAlchemy declared_attr convention
        return mapped_column(
            UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
        )
