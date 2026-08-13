"""reversal manifests: what an operation moved, so it can be moved back

Revision ID: d9e4a17c2b68
Revises: c4f7b21e9a35
Create Date: 2026-08-14

`erp merge customer "A" into "B"` recorded `{"merged": "A", "moved": 7}`
in the audit log -- a *count*, not a list. Nothing anywhere said which
seven rows moved, so "reversible" was a claim the operation had not
earned. Two months later, with new sales and payments against B, there
was no way to work out which rows had ever been A's.

This table is that list. Every reversible operation writes one row
naming, by primary key, each row it touched and the value it held
before.

Why not `audit_logs`:

  * it is partitioned by `created_at` and append-only, which is right
    for "what happened" and wrong for "look this up, then mark it
    reversed";
  * `reversed_at` has to be *updated*, and updating an audit row is a
    contradiction;
  * the payload here is a working set to be queried by operation, not a
    record to be read by a human.

The two are complementary and both are written: the audit log says a
merge happened, the manifest says what it would take to undo it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d9e4a17c2b68"
down_revision = "c4f7b21e9a35"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reversal_manifests",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("org_id", sa.UUID(), nullable=False),
        # 'merge_party' | 'merge_brand' | 'merge_purchase' | 'purge' | 'split'
        sa.Column("operation", sa.String(), nullable=False),
        # Human handle for the thing operated on, so a person can find
        # their manifest without knowing an id: "Yakub Asif -> Asif Panipat".
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Set when the operation has been reversed. A manifest is usable
        # exactly once -- reversing twice would move rows that are
        # already home.
        sa.Column("reversed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("reversed_by", sa.UUID(), nullable=True),
        # {"moved": [{table, id, column, from, to}], "hidden": [...], "created": [...]}
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], name=op.f("fk_reversal_manifests_org_id_organizations")
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_reversal_manifests_actor_user_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reversal_manifests")),
        sa.CheckConstraint(
            "operation IN ('merge_party','merge_brand','merge_purchase','purge','split')",
            name=op.f("ck_reversal_manifests_operation_valid"),
        ),
    )
    # The only query that matters: "what can still be reversed", newest
    # first. Partial, because a reversed manifest is history.
    op.create_index(
        "ix_reversal_manifests_open",
        "reversal_manifests",
        ["org_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("reversed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_reversal_manifests_open", table_name="reversal_manifests")
    op.drop_table("reversal_manifests")
