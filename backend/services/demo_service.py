"""A second, throwaway business to demonstrate the system in --
docs/29_DemoMode.md.

Showing someone how this works means recording purchases, sales and
payments. Doing that in the partners' own books leaves ₹15,000 test
receivables and duplicate bills to clean up afterwards -- which has
already happened twice.

So a demo lives in its own `organizations` row. Nothing else changes:
every table is already `org_id`-scoped and every query already filters
on it, which is exactly the property `docs/18_FutureRoadmap.md` says
multi-tenancy would be a migration rather than a rewrite. The isolation
is the schema's, not a flag anyone has to remember to check.

**What is *not* isolated:** the WhatsApp number. One person, one phone,
two sets of books -- so the mode is per sender, and every reply while it
is on says so. A demo that looked identical to the real thing would be
worse than no demo.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import (
    OcrTemplate,
    Organization,
    Partner,
    ProductType,
    Unit,
    User,
    Warehouse,
)

#: Fixed so it survives restarts and can be referred to in logs, and
#: unmistakable beside the seeded org's ...0001 in a query result.
DEMO_ORG_ID = uuid.UUID("00000000-0000-4000-a000-0000000dbeef")
DEMO_ORG_NAME = "Demo Business (not real)"

#: What one reply looks like while the demo is on. Prefixed rather than
#: appended: on a long reply the footer scrolls off, and the whole point
#: is that nobody mistakes a demo figure for a real one.
DEMO_BANNER = "🧪 *DEMO* — test books, not your real business."

#: Children whose parent carries the org. `journal_lines` has no
#: org_id of its own -- it belongs to a journal entry, which does.
_RESET_VIA_PARENT = (("journal_lines", "journal", "journal_id"),)

#: Business rows, in an order that respects the foreign keys. Mirrors
#: the test suite's purge list -- same problem, same answer.
_RESET_ORDER = (
    "journal",
    "audit_logs",
    "cash_ledger",
    "bank_ledger",
    "partner_capital",
    "inventory_movements",
    "inventory",
    "purchase_lines",
    "purchase_headers",
    "sales_lines",
    "sales_headers",
    "expenses",
    "income",
    "ocr_learning_dictionary",
    "reconciliation_runs",
    "report_jobs",
    "attachments",
    "settings",
    "products",
    "brands",
    "suppliers",
    "customers",
    "whatsapp_sessions",
)

#: `partners` is deliberately *not* in that list. The partners are who
#: runs the business, like the units and the product types -- part of
#: the seed, not the books. `partner_capital` is the books and is
#: cleared, so a reset leaves the same three people with nothing
#: invested, ready for the capital demonstration to be given again.


class DemoService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists(self) -> bool:
        return (
            await self._session.execute(
                select(Organization.id).where(Organization.id == DEMO_ORG_ID)
            )
        ).scalar_one_or_none() is not None

    async def ensure(self, source_org_id: uuid.UUID) -> uuid.UUID:
        """Create the demo org if it isn't there, seeded like the real one.

        Copied from `source_org_id` rather than from the migration's
        literals: the seed is whatever the live business actually has,
        including any product type or OCR template added since. A demo
        that could not read the same sheets would demonstrate nothing.

        **Runs the copies every time**, not only on creation. `_twin_id`
        makes every copy's id a function of its original, so a row
        already seeded is skipped and one added since is picked up. An
        existing demo therefore gains a new product type -- or, the case
        that forced this, the partners -- without having to be destroyed
        and rebuilt.
        """
        if not await self.exists():
            self._session.add(Organization(id=DEMO_ORG_ID, name=DEMO_ORG_NAME))
            await self._session.flush()

        # Kept in dependency order: product_types point at units,
        # ocr_templates at product_types.
        await self._copy(Unit, source_org_id)
        await self._copy(ProductType, source_org_id, remap={"default_unit_id": Unit})
        await self._copy(Warehouse, source_org_id)
        await self._copy(
            OcrTemplate,
            source_org_id,
            remap={"product_type_id": ProductType},
            skip=lambda row: row.supplier_id is not None,
        )
        # The partners themselves, so `capital` and `withdraw` can be
        # rehearsed. `user_id` deliberately points at the *real* user
        # rather than a copy: a withdrawal needs a second partner's
        # approval, and that approval has to arrive on the phone of
        # someone who can actually tap it. Nothing about the demo's
        # books reaches the real org through that link -- the user row
        # is read for a name and a number, never written.
        await self._copy(Partner, source_org_id)
        return DEMO_ORG_ID

    async def _copy(
        self,
        model: type,
        source_org_id: uuid.UUID,
        *,
        remap: dict[str, type] | None = None,
        skip: Callable[[Any], bool] | None = None,
    ) -> None:
        """One table's seed rows, org-swapped and re-keyed.

        Foreign keys between seeded tables have to be re-pointed at the
        copies -- a demo product type referencing the *real* org's KG
        unit would work until someone deleted it, and would quietly make
        the two businesses share a row.

        A row whose twin is already there is skipped, which is what lets
        `ensure` run on every switch instead of only at creation.
        """
        rows: list[Any] = list(
            (
                await self._session.execute(
                    select(model).where(model.org_id == source_org_id)  # type: ignore[attr-defined]
                )
            )
            .scalars()
            .all()
        )
        present: set[uuid.UUID] = set(
            (
                await self._session.execute(
                    select(model.id).where(model.org_id == DEMO_ORG_ID)  # type: ignore[attr-defined]
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            if skip is not None and skip(row):
                continue
            if self._twin_id(row.id) in present:
                continue
            values = {
                column.name: getattr(row, column.name)
                for column in row.__table__.columns
                if column.name not in {"id", "created_at", "updated_at"}
            }
            values["org_id"] = DEMO_ORG_ID
            for field, referenced in (remap or {}).items():
                values[field] = await self._twin(referenced, values[field])
            self._session.add(model(id=self._twin_id(row.id), **values))
        await self._session.flush()

    @staticmethod
    def _twin_id(source_id: uuid.UUID) -> uuid.UUID:
        """A demo row's id, derived from its original's.

        Derived rather than random so `ensure` is idempotent under a
        retry and so a row can be traced back to what it was copied
        from -- both of which matter more here than the ids looking
        tidy.
        """
        return uuid.uuid5(DEMO_ORG_ID, str(source_id))

    async def _twin(self, model: type, source_id: uuid.UUID | None) -> uuid.UUID | None:
        return None if source_id is None else self._twin_id(source_id)

    async def reset(self) -> dict[str, int]:
        """Empty the demo's books, keeping its seed.

        A demonstration is given more than once, and the second one
        should not open on the first one's stock. Hard deletes, not the
        soft delete the rest of the system insists on: there is no audit
        trail worth keeping for a business that was never real.
        """
        removed: dict[str, int] = {}
        for table, parent, key in _RESET_VIA_PARENT:
            result = await self._session.execute(
                text(
                    f"DELETE FROM {table} WHERE {key} IN "
                    f"(SELECT id FROM {parent} WHERE org_id = :org)"
                ).bindparams(org=DEMO_ORG_ID)
            )
            deleted = result.rowcount  # type: ignore[attr-defined]
            if deleted:
                removed[table] = deleted
        for table in _RESET_ORDER:
            result = await self._session.execute(
                text(f"DELETE FROM {table} WHERE org_id = :org").bindparams(org=DEMO_ORG_ID)
            )
            deleted = result.rowcount  # type: ignore[attr-defined]
            if deleted:
                removed[table] = deleted
        return removed

    async def summary(self) -> dict[str, int]:
        """What is in the demo right now, for the mode banner."""
        counts: dict[str, int] = {}
        for table in ("products", "purchase_headers", "sales_headers", "suppliers", "customers"):
            counts[table] = (
                await self._session.execute(
                    text(f"SELECT count(*) FROM {table} WHERE org_id = :org").bindparams(
                        org=DEMO_ORG_ID
                    )
                )
            ).scalar_one()
        return counts


def as_demo(user: User) -> User:
    """The same person, acting in the demo's books.

    The user row is detached by the time a command sees it (the
    dispatcher closes the session that loaded it), so overriding
    `org_id` here changes what every service scopes to and is never
    written back. That is the whole switch: no service, repository or
    query needed to learn about demo mode, because none of them ever
    stopped filtering by org.
    """
    user.org_id = DEMO_ORG_ID
    return user


def demo_since(started: datetime.datetime) -> str:
    minutes = int((datetime.datetime.now(datetime.UTC) - started).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    return f"{minutes // 60}h ago"
