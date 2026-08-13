"""discount on purchases and sales

Revision ID: f5c81d3a7e94
Revises: e1b73f4a92c5
Create Date: 2026-08-14

There was no discount anywhere -- not a column, not an account, not a
service -- and it could not be smuggled in as a negative `other_charges`
because the two sides are not the same kind of thing:

  * A discount *given* on a sale reduces revenue. It belongs on the
    profit and loss, and it is worth seeing on its own: "we sold
    12 lakh and gave away 40,000" is a different sentence from "we sold
    11.6 lakh", and only one of them tells you to stop.

  * A discount *received* on a purchase reduces what the goods cost.
    That is a balance-sheet fact, not a P&L one -- the stock is worth
    what was paid for it, so the discount flows into the landed cost
    alongside freight and charges and there is nothing to put in an
    income account.

So the accounting is deliberately asymmetric, and only the sale side
gets a new account:

    SALES_DISCOUNT   contra-revenue, debit balance.
                     Dr AR (net) + Dr SALES_DISCOUNT = Cr SALES_REVENUE (gross)

`journal_lines.account_code` is a plain String with no check constraint,
so the new code needs no schema change of its own.

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
