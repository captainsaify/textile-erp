"""sales_lines gets the weight columns purchase_lines already has

Revision ID: e1b73f4a92c5
Revises: d9e4a17c2b68
Create Date: 2026-08-14

The sheets these books are copied from have three quantity columns --
Qty (bales), KG (per bale) and Total KG -- and the rate is per kilogram,
so the line amount is `Total KG x Rate`. `purchase_lines` has carried
`weight_kg` and `total_weight_kg` since the beginning; `sales_lines`
never did, and has only `qty`.

That asymmetry did not matter while both sides were entered over
WhatsApp a field at a time. It matters the moment there is a form: a
purchase screen with three quantity columns and a sale screen with one
reads as two different products, and the sale side could not record what
the seller actually counted.

Nullable, because every sale recorded so far has no weight and inventing
one would be fabrication. `qty` remains the costing quantity in
kilograms on both sides, which is what keeps every existing query, the
weighted-average costing and the reconciliation untouched. Bales stay
derived (`qty / weight_kg`) rather than stored, exactly as on the
purchase side, so there is one place the arithmetic lives.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e1b73f4a92c5"
down_revision = "d9e4a17c2b68"
branch_labels = None
depends_on = None

# Numeric(12, 3) -- the QTY alias in backend/models/base.py, which is
# what purchase_lines uses. Written out rather than imported so the
# migration keeps meaning what it meant if the alias ever changes, but
# it must match: a query that joins or unions the two sides should not
# have to remember which one it is on.
QTY = sa.Numeric(12, 3)


def upgrade() -> None:
    op.add_column("sales_lines", sa.Column("weight_kg", QTY, nullable=True))
    op.add_column("sales_lines", sa.Column("total_weight_kg", QTY, nullable=True))


def downgrade() -> None:
    op.drop_column("sales_lines", "total_weight_kg")
    op.drop_column("sales_lines", "weight_kg")
