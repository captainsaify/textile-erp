"""a separate credential for Master Control

Revision ID: a2d64e19f7b3
Revises: f5c81d3a7e94
Create Date: 2026-08-14

plan.md §4. The read-only dashboard and Master Control are reached with
different passwords, because they are different propositions: one shows
you charts, the other can merge two customers and purge a bill.

A column on `users` rather than a second table or a config value:

  * it is per-person, so revoking control access is one row, not a
    redeploy;
  * it is NULL for everyone by default, which means the danger surface
    does not exist until someone deliberately creates it -- a
    config-file password exists the moment the file does;
  * `set-control-password` in the CLI is the only way to fill it, and
    that command runs on the box, behind SSH.

Nullable and unset everywhere after this migration. Master Control is
unreachable until a password is deliberately set.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a2d64e19f7b3"
down_revision = "f5c81d3a7e94"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("control_password_hash", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "control_password_hash")
