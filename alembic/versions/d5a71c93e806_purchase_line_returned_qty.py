"""purchase_lines.returned_qty

`return purchase ...` (docs/08_WhatsApp.md #return) must reject a return
that exceeds what was bought, and a line can be returned more than once.
sales_lines already tracks this (docs/02_Database.md §3.13); purchases
need the same counter.

Revision ID: d5a71c93e806
Revises: c8e2f0b41d73
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5a71c93e806"
down_revision: str | Sequence[str] | None = "c8e2f0b41d73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "purchase_lines",
        sa.Column(
            "returned_qty",
            sa.Numeric(precision=12, scale=3),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint(
        "returned_qty_within_qty", "purchase_lines", "returned_qty >= 0 AND returned_qty <= qty"
    )


def downgrade() -> None:
    op.drop_constraint("returned_qty_within_qty", "purchase_lines", type_="check")
    op.drop_column("purchase_lines", "returned_qty")
