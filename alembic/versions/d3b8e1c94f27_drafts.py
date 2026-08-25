"""a half-entered bill that nobody has committed to

Revision ID: d3b8e1c94f27
Revises: c1f9a4b72e58
Create Date: 2026-08-25

The form already kept a draft, in one browser's localStorage, in one
slot: starting a sale overwrote a half-typed purchase, and neither
partner could see what the other had left unfinished. A draft that only
exists on the laptop that typed it does not survive the thing it exists
to survive.

Deliberately its own table rather than `purchase_headers` with
`status='draft'`. A draft may name a supplier who does not exist, a
code that resolves to nothing, a quantity half-typed. In the books,
every query that totals a payable or reconciles inventory would have to
remember to exclude it, and the first one that forgets reports a number
nobody can explain. Here, nothing can move money.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d3b8e1c94f27"
down_revision = "c1f9a4b72e58"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "drafts",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        # 'purchase' | 'sale'
        sa.Column("kind", sa.String(), nullable=False),
        # Minted by the form when it opens and kept for the life of that
        # entry, so autosave updates one row rather than leaving a trail
        # of near-identical drafts behind every keystroke.
        sa.Column("client_key", sa.String(), nullable=False),
        sa.Column(
            "payload",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # Derived on save, not on read: the list must not depend on
        # resolving a code that may since have been merged away.
        sa.Column("label", sa.String(), nullable=False, server_default=""),
        sa.Column("line_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("ready", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_drafts_org_id", "drafts", ["org_id"])
    op.create_index(
        "drafts_org_key_uq",
        "drafts",
        ["org_id", "client_key"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("drafts_org_key_uq", table_name="drafts")
    op.drop_index("ix_drafts_org_id", table_name="drafts")
    op.drop_table("drafts")
