"""A half-entered bill that nobody has committed to -- docs/33_Drafts.md.

Deliberately *not* a `purchase_headers` row with `status='draft'`. A
draft is a saved form, not a business record: it may name a supplier
who does not exist yet, a code that resolves to nothing, a quantity
half-typed. Putting that in the books would mean every query that
totals a payable, reconciles inventory or builds a statement has to
remember to exclude it, and the first one that forgets reports a
number nobody can explain.

So the payload is opaque JSON -- exactly what the form held -- and it
becomes a record only by going through the same endpoint a fresh entry
does, with the same checks. Nothing here can move money.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base
from backend.models.mixins import OrgScopedMixin, UUIDPkMixin


class Draft(UUIDPkMixin, OrgScopedMixin, Base):
    __tablename__ = "drafts"
    __table_args__ = (
        # The form mints a key when it opens and keeps it for the life of
        # that entry, so autosave updates one row instead of leaving a
        # trail of near-identical drafts behind every keystroke.
        Index(
            "drafts_org_key_uq",
            "org_id",
            "client_key",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    #: 'purchase' | 'sale'
    kind: Mapped[str] = mapped_column(String, nullable=False)
    client_key: Mapped[str] = mapped_column(String, nullable=False)
    #: Whatever the form held. Read back only by the form that wrote it.
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    #: Shown in the list so a half-finished bill can be recognised
    #: without opening it. Derived from the payload on save rather than
    #: computed on read -- the list must not depend on resolving a code
    #: that may no longer exist.
    label: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    line_count: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    #: True when the payload has everything the create endpoint needs.
    #: Advisory only: the endpoint still validates for itself.
    ready: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    deleted_at: Mapped[datetime.datetime | None]
