"""The safety net, with no terminal attached.

This is `backend/admin/harness.py`'s logic with the printing taken out,
so an HTTP handler can run an operation under exactly the same
protections the CLI does -- rather than a similar set, written twice,
that drift apart on the day one of them is fixed.

The sequence, and why each step is where it is:

    baseline snapshot   what is *already* wrong, so repairing broken
                        books is not blocked by the breakage
    backup              before the transaction opens, because it is the
                        only thing that survives a bug in this file
    one transaction     the work
    snapshot again      inside it, so it sees the uncommitted result
    commit or roll back a regression means it did not happen
"""

from __future__ import annotations

import contextlib
import dataclasses
import decimal
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Brand, Inventory, Product
from backend.services.backup_service import BackupService
from backend.services.reconciliation_service import Discrepancy, ReconciliationService


class GuardRegression(Exception):
    """The books stopped balancing, so the work was thrown away."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class _DryRun(Exception):
    """Internal: unwinds a preview. Not an error."""


@dataclasses.dataclass
class GuardReport:
    """What the operation did, for a caller to render however it likes."""

    notes: list[str] = dataclasses.field(default_factory=list)
    pre_existing: dict[str, decimal.Decimal] = dataclasses.field(default_factory=dict)
    backup: str | None = None
    committed: bool = False
    dry_run: bool = False

    def note(self, text: str) -> None:
        self.notes.append(text)


def _gap(discrepancy: Discrepancy) -> decimal.Decimal:
    try:
        return abs(decimal.Decimal(discrepancy.cached) - decimal.Decimal(discrepancy.replayed))
    except (decimal.InvalidOperation, ValueError):
        return (
            decimal.Decimal(0) if discrepancy.cached == discrepancy.replayed else decimal.Decimal(1)
        )


async def _negative_stock(session: AsyncSession, org_id: uuid.UUID) -> dict[str, decimal.Decimal]:
    """Products holding less than nothing.

    Reconciliation cannot catch this -- it proves `qty_on_hand` equals
    the signed sum of movements, and -800 satisfies that perfectly well.
    Internally consistent and physically impossible.
    """
    rows = (
        await session.execute(
            select(Product.code, Brand.name, Inventory.qty_on_hand)
            .join(Product, Product.id == Inventory.product_id)
            .join(Brand, Brand.id == Product.brand_id, isouter=True)
            .where(Inventory.org_id == org_id, Inventory.qty_on_hand < 0)
        )
    ).all()
    return {f"negative stock:{brand or '—'} {code}": abs(qty) for code, brand, qty in rows}


async def snapshot(session: AsyncSession, org_id: uuid.UUID) -> dict[str, decimal.Decimal]:
    """Everything wrong right now, by subject, with its size."""
    service = ReconciliationService(session)
    found: dict[str, decimal.Decimal] = {}
    for outcome in (
        await service.check_inventory(org_id),
        await service.check_ledgers(org_id),
    ):
        for discrepancy in outcome.discrepancies:
            found[f"{outcome.kind}:{discrepancy.subject}"] = _gap(discrepancy)
    found.update(await _negative_stock(session, org_id))
    return found


def regressions(before: dict[str, decimal.Decimal], after: dict[str, decimal.Decimal]) -> list[str]:
    """A regression is a subject that became wrong, or got wronger.

    Comparing only "was it listed" would let an operation double an
    existing mismatch and call it no change; comparing only the count
    would let one problem be swapped for another.
    """
    problems = []
    for subject, gap in sorted(after.items()):
        was = before.get(subject)
        if was is None:
            problems.append(f"{subject}: now off by {gap}")
        elif gap > was:
            problems.append(f"{subject}: was off by {was}, now off by {gap}")
    return problems


@contextlib.asynccontextmanager
async def guarded(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    dry_run: bool = False,
    backup: bool = True,
) -> AsyncIterator[GuardReport]:
    """Run a mutation so that it either leaves the books balanced or does
    not happen at all.

    `dry_run` is what makes a Preview honest: the operation genuinely
    runs, the numbers reported are computed rather than estimated, and
    the transaction is thrown away.
    """
    report = GuardReport(dry_run=dry_run)
    report.pre_existing = await snapshot(session, org_id)

    if backup and not dry_run:
        record = await BackupService(session).create_backup(org_id)
        report.backup = Path(record.file_path).name

    # The snapshot's SELECTs autobegin a transaction, and `begin()` then
    # refuses. Released with commit() rather than rollback(): nothing was
    # written either way, but rollback expires every loaded instance and
    # the next attribute touch re-queries -- inside a flush that raises
    # MissingGreenlet instead of doing IO.
    if session.in_transaction():
        await session.commit()

    try:
        async with session.begin():
            yield report
            after = await snapshot(session, org_id)
            problems = regressions(report.pre_existing, after)
            if problems:
                raise GuardRegression(problems)
            if dry_run:
                raise _DryRun
            report.committed = True
    except _DryRun:
        return
