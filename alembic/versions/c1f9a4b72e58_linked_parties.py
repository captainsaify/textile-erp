"""one person who both sells to us and buys from us

Revision ID: c1f9a4b72e58
Revises: b7c3f1e08a25
Create Date: 2026-08-22

Afzal sells us fabric and buys fabric from us. Until now that was two
unrelated rows -- a `suppliers` row and a `customers` row that happened
to share a spelling -- so the books held a payable of 5,00,000 and a
receivable of 98,000 with nothing anywhere saying they were the same
man. Two statements, two balances, and the net position, which is the
only number either partner actually wants, existed nowhere.

`suppliers.linked_customer_id` says they are one person. It is nullable
because almost nobody trades both ways, and unique because a supplier
is at most one customer -- a many-to-many here would be a data model
for a question nobody asked.

Deliberately *not* a merge of the two tables. A purchase must carry a
supplier and a sale a customer; collapsing them into one `parties`
table would rewrite every foreign key in the schema to buy a property
one nullable column already provides.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c1f9a4b72e58"
down_revision = "b7c3f1e08a25"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "suppliers",
        sa.Column("linked_customer_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "suppliers_linked_customer_fk",
        "suppliers",
        "customers",
        ["linked_customer_id"],
        ["id"],
    )
    # One customer answers to at most one supplier. Without this, two
    # supplier rows could both claim Afzal and the net position would
    # depend on which one you opened.
    op.create_index(
        "suppliers_linked_customer_uq",
        "suppliers",
        ["linked_customer_id"],
        unique=True,
        postgresql_where=sa.text("linked_customer_id IS NOT NULL AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("suppliers_linked_customer_uq", table_name="suppliers")
    op.drop_constraint("suppliers_linked_customer_fk", "suppliers", type_="foreignkey")
    op.drop_column("suppliers", "linked_customer_id")
