"""Merging two parties that are one person.

Preview and apply are separate calls returning the same shape, because
the browser needs to show what will happen before it happens and the
terminal needs the same facts to print. Neither builds its own idea of
what a merge does.

`preview` runs the real operation inside a transaction that is thrown
away, so the numbers are computed rather than estimated -- an estimate
that disagrees with the commit is worse than no preview at all.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import ValidationError
from backend.models import Customer, PurchaseHeader, SalesHeader, Supplier, User
from backend.services.admin.guard import guarded
from backend.services.audit_service import AuditService
from backend.services.reversal_service import ReversalService

ZERO = decimal.Decimal("0")


@dataclasses.dataclass(frozen=True)
class MergePlan:
    kind: str
    loser_id: uuid.UUID
    loser_name: str
    winner_id: uuid.UUID
    winner_name: str
    transaction_ids: list[uuid.UUID]
    moved_value: decimal.Decimal
    loser_outstanding: decimal.Decimal
    winner_outstanding: decimal.Decimal
    blockers: list[str]

    @property
    def ok(self) -> bool:
        return not self.blockers

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "loser": self.loser_name,
            "winner": self.winner_name,
            "transactions": len(self.transaction_ids),
            "moved_value": str(self.moved_value),
            "loser_outstanding": str(self.loser_outstanding),
            "winner_outstanding": str(self.winner_outstanding),
            "outstanding_after": str(self.loser_outstanding + self.winner_outstanding),
            "blockers": self.blockers,
            "ok": self.ok,
        }


class PartyMergeService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _models(self, kind: str) -> tuple[Any, Any, str]:
        if kind == "supplier":
            return Supplier, PurchaseHeader, "supplier_id"
        if kind == "customer":
            return Customer, SalesHeader, "customer_id"
        raise ValidationError("kind must be 'supplier' or 'customer'")

    async def _party(self, model: Any, org_id: uuid.UUID, name: str) -> Any:
        wanted = " ".join(name.split()).casefold()
        rows = list(
            (
                await self._session.execute(
                    select(model).where(model.org_id == org_id, model.deleted_at.is_(None))
                )
            ).scalars()
        )
        exact = [r for r in rows if " ".join(r.name.split()).casefold() == wanted]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise ValidationError(f"{len(exact)} parties are called {name!r}")
        # Named, never assumed. A lone near-match taken silently is what
        # filed three sales under the wrong customer.
        near = [r for r in rows if wanted in " ".join(r.name.split()).casefold()]
        if near:
            raise ValidationError(
                f"no party exactly named {name!r}. Did you mean "
                + ", ".join(sorted(r.name for r in near))
                + "?"
            )
        raise ValidationError(f"no party named {name!r}")

    async def plan(self, org_id: uuid.UUID, *, kind: str, loser: str, winner: str) -> MergePlan:
        """What this merge would do. Reads only."""
        model, header_model, fk = self._models(kind)
        losing = await self._party(model, org_id, loser)
        winning = await self._party(model, org_id, winner)
        if losing.id == winning.id:
            raise ValidationError("those are the same party")

        rows = (
            await self._session.execute(
                select(header_model.id, header_model.grand_total, header_model.amount_paid).where(
                    getattr(header_model, fk) == losing.id,
                    header_model.deleted_at.is_(None),
                )
            )
        ).all()

        blockers: list[str] = []
        moved = sum((decimal.Decimal(total) for _, total, _ in rows), ZERO)

        async def outstanding(party_id: uuid.UUID) -> decimal.Decimal:
            value = (
                await self._session.execute(
                    select(
                        func.coalesce(
                            func.sum(header_model.grand_total - header_model.amount_paid), ZERO
                        )
                    ).where(
                        getattr(header_model, fk) == party_id,
                        header_model.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
            return decimal.Decimal(value)

        return MergePlan(
            kind=kind,
            loser_id=losing.id,
            loser_name=losing.name,
            winner_id=winning.id,
            winner_name=winning.name,
            transaction_ids=[row_id for row_id, _, _ in rows],
            moved_value=moved,
            loser_outstanding=await outstanding(losing.id),
            winner_outstanding=await outstanding(winning.id),
            blockers=blockers,
        )

    async def apply(
        self, org_id: uuid.UUID, actor: User, plan: MergePlan, *, dry_run: bool = False
    ) -> dict[str, Any]:
        """Do it, under the guard.

        The manifest is written *before* the update, so the rows it
        records are the ones as they were -- afterwards there is nothing
        left to read the old value from, which is exactly why a merge
        that recorded only a count could never be undone.
        """
        if not plan.ok:
            raise ValidationError("; ".join(plan.blockers))
        model, header_model, fk = self._models(plan.kind)

        async with guarded(self._session, org_id, dry_run=dry_run) as report:
            manifest = await ReversalService(self._session).record(
                org_id,
                actor,
                operation="merge_party",
                subject=f"{plan.loser_name} → {plan.winner_name}",
                moved=[
                    {
                        "table": header_model.__tablename__,
                        "id": str(row_id),
                        "column": fk,
                        "from": str(plan.loser_id),
                        "to": str(plan.winner_id),
                    }
                    for row_id in plan.transaction_ids
                ],
                hidden=[{"table": f"{plan.kind}s", "id": str(plan.loser_id)}],
            )
            await self._session.execute(
                update(header_model)
                .where(getattr(header_model, fk) == plan.loser_id)
                .values(**{fk: plan.winner_id})
            )
            losing = await self._session.get(model, plan.loser_id)
            if losing is not None:
                losing.deleted_at = datetime.datetime.now(datetime.UTC)
            await self._session.flush()

            report.note(f"{len(plan.transaction_ids)} transaction(s) moved")
            report.note(f"{plan.loser_name} removed")
            await AuditService(self._session).record(
                org_id,
                actor.id,
                action=f"{plan.kind}.merged",
                entity_type=f"{plan.kind}s",
                entity_id=plan.winner_id,
                before_state={"merged": plan.loser_name},
                after_state={
                    "into": plan.winner_name,
                    "moved": len(plan.transaction_ids),
                    "manifest": str(manifest.id),
                },
                channel="cli",
            )
            reversal_id = str(manifest.id)[:8]

        return {
            **plan.as_dict(),
            "committed": report.committed,
            "dry_run": report.dry_run,
            "backup": report.backup,
            "notes": report.notes,
            "reversal": reversal_id,
        }
