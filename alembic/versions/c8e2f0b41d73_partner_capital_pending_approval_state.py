"""partner_capital pending-approval state

docs/06_Accounting.md §8 requires a second partner's approval before a
large withdrawal actually moves money. The request has to be durable
(48h expiry, survives restarts, visible to another user) but must NOT
affect any balance until approved -- if it did, equity would fall while
assets stayed put and the balance-sheet identity in §6 would be broken
for as long as the request went unanswered.

`status` + `posted_at` make that explicit: only 'posted' rows are in the
balance chain, and `posted_at` (not `created_at`) orders it, because an
approval can land long after the request and the chain must follow the
order money actually moved.

Existing rows are all already-effective postings, so they backfill to
status='posted' with posted_at = created_at.

Revision ID: c8e2f0b41d73
Revises: b3d1c7a9e42f
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8e2f0b41d73"
down_revision: str | Sequence[str] | None = "b3d1c7a9e42f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "partner_capital",
        sa.Column("status", sa.String(), nullable=False, server_default="posted"),
    )
    op.add_column(
        "partner_capital", sa.Column("posted_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )
    # every pre-existing row was an immediately-effective posting
    op.execute("UPDATE partner_capital SET posted_at = created_at WHERE posted_at IS NULL")

    op.create_check_constraint(
        "status_valid", "partner_capital", "status IN ('pending','posted','rejected')"
    )
    op.create_check_constraint(
        "posted_at_matches_status",
        "partner_capital",
        "(status = 'posted') = (posted_at IS NOT NULL)",
    )
    op.create_index("idx_partner_capital_pending", "partner_capital", ["org_id", "status"])


def downgrade() -> None:
    # A pending request has never moved money, so dropping it loses no
    # posted history; a rejected one likewise. Both are removed rather
    # than left behind as rows the old schema would read as effective.
    op.execute("DELETE FROM partner_capital WHERE status IN ('pending','rejected')")
    op.drop_index("idx_partner_capital_pending", table_name="partner_capital")
    op.drop_constraint("posted_at_matches_status", "partner_capital", type_="check")
    op.drop_constraint("status_valid", "partner_capital", type_="check")
    op.drop_column("partner_capital", "posted_at")
    op.drop_column("partner_capital", "status")
