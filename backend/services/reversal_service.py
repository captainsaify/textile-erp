"""Recording what an operation moved, and putting it back.

plan.md §10. An operation that claims to be reversible has to earn it,
and the way it earns it is by writing down -- at the time, by primary
key -- every row it touched and the value that row held before.

The reason this is not simply "run the merge backwards" is time. Two
months pass. Rows get edited, moved again by a *later* merge, purged, or
paid against. So reversal never just acts: it classifies every row in
the manifest against current state first, and refuses as a whole if any
of them has drifted in a way that makes putting it back a lie.

Rows *created after* the manifest are never touched. They were never the
restored party's. That single rule is what keeps a reversal from
inventing history.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Any

from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import NotFoundError, ValidationError
from backend.models import (
    AuditLog,
    Brand,
    Customer,
    Product,
    PurchaseHeader,
    ReversalManifest,
    SalesHeader,
    Supplier,
    User,
)

#: Only tables a manifest is allowed to name. An allow-list rather than
#: a lookup by string, so a typo in an operation cannot reach an
#: arbitrary table.
TABLES: dict[str, Any] = {
    "purchase_headers": PurchaseHeader,
    "sales_headers": SalesHeader,
    "products": Product,
    "brands": Brand,
    "suppliers": Supplier,
    "customers": Customer,
}

#: Headers can have money allocated against them; the others cannot.
SETTLEABLE = {"purchase_headers", "sales_headers"}


@dataclasses.dataclass(frozen=True)
class RowState:
    table: str
    row_id: uuid.UUID
    column: str
    previous: str | None
    expected: str | None
    state: str  # intact | modified | re-pointed | missing | entangled
    detail: str = ""

    @property
    def blocks(self) -> bool:
        return self.state in {"re-pointed", "missing", "entangled"}


@dataclasses.dataclass(frozen=True)
class ReversalPlan:
    manifest: ReversalManifest
    rows: list[RowState]

    @property
    def blocked(self) -> list[RowState]:
        return [r for r in self.rows if r.blocks]

    @property
    def movable(self) -> list[RowState]:
        return [r for r in self.rows if not r.blocks]

    @property
    def ok(self) -> bool:
        return not self.blocked


class ReversalService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- writing ------------------------------------------------------

    async def record(
        self,
        org_id: uuid.UUID,
        actor: User,
        *,
        operation: str,
        subject: str,
        moved: list[dict[str, Any]] | None = None,
        hidden: list[dict[str, Any]] | None = None,
        created: list[dict[str, Any]] | None = None,
    ) -> ReversalManifest:
        manifest = ReversalManifest(
            org_id=org_id,
            operation=operation,
            subject=subject,
            actor_user_id=actor.id,
            payload={
                "moved": moved or [],
                "hidden": hidden or [],
                "created": created or [],
            },
        )
        self._session.add(manifest)
        await self._session.flush()
        return manifest

    # --- reading ------------------------------------------------------

    async def open_manifests(self, org_id: uuid.UUID, limit: int = 20) -> list[ReversalManifest]:
        return list(
            (
                await self._session.execute(
                    select(ReversalManifest)
                    .where(
                        ReversalManifest.org_id == org_id,
                        ReversalManifest.reversed_at.is_(None),
                    )
                    .order_by(ReversalManifest.created_at.desc())
                    .limit(limit)
                )
            ).scalars()
        )

    async def get(self, org_id: uuid.UUID, reference: str) -> ReversalManifest:
        manifest = (
            (
                await self._session.execute(
                    select(ReversalManifest).where(
                        ReversalManifest.org_id == org_id,
                        cast(ReversalManifest.id, String).ilike(f"{reference.strip().lower()}%"),
                    )
                )
            )
            .scalars()
            .first()
        )
        if manifest is None:
            raise NotFoundError("reversal", reference)
        return manifest

    # --- classifying --------------------------------------------------

    async def plan(self, manifest: ReversalManifest) -> ReversalPlan:
        """Work out, row by row, whether putting this back is honest."""
        if manifest.reversed_at is not None:
            raise ValidationError("That has already been reversed.")

        rows: list[RowState] = []
        for entry in manifest.payload.get("moved", []):
            rows.append(await self._classify(manifest, entry))
        return ReversalPlan(manifest=manifest, rows=rows)

    async def _classify(self, manifest: ReversalManifest, entry: dict[str, Any]) -> RowState:
        table = str(entry.get("table", ""))
        column = str(entry.get("column", ""))
        previous = entry.get("from")
        expected = entry.get("to")
        model = TABLES.get(table)
        row_id = uuid.UUID(str(entry.get("id")))

        def state(kind: str, detail: str = "") -> RowState:
            return RowState(
                table=table,
                row_id=row_id,
                column=column,
                previous=None if previous is None else str(previous),
                expected=None if expected is None else str(expected),
                state=kind,
                detail=detail,
            )

        if model is None:
            return state("missing", f"unknown table {table!r}")

        row = await self._session.get(model, row_id)
        if row is None or getattr(row, "deleted_at", None) is not None:
            return state("missing", "purged or deleted since the operation")

        current = getattr(row, column, None)
        if str(current) != str(expected):
            return state(
                "re-pointed",
                f"now {current}, not where the operation left it — moved again since",
            )

        # Money is the case that actually bites: a receipt taken after
        # the operation, allocated across bills that are about to move
        # back, would be settling someone else's bill afterwards.
        if table in SETTLEABLE:
            payment = await self._allocated_after(manifest, row_id)
            if payment is not None:
                return state(
                    "entangled",
                    f"payment {payment} was allocated against it after the operation",
                )

        updated = getattr(row, "updated_at", None)
        if updated is not None and updated > manifest.created_at:
            return state("modified", "edited since — it will still go back")
        return state("intact")

    async def _allocated_after(self, manifest: ReversalManifest, row_id: uuid.UUID) -> str | None:
        """Any payment recorded after the operation that landed on this bill.

        Only findable because allocations now carry `header_id`; the old
        (party, invoice_no) key would have been invalidated by the very
        operation being reversed."""
        probe: list[dict[str, str]] = [{"header_id": str(row_id)}]
        entry = (
            (
                await self._session.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.org_id == manifest.org_id,
                        AuditLog.action.in_(["payment.paid", "payment.received"]),
                        AuditLog.created_at > manifest.created_at,
                        AuditLog.after_state["allocations"].contains(probe),
                    )
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        return str(entry.id)[:8] if entry is not None else None

    # --- reversing ----------------------------------------------------

    async def apply(self, plan: ReversalPlan, actor: User) -> int:
        """Put the rows back. Callers check `plan.ok` first, and the
        guard around them re-checks the books afterwards."""
        if not plan.ok:
            raise ValidationError("that reversal is blocked; nothing was changed.")
        moved = 0
        for row in plan.movable:
            model = TABLES[row.table]
            record = await self._session.get(model, row.row_id)
            if record is None:
                continue
            setattr(
                record,
                row.column,
                uuid.UUID(row.previous) if row.previous else None,
            )
            moved += 1

        for entry in plan.manifest.payload.get("hidden", []):
            model = TABLES.get(str(entry.get("table", "")))
            if model is None:
                continue
            record = await self._session.get(model, uuid.UUID(str(entry["id"])))
            if record is not None:
                record.deleted_at = None
                if hasattr(record, "purged_at"):
                    record.purged_at = None

        plan.manifest.reversed_at = datetime.datetime.now(datetime.UTC)
        plan.manifest.reversed_by = actor.id
        await self._session.flush()
        return moved
