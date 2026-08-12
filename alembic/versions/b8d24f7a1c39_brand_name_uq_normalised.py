"""A brand name is unique ignoring case and surrounding space.

The index was on the raw name and the lookup compared case only, so
"TOP " passed both and the catalogue ended up with two brands displaying
as TOP. Twenty-six products were duplicated between them, and the
"which brand?" question offered TOP twice -- the choices are built from
a *set* of names, which collapses two entries into one useless answer.
That is why a sale could pick the wrong brand's stock without asking.

Existing names are trimmed first, otherwise the new index cannot be
built. Any genuine collision that trimming creates has to be merged by
hand before this will apply -- there are none as of this revision.

Revision ID: b8d24f7a1c39
Revises: a7c3e91b5d02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8d24f7a1c39"
down_revision: str | Sequence[str] | None = "a7c3e91b5d02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAME = "brands_org_name_active_uq"


def upgrade() -> None:
    op.execute(sa.text("UPDATE brands SET name = btrim(name) WHERE name <> btrim(name)"))
    op.drop_index(NAME, table_name="brands")
    op.execute(
        sa.text(
            f"CREATE UNIQUE INDEX {NAME} ON brands (org_id, lower(btrim(name))) "
            "WHERE deleted_at IS NULL"
        )
    )


def downgrade() -> None:
    op.drop_index(NAME, table_name="brands")
    op.create_index(
        NAME, "brands", ["org_id", "name"], unique=True, postgresql_where="deleted_at IS NULL"
    )
