"""message log, and the reversible operations that did not exist yet

Revision ID: b7c3f1e08a25
Revises: a2d64e19f7b3
Create Date: 2026-08-14

Two unrelated things, one migration, because both are one-way additions
that ship together.

**message_log.** Seventeen messages failed overnight with Meta code
131047 and nobody knew until someone read the container logs. A send
that fails is an event about the business -- a partner did not get their
sheet -- and events about the business belong in a table, not in a log
line that rotates away.

Deliberately *not* org-scoped. The sender is a process-global client
that has no idea which books a message came from, and threading an
org through every call site to satisfy a mixin would be inventing a
fact to fill a column. This is transport telemetry: who we tried to
reach, whether it landed, and what the other end said.

**The operation check constraint.** `reversal_manifests.operation` is
an allow-list, which is why merging two products could not be recorded
until the list knew the word.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b7c3f1e08a25"
down_revision = "a2d64e19f7b3"
branch_labels = None
depends_on = None

_OLD = "operation IN ('merge_party','merge_brand','merge_purchase','purge','split')"
_NEW = (
    "operation IN ('merge_party','merge_brand','merge_purchase','purge','split',"
    "'merge_product','delete_product','relink_contact')"
)


def upgrade() -> None:
    op.create_table(
        "message_log",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        # 'out' | 'in'
        sa.Column("direction", sa.String(), nullable=False),
        # 'cloud' (Meta Graph) | 'bridge' (whatsapp-web.js) | 'webhook'
        sa.Column("transport", sa.String(), nullable=False),
        # The number or chat id at the other end, as the transport knows
        # it: E.164 for Meta, `...@c.us` / `...@g.us` for the bridge.
        sa.Column("peer", sa.String(), nullable=False),
        # 'text' | 'interactive' | 'document' | 'image' | ...
        sa.Column("kind", sa.String(), nullable=False),
        # First 300 characters. Enough to recognise which message this
        # was; not a second copy of the business record.
        sa.Column("preview", sa.String(), nullable=False, server_default=""),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        # Meta's own code -- 131047 is the one that started this.
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("error_detail", sa.String(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_log")),
        sa.CheckConstraint("direction IN ('out','in')", name=op.f("ck_message_log_direction_valid")),
    )
    # The two questions actually asked of this table: "what happened
    # lately" and "what is broken". The second is partial because
    # failures are the rare case and the index should stay small.
    op.create_index("ix_message_log_recent", "message_log", [sa.text("created_at DESC")])
    op.create_index(
        "ix_message_log_failures",
        "message_log",
        [sa.text("created_at DESC")],
        postgresql_where=sa.text("NOT ok"),
    )

    op.drop_constraint("ck_reversal_manifests_operation_valid", "reversal_manifests")
    op.create_check_constraint(
        op.f("ck_reversal_manifests_operation_valid"), "reversal_manifests", sa.text(_NEW)
    )


def downgrade() -> None:
    op.drop_constraint("ck_reversal_manifests_operation_valid", "reversal_manifests")
    op.create_check_constraint(
        op.f("ck_reversal_manifests_operation_valid"), "reversal_manifests", sa.text(_OLD)
    )
    op.drop_index("ix_message_log_failures", table_name="message_log")
    op.drop_index("ix_message_log_recent", table_name="message_log")
    op.drop_table("message_log")
