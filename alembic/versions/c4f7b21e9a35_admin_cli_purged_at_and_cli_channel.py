"""admin cli: purged_at on transactions, and 'cli' as an audit channel

Revision ID: c4f7b21e9a35
Revises: b8d24f7a1c39
Create Date: 2026-08-13

Two changes, both for the admin CLI (docs/31_AdminCLI.md).

1. `purged_at` on purchase_headers and sales_headers.

   A purge sets `deleted_at` *and* `purged_at`. Setting `deleted_at` is
   what does the work: every query in the codebase already filters
   `deleted_at IS NULL`, so a purged record leaves every report, total,
   ledger, search and reconciliation pass without a single one of those
   filters having to learn about a new column. Introducing `purged_at`
   as a second, parallel condition would have meant finding and
   amending dozens of them, and the one that got missed would put a
   purged bill back inside a total -- precisely the failure purge exists
   to prevent.

   `purged_at` therefore carries no visibility meaning at all. It exists
   to tell two states apart that would otherwise be identical: a record
   soft-deleted because it was cancelled, and one purged because it
   should never have been entered. Only `restore-purged` reads it, and
   it must not restore something that was merely cancelled.

2. 'cli' added to the audit channel check constraint.

   Every mutation writes an audit row, and the CLI is a fourth way in
   alongside whatsapp, api and dashboard. Without this the first admin
   command fails on ck_audit_logs_channel_valid -- which is not a
   hypothetical: it was hit during development.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c4f7b21e9a35"
down_revision = "b8d24f7a1c39"
branch_labels = None
depends_on = None


_CHANNELS_BEFORE = "channel IN ('whatsapp','api','dashboard','system')"
_CHANNELS_AFTER = "channel IN ('whatsapp','api','dashboard','system','cli')"


def upgrade() -> None:
    for table in ("purchase_headers", "sales_headers"):
        op.add_column(
            table,
            sa.Column("purged_at", sa.TIMESTAMP(timezone=True), nullable=True),
        )
        # Partial: purged rows are the rare case, and the only query that
        # reads this column asks for exactly them.
        op.create_index(
            f"ix_{table}_purged_at",
            table,
            ["purged_at"],
            unique=False,
            postgresql_where=sa.text("purged_at IS NOT NULL"),
        )

    # op.f(): the metadata naming convention prepends "ck_<table>_", so a
    # bare name becomes ck_audit_logs_ck_audit_logs_channel_valid and the
    # DROP finds nothing. op.f marks the name as already final.
    op.drop_constraint(op.f("ck_audit_logs_channel_valid"), "audit_logs", type_="check")
    op.create_check_constraint(op.f("ck_audit_logs_channel_valid"), "audit_logs", _CHANNELS_AFTER)


def downgrade() -> None:
    # Rows written by the CLI would violate the narrower constraint, so
    # they are re-labelled rather than left to fail the migration. They
    # are audit records: losing the distinction is bad, losing the row
    # would be worse.
    op.execute("UPDATE audit_logs SET channel = 'system' WHERE channel = 'cli'")
    op.drop_constraint(op.f("ck_audit_logs_channel_valid"), "audit_logs", type_="check")
    op.create_check_constraint(op.f("ck_audit_logs_channel_valid"), "audit_logs", _CHANNELS_BEFORE)

    for table in ("purchase_headers", "sales_headers"):
        op.drop_index(f"ix_{table}_purged_at", table_name=table)
        op.drop_column(table, "purged_at")
