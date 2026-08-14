"""Taking a record out of the books.

A deep delete, not a hard one: the record leaves every report, total,
ledger, search and reconciliation, and the rows stay so a purge aimed at
the wrong invoice is one mistake rather than two.

Preview and apply return the same shape, so the browser can show what
will happen and the terminal can print it without either building its
own idea of what a purge does.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import NotFoundError, ValidationError
from backend.models import (
    InventoryMovement,
    PurchaseHeader,
    PurchaseLine,
    User,
)
from backend.models.enums import PurchaseStatus
from backend.services.admin.guard import guarded
from backend.services.audit_service import AuditService
from backend.services.cost_replay_service import CostReplayService
from backend.services.reversal_service import ReversalService
from backend.services.undo_service import UndoService

ZERO = decimal.Decimal("0")


@dataclasses.dataclass(frozen=True)
class PurgePlan:
    kind: str
    header_id: uuid.UUID
    label: str
    grand_total: decimal.Decimal
    amount_paid: decimal.Decimal
    lines: int
    live: bool
    blockers: list[str]

    @property
    def ok(self) -> bool:
        return not self.blockers

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reference": self.label,
            "grand_total": str(self.grand_total),
            "amount_paid": str(self.amount_paid),
            "lines": self.lines,
            "carries_stock": self.live,
            "blockers": self.blockers,
            "ok": self.ok,
        }


class PurgeService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def plan(self, org_id: uuid.UUID, *, kind: str, reference: str) -> PurgePlan:
        if kind == "sale":
            return await self._sale_plan(org_id, reference)
        if kind != "purchase":
            raise ValidationError("kind must be 'purchase' or 'sale'")

        header = (
            (
                await self._session.execute(
                    select(PurchaseHeader).where(
                        PurchaseHeader.org_id == org_id,
                        PurchaseHeader.invoice_no == reference.strip(),
                        PurchaseHeader.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .first()
        )
        if header is None:
            raise NotFoundError("purchase", reference)
        label = header.invoice_no
        live = header.status is PurchaseStatus.CONFIRMED
        lines = len(
            (
                await self._session.execute(
                    select(PurchaseLine.id).where(PurchaseLine.purchase_header_id == header.id)
                )
            ).all()
        )

        blockers: list[str] = []
        if header.amount_paid > ZERO:
            # Purging would leave the money recorded against nothing.
            blockers.append(
                f"{header.amount_paid} has been paid against it — reverse that payment first"
            )

        return PurgePlan(
            kind=kind,
            header_id=header.id,
            label=label,
            grand_total=header.grand_total,
            amount_paid=header.amount_paid,
            lines=lines,
            live=live,
            blockers=blockers,
        )

    async def _sale_plan(self, org_id: uuid.UUID, reference: str) -> PurgePlan:
        """A sale, found by the first characters of its id -- which is
        what every message the system sends already shows."""
        from sqlalchemy import String, cast

        from backend.models import SalesHeader, SalesLine

        rows = list(
            (
                await self._session.execute(
                    select(SalesHeader)
                    .where(
                        SalesHeader.org_id == org_id,
                        SalesHeader.deleted_at.is_(None),
                        cast(SalesHeader.id, String).ilike(f"{reference.strip().lower()}%"),
                    )
                    .limit(5)
                )
            ).scalars()
        )
        if not rows:
            raise NotFoundError("sale", reference)
        if len(rows) > 1:
            raise ValidationError(f"{reference!r} matches {len(rows)} sales — use more characters")
        header = rows[0]

        lines = len(
            (
                await self._session.execute(
                    select(SalesLine.id).where(SalesLine.sales_header_id == header.id)
                )
            ).all()
        )
        blockers: list[str] = []
        if header.amount_paid > ZERO:
            blockers.append(
                f"{header.amount_paid} has been received against it — reverse that receipt first"
            )
        return PurgePlan(
            kind="sale",
            header_id=header.id,
            label=str(header.id)[:8],
            grand_total=header.grand_total,
            amount_paid=header.amount_paid,
            lines=lines,
            live=header.status == "confirmed",
            blockers=blockers,
        )

    async def _movements(
        self, org_id: uuid.UUID, header_id: uuid.UUID, kind: str = "purchase"
    ) -> list[Any]:
        from backend.models import SalesLine

        if kind == "purchase":
            line_ids = list(
                (
                    await self._session.execute(
                        select(PurchaseLine.id).where(PurchaseLine.purchase_header_id == header_id)
                    )
                ).scalars()
            )
            source = "purchase_line"
        else:
            line_ids = list(
                (
                    await self._session.execute(
                        select(SalesLine.id).where(SalesLine.sales_header_id == header_id)
                    )
                ).scalars()
            )
            source = "sales_line"
        if not line_ids:
            return []
        return list(
            (
                await self._session.execute(
                    select(InventoryMovement).where(
                        InventoryMovement.org_id == org_id,
                        InventoryMovement.source_type == source,
                        InventoryMovement.source_id.in_(line_ids),
                    )
                )
            ).scalars()
        )

    async def apply(
        self, org_id: uuid.UUID, actor: User, plan: PurgePlan, *, dry_run: bool = False
    ) -> dict[str, Any]:
        if not plan.ok:
            raise ValidationError("; ".join(plan.blockers))

        async with guarded(self._session, org_id, dry_run=dry_run) as report:
            created: list[dict[str, str]] = []
            if plan.live:
                # Undoing adds compensating movements rather than
                # deleting the originals, so the set it creates is what a
                # restore has to remove. Captured by diffing ids around
                # the call, not by matching type and time -- that would
                # be a guess, wrong the day two operations share a second.
                before = {m.id for m in await self._movements(org_id, plan.header_id, plan.kind)}
                await UndoService(self._session).undo_in_transaction(
                    actor, entity=plan.kind, reference=plan.label
                )
                created = [
                    {
                        "table": "inventory_movements",
                        "id": str(m.id),
                        "product_id": str(m.product_id),
                        "warehouse_id": str(m.warehouse_id),
                    }
                    for m in await self._movements(org_id, plan.header_id, plan.kind)
                    if m.id not in before
                ]
                report.note(f"stock and journal unwound ({len(created)} movement(s))")

            from backend.models import SalesHeader

            # Fetched per branch rather than through a variable model:
            # both carry deleted_at and purged_at, but a `type[Base]`
            # loses that and mypy is right to say so.
            header: PurchaseHeader | SalesHeader | None
            if plan.kind == "purchase":
                header = await self._session.get(PurchaseHeader, plan.header_id)
            else:
                header = await self._session.get(SalesHeader, plan.header_id)
            if header is None:
                raise NotFoundError(plan.kind, plan.label)
            now = datetime.datetime.now(datetime.UTC)
            header.deleted_at = now
            header.purged_at = now
            await self._session.flush()
            report.note("hidden from every report, total and reconciliation")

            manifest = await ReversalService(self._session).record(
                org_id,
                actor,
                operation="purge",
                subject=f"{plan.kind} {plan.label}",
                hidden=[
                    {
                        "table": "purchase_headers" if plan.kind == "purchase" else "sales_headers",
                        "id": str(plan.header_id),
                    }
                ],
                created=created,
            )
            await AuditService(self._session).record(
                org_id,
                actor.id,
                action=f"{plan.kind}.purged",
                entity_type="purchase_headers" if plan.kind == "purchase" else "sales_headers",
                entity_id=plan.header_id,
                after_state={"reference": plan.label, "stock_reversed": plan.live},
                channel="cli",
            )
            reversal = str(manifest.id)[:8]

        return {
            **plan.as_dict(),
            "committed": report.committed,
            "dry_run": report.dry_run,
            "backup": report.backup,
            "notes": report.notes,
            "reversal": reversal,
        }

    async def restore(self, org_id: uuid.UUID, actor: User, reference: str) -> dict[str, Any]:
        """Bring it back, stock and all.

        The purge recorded which compensating movements it created;
        without that this could only unhide the record and the goods
        would stay reversed -- a bill in every total with nothing behind
        it.
        """
        header = (
            (
                await self._session.execute(
                    select(PurchaseHeader).where(
                        PurchaseHeader.org_id == org_id,
                        PurchaseHeader.invoice_no == reference.strip(),
                        PurchaseHeader.purged_at.is_not(None),
                    )
                )
            )
            .scalars()
            .first()
        )
        if header is None:
            raise NotFoundError("purged purchase", reference)

        from backend.models import ReversalManifest

        manifest = (
            (
                await self._session.execute(
                    select(ReversalManifest).where(
                        ReversalManifest.org_id == org_id,
                        ReversalManifest.operation == "purge",
                        ReversalManifest.reversed_at.is_(None),
                        ReversalManifest.payload["hidden"].contains([{"id": str(header.id)}]),
                    )
                )
            )
            .scalars()
            .first()
        )

        async with guarded(self._session, org_id) as report:
            header.deleted_at = None
            header.purged_at = None
            await self._session.flush()
            report.note("visible again in reports and totals")

            if manifest is None:
                report.note("no purge record — this predates them, so the stock stays reversed")
            else:
                replay = CostReplayService(self._session)
                touched: set[tuple[uuid.UUID, uuid.UUID]] = set()
                for entry in manifest.payload.get("created", []):
                    movement = (
                        (
                            await self._session.execute(
                                select(InventoryMovement).where(
                                    InventoryMovement.id == uuid.UUID(str(entry["id"]))
                                )
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if movement is None:
                        continue
                    touched.add((movement.product_id, movement.warehouse_id))
                    await self._session.delete(movement)
                await self._session.flush()
                for product_id, warehouse_id in sorted(touched, key=str):
                    await replay.replay(org_id, product_id, warehouse_id)
                report.note(f"stock restored on {len(touched)} product(s)")
                manifest.reversed_at = datetime.datetime.now(datetime.UTC)
                manifest.reversed_by = actor.id

        return {
            "reference": header.invoice_no,
            "committed": report.committed,
            "notes": report.notes,
        }
