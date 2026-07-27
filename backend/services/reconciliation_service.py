"""Nightly integrity checks -- docs/11_BackgroundWorkers.md §6,
docs/03_Inventory.md §6, docs/06_Accounting.md §12.

**Detect, never silently fix.** Every check here replays the
append-only source of truth, compares it against the cached snapshot,
and on disagreement records the detail and alerts an owner. None of
them writes a correction. A job that quietly repaired a financial
number would destroy the only evidence that something upstream is
wrong, and the next occurrence would look like the first.

Recording a *successful* run matters as much as recording a failed
one: without the `reconciliation_runs` row, "nothing was wrong" and
"the job never fired" are the same silence.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.logging import get_logger
from backend.models import (
    BankLedger,
    CashLedger,
    Inventory,
    InventoryMovement,
    JournalLine,
    PartnerCapital,
    Product,
    ReconciliationRun,
)

logger = get_logger(__name__)

ZERO = decimal.Decimal("0")


@dataclasses.dataclass(frozen=True)
class Discrepancy:
    subject: str
    cached: str
    replayed: str

    def as_dict(self) -> dict[str, str]:
        return {"subject": self.subject, "cached": self.cached, "replayed": self.replayed}


@dataclasses.dataclass(frozen=True)
class ReconciliationOutcome:
    kind: str
    checked: int
    discrepancies: list[Discrepancy]

    @property
    def ok(self) -> bool:
        return not self.discrepancies

    def alert_text(self) -> str:
        """Copy per docs/03_Inventory.md §6.2 -- names both numbers and
        says plainly that nothing was changed."""
        lines = [
            f"⚠️ {self.kind.capitalize()} mismatch detected "
            f"({len(self.discrepancies)} of {self.checked} checked):"
        ]
        for item in self.discrepancies[:10]:
            lines.append(
                f"• {item.subject}: system shows {item.cached}, replay shows {item.replayed}"
            )
        if len(self.discrepancies) > 10:
            lines.append(f"…and {len(self.discrepancies) - 10} more.")
        lines.append("Not auto-corrected — please review.")
        return "\n".join(lines)


class ReconciliationService:
    """The shared runner docs/11_BackgroundWorkers.md §6 asks for: a new
    check added later inherits detect-don't-fix by construction, because
    `run` is the only way to record an outcome and it never writes to
    the tables it is checking."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def run(self, org_id: uuid.UUID, kind: str) -> ReconciliationOutcome:
        started = datetime.datetime.now(datetime.UTC)
        if kind == "inventory":
            outcome = await self.check_inventory(org_id)
        elif kind == "ledger":
            outcome = await self.check_ledgers(org_id)
        else:  # pragma: no cover -- guarded by the task definitions
            raise ValueError(f"unknown reconciliation kind {kind!r}")

        self._session.add(
            ReconciliationRun(
                org_id=org_id,
                kind=kind,
                status="ok" if outcome.ok else "mismatch",
                checked_count=outcome.checked,
                mismatch_count=len(outcome.discrepancies),
                details=[d.as_dict() for d in outcome.discrepancies] or None,
                started_at=started,
                finished_at=datetime.datetime.now(datetime.UTC),
            )
        )
        await self._session.flush()
        if not outcome.ok:
            logger.error(
                "reconciliation_mismatch",
                kind=kind,
                org_id=str(org_id),
                count=len(outcome.discrepancies),
                details=[d.as_dict() for d in outcome.discrepancies],
            )
        else:
            logger.info("reconciliation_ok", kind=kind, org_id=str(org_id), checked=outcome.checked)
        return outcome

    async def check_inventory(self, org_id: uuid.UUID) -> ReconciliationOutcome:
        """`inventory.qty_on_hand` must equal the signed sum of that
        product's movements -- CLAUDE.md's standing acceptance
        criterion, checked here against live data rather than only in
        per-feature tests."""
        replayed = {
            (product_id, warehouse_id): total
            for product_id, warehouse_id, total in (
                await self._session.execute(
                    select(
                        InventoryMovement.product_id,
                        InventoryMovement.warehouse_id,
                        func.coalesce(func.sum(InventoryMovement.qty_delta), ZERO),
                    )
                    .where(InventoryMovement.org_id == org_id)
                    .group_by(InventoryMovement.product_id, InventoryMovement.warehouse_id)
                )
            ).all()
        }

        rows = (
            await self._session.execute(
                select(
                    Inventory.product_id,
                    Inventory.warehouse_id,
                    Inventory.qty_on_hand,
                    Product.code,
                )
                .join(Product, Product.id == Inventory.product_id)
                .where(Inventory.org_id == org_id)
            )
        ).all()

        discrepancies: list[Discrepancy] = []
        for product_id, warehouse_id, cached, code in rows:
            expected = replayed.pop((product_id, warehouse_id), ZERO)
            if cached != expected:
                discrepancies.append(
                    Discrepancy(subject=code, cached=str(cached), replayed=str(expected))
                )
        # movements for a product with no inventory row at all: the cache
        # is missing, not merely stale, which is worth naming separately
        for (product_id, _warehouse_id), total in replayed.items():
            if total != ZERO:
                discrepancies.append(
                    Discrepancy(
                        subject=f"product {product_id} (no inventory row)",
                        cached="—",
                        replayed=str(total),
                    )
                )
        return ReconciliationOutcome(
            kind="inventory", checked=len(rows), discrepancies=discrepancies
        )

    async def check_ledgers(self, org_id: uuid.UUID) -> ReconciliationOutcome:
        """docs/06_Accounting.md §12: cash/bank/capital running balances
        re-summed from scratch, plus the journal's per-entry balance."""
        discrepancies: list[Discrepancy] = []
        checked = 0

        for name, model in (("cash", CashLedger), ("bank", BankLedger)):
            checked += 1
            total = (
                await self._session.execute(
                    select(func.coalesce(func.sum(model.amount), ZERO)).where(
                        model.org_id == org_id
                    )
                )
            ).scalar_one()
            snapshot = (
                await self._session.execute(
                    select(model.resulting_balance)
                    .where(model.org_id == org_id)
                    .order_by(model.created_at.desc(), model.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none() or ZERO
            if snapshot != total:
                discrepancies.append(
                    Discrepancy(
                        subject=f"{name} balance", cached=str(snapshot), replayed=str(total)
                    )
                )

        partner_totals = (
            await self._session.execute(
                select(
                    PartnerCapital.partner_id,
                    func.coalesce(func.sum(PartnerCapital.amount), ZERO),
                )
                .where(PartnerCapital.org_id == org_id, PartnerCapital.status == "posted")
                .group_by(PartnerCapital.partner_id)
            )
        ).all()
        for partner_id, total in partner_totals:
            checked += 1
            snapshot = (
                await self._session.execute(
                    select(PartnerCapital.resulting_balance)
                    .where(
                        PartnerCapital.org_id == org_id,
                        PartnerCapital.partner_id == partner_id,
                        PartnerCapital.status == "posted",
                    )
                    .order_by(PartnerCapital.posted_at.desc(), PartnerCapital.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none() or ZERO
            if snapshot != total:
                discrepancies.append(
                    Discrepancy(
                        subject=f"partner {partner_id} capital",
                        cached=str(snapshot),
                        replayed=str(total),
                    )
                )

        # §12.2: structurally guaranteed by JournalService, checked anyway
        # -- "should never happen" and "provably never happens" differ
        unbalanced = (
            (
                await self._session.execute(
                    select(JournalLine.journal_id)
                    .group_by(JournalLine.journal_id)
                    .having(func.sum(JournalLine.debit) != func.sum(JournalLine.credit))
                )
            )
            .scalars()
            .all()
        )
        checked += 1
        for journal_id in unbalanced:
            discrepancies.append(
                Discrepancy(
                    subject=f"journal {journal_id}", cached="unbalanced", replayed="debits≠credits"
                )
            )

        return ReconciliationOutcome(kind="ledger", checked=checked, discrepancies=discrepancies)
