"""Only a confirmed bill reserves its invoice number.

The partial unique index excluded soft-deleted rows but not cancelled
ones, so a bill that had been entered and undone held its number for
ever. An exact duplicate cannot be overridden by design, which made the
correction this system actually recommends -- undo it and enter it again
-- impossible on any bill that had been entered once. It blocked
re-entering 1051 after an undo, and again when merging 007 and 007B into
a single bill.

Repairing `get_confirmed_by_invoice` in the repository was only half of
it: the database index raised IntegrityError from underneath the service
check, which the service reported as the same duplicate error.

Revision ID: a7c3e91b5d02
Revises: f2a91c4d7e13
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a7c3e91b5d02"
down_revision: str | Sequence[str] | None = "f2a91c4d7e13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAME = "purchase_headers_org_supplier_invoice_active_uq"
COLUMNS = ["org_id", "supplier_id", "invoice_no"]


def upgrade() -> None:
    op.drop_index(NAME, table_name="purchase_headers")
    op.create_index(
        NAME,
        "purchase_headers",
        COLUMNS,
        unique=True,
        postgresql_where="deleted_at IS NULL AND status = 'confirmed'",
    )


def downgrade() -> None:
    op.drop_index(NAME, table_name="purchase_headers")
    op.create_index(
        NAME,
        "purchase_headers",
        COLUMNS,
        unique=True,
        postgresql_where="deleted_at IS NULL",
    )
