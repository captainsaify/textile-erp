"""Charges on a sale: GST, packing, delivery.

A bill carries more than the goods. Purchases have had `freight` and
`other_charges` since the beginning; sales never did, so anything
recovered from a customer on top of the goods had nowhere to go and was
being recorded as a separate operating expense -- which put it on the
wrong side of the books entirely.

NOT NULL with a server default of 0, so every sale already recorded
reads as "no charges" rather than NULL. Backfilling is unnecessary and
the default makes the column safe to add while the app is running.

Revision ID: f2a91c4d7e13
Revises: 713195434a81
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2a91c4d7e13"
down_revision: str | Sequence[str] | None = "713195434a81"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(14, 2)


def upgrade() -> None:
    op.add_column(
        "sales_headers",
        sa.Column("freight", MONEY, nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "sales_headers",
        sa.Column("other_charges", MONEY, nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("sales_headers", "other_charges")
    op.drop_column("sales_headers", "freight")
