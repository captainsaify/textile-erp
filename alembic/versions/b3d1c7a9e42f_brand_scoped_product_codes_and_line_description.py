"""brand-scoped product codes, per-line description

Suppliers reuse short codes (VVP, MJP, TRP) across brands, so a code is
only unique within a brand -- see docs/03_Inventory.md (multi-brand via
brand_id). NULLS NOT DISTINCT keeps brandless products honest: without
it Postgres treats every NULL brand as distinct and the same unbranded
code could be inserted twice.

purchase_lines.description records the item name exactly as printed on
that invoice. The canonical name stays on products.description; this is
the audit trail back to the original sheet, since suppliers rename items
between invoices.

Revision ID: b3d1c7a9e42f
Revises: 8f6540f6f110
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3d1c7a9e42f"
down_revision: str | Sequence[str] | None = "8f6540f6f110"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("purchase_lines", sa.Column("description", sa.String(), nullable=True))

    op.drop_index(
        "products_org_code_active_uq",
        table_name="products",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    # NULLS NOT DISTINCT needs PG 15+; the project targets PG 16, and
    # Alembic has no kwarg for it, so this one is raw SQL.
    op.execute(
        """
        CREATE UNIQUE INDEX products_org_code_active_uq
            ON products (org_id, upper(code), brand_id)
            NULLS NOT DISTINCT
            WHERE deleted_at IS NULL
        """
    )


def downgrade() -> None:
    # Codes that only differ by brand collide under the old index, so
    # soft-delete the later duplicates before restoring it rather than
    # letting the index creation fail.
    op.execute(
        """
        UPDATE products p SET deleted_at = now()
        WHERE p.deleted_at IS NULL
          AND EXISTS (
              SELECT 1 FROM products o
              WHERE o.org_id = p.org_id
                AND upper(o.code) = upper(p.code)
                AND o.deleted_at IS NULL
                AND (o.created_at, o.id) < (p.created_at, p.id)
          )
        """
    )
    op.drop_index("products_org_code_active_uq", table_name="products")
    op.create_index(
        "products_org_code_active_uq",
        "products",
        ["org_id", sa.literal_column("upper(code)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.drop_column("purchase_lines", "description")
