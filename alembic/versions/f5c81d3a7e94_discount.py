"""discount on purchases and sales

Revision ID: f5c81d3a7e94
Revises: e1b73f4a92c5
Create Date: 2026-08-14

There was no discount anywhere -- not a column, not a service. It is a
deduction and nothing more: on a sale less was charged, so revenue is
lower; on a purchase less was paid, so the goods cost less.

No account either side. A discount account would carry a figure that
never happened -- the gross was not billed and not paid -- and every
revenue total downstream would read high until someone remembered to
net it off. Both effects are already visible where they belong: lower
revenue on the P&L, lower stock value on the balance sheet, and the
amount itself on the header for anyone who wants to see it.

What it must not be is a negative `other_charges`. That column holds
amounts genuinely charged on top -- GST, packing -- and a price
reduction sharing it would give two things one column and let them
disagree about the sign.

Header level, not per line. A per-line discount is a different feature
with a different UI, and this column does not stand in its way.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f5c81d3a7e94"
down_revision = "e1b73f4a92c5"
branch_labels = None
depends_on = None

MONEY = sa.Numeric(14, 2)


def upgrade() -> None:
    for table in ("purchase_headers", "sales_headers"):
        op.add_column(
            table,
            sa.Column("discount", MONEY, nullable=False, server_default=sa.text("0")),
        )
        # Negative discounts are surcharges, and a surcharge is what
        # `other_charges` is for. Allowing both would give two ways to
        # say one thing and let them disagree.
        op.create_check_constraint(
            op.f(f"ck_{table}_discount_non_negative"), table, "discount >= 0"
        )


def downgrade() -> None:
    for table in ("purchase_headers", "sales_headers"):
        op.drop_constraint(op.f(f"ck_{table}_discount_non_negative"), table, type_="check")
        op.drop_column(table, "discount")
