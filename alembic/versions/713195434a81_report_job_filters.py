"""report job filters

An export can now be narrowed to one supplier, one customer or one
invoice (docs/13_Reports.md §5). JSONB rather than three nullable
columns: the set of things a report can be narrowed by grows with the
reports themselves, and each new one would otherwise be a migration.

Revision ID: 713195434a81
Revises: 88e94ba932a8
Create Date: 2026-07-28 22:57:36.135291

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "713195434a81"
down_revision: str | Sequence[str] | None = "88e94ba932a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "report_jobs",
        sa.Column(
            "filters",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("report_jobs", "filters")
