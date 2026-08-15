"""Two products that are one product, and products that were never one.

Inline item creation made this necessary. Before it, a code that did not
exist stopped the entry; now it offers to create one, which is the right
trade for entry speed and the wrong one for the catalogue — `55X` typed
twice under `TOP` and `TOP ` is two products holding half the stock each,
and every average cost, stock figure and reorder level is then computed
on half the history.

Merging products is a harder operation than merging parties, and the
difference is cost. A party merge moves ownership of bills; nothing about
the goods changes. A product merge moves *movements*, and the weighted
average is a running function of movement order — so the surviving
product's cost has to be replayed from the combined history, not
inherited from either side. That replay is the whole reason this cannot
be an UPDATE and a soft delete.

Deleting is the small case and is deliberately narrow: only a product
nothing has ever happened to. Anything else is a merge.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import NotFoundError, ValidationError
from backend.models import (
    Brand,
    Inventory,
    InventoryMovement,
    Product,
    PurchaseLine,
    ReversalManifest,
    SalesLine,
    User,
)
from backend.services.admin.guard import guarded
from backend.services.audit_service import AuditService
from backend.services.cost_replay_service import CostReplayService
from backend.services.reversal_service import ReversalService

ZERO = decimal.Decimal("0")


def label_of(code: str, brand: str | None) -> str:
    """How a product is named to a person. A code alone is ambiguous on
    these books — three brands carry `55X` — so the brand is never
    dropped, even when it is missing."""
    return f"{code} ({brand or 'no label'})"


@dataclasses.dataclass(frozen=True)
class ProductMergePlan:
    loser_id: uuid.UUID
    loser_label: str
    winner_id: uuid.UUID
    winner_label: str
    purchase_lines: list[uuid.UUID]
    sales_lines: list[uuid.UUID]
    movements: list[uuid.UUID]
    loser_qty: decimal.Decimal
    winner_qty: decimal.Decimal
    loser_avg: decimal.Decimal
    winner_avg: decimal.Decimal
    blockers: list[str]

    @property
    def ok(self) -> bool:
        return not self.blockers

    def as_dict(self) -> dict[str, Any]:
        return {
            "loser": self.loser_label,
            "winner": self.winner_label,
            "purchase_lines": len(self.purchase_lines),
            "sales_lines": len(self.sales_lines),
            "movements": len(self.movements),
            "loser_qty": str(self.loser_qty),
            "winner_qty": str(self.winner_qty),
            "loser_avg": str(self.loser_avg),
            "winner_avg": str(self.winner_avg),
            "qty_after": str(self.loser_qty + self.winner_qty),
            "blockers": self.blockers,
            "ok": self.ok,
        }


class ProductAdminService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- reading ------------------------------------------------------

    async def resolve(self, org_id: uuid.UUID, code: str, brand: str | None) -> Product:
        """Find exactly one product, or say why not.

        Never picks a lone near-match. Taking the nearest name silently
        is what filed three sales under the wrong customer, and doing it
        here would merge the wrong two products — an operation whose
        whole promise is that it is reversible, made on a product the
        person never named.
        """
        wanted = code.strip().casefold()
        rows = (
            await self._session.execute(
                select(Product, Brand.name)
                .join(Brand, Brand.id == Product.brand_id, isouter=True)
                .where(
                    Product.org_id == org_id,
                    Product.deleted_at.is_(None),
                    func.lower(Product.code) == wanted,
                )
            )
        ).all()
        if not rows:
            raise NotFoundError("product", code)

        if brand is not None:
            target = " ".join(brand.split()).casefold()
            rows = [r for r in rows if " ".join((r[1] or "").split()).casefold() == target]
            if not rows:
                raise NotFoundError("product", f"{code} under {brand}")
        if len(rows) > 1:
            names = ", ".join(sorted(str(name or "no label") for _, name in rows))
            raise ValidationError(f"{code} exists under {len(rows)} labels ({names}) — name one")
        product: Product = rows[0][0]
        return product

    async def _stock(
        self, org_id: uuid.UUID, product_id: uuid.UUID
    ) -> tuple[decimal.Decimal, decimal.Decimal]:
        row = (
            await self._session.execute(
                select(
                    func.coalesce(func.sum(Inventory.qty_on_hand), ZERO),
                    func.coalesce(func.max(Inventory.weighted_avg_cost), ZERO),
                ).where(Inventory.org_id == org_id, Inventory.product_id == product_id)
            )
        ).one()
        return decimal.Decimal(row[0]), decimal.Decimal(row[1])

    async def _usage(
        self, product_id: uuid.UUID
    ) -> tuple[list[uuid.UUID], list[uuid.UUID], list[uuid.UUID]]:
        purchases = list(
            (
                await self._session.execute(
                    select(PurchaseLine.id).where(PurchaseLine.product_id == product_id)
                )
            ).scalars()
        )
        sales = list(
            (
                await self._session.execute(
                    select(SalesLine.id).where(SalesLine.product_id == product_id)
                )
            ).scalars()
        )
        movements = list(
            (
                await self._session.execute(
                    select(InventoryMovement.id).where(InventoryMovement.product_id == product_id)
                )
            ).scalars()
        )
        return purchases, sales, movements

    async def catalogue(self, org_id: uuid.UUID, *, query: str = "") -> list[dict[str, Any]]:
        """Every product with what has happened to it.

        The counts are the point: they are what tells a duplicate from a
        real product, and what tells a deletable row from one that has
        history. Reading them here means the screen never has to guess.
        """
        stmt = (
            select(
                Product.id,
                Product.code,
                Brand.name,
                Product.description,
                func.coalesce(func.sum(Inventory.qty_on_hand), ZERO),
                func.coalesce(func.max(Inventory.weighted_avg_cost), ZERO),
            )
            .join(Brand, Brand.id == Product.brand_id, isouter=True)
            .join(
                Inventory,
                (Inventory.product_id == Product.id) & (Inventory.org_id == org_id),
                isouter=True,
            )
            .where(Product.org_id == org_id, Product.deleted_at.is_(None))
            .group_by(Product.id, Product.code, Brand.name, Product.description)
            .order_by(func.lower(Product.code), Brand.name)
        )
        if query.strip():
            like = f"%{query.strip()}%"
            stmt = stmt.where(Product.code.ilike(like) | Product.description.ilike(like))

        rows = (await self._session.execute(stmt)).all()

        # Counted in two grouped queries rather than one per product: the
        # catalogue is small today and would still be N+1 tomorrow.
        purchase_counts: dict[uuid.UUID, int] = {
            pid: int(n)
            for pid, n in (
                await self._session.execute(
                    select(PurchaseLine.product_id, func.count())
                    .where(PurchaseLine.org_id == org_id)
                    .group_by(PurchaseLine.product_id)
                )
            ).all()
        }
        sales_counts: dict[uuid.UUID, int] = {
            pid: int(n)
            for pid, n in (
                await self._session.execute(
                    select(SalesLine.product_id, func.count())
                    .where(SalesLine.org_id == org_id)
                    .group_by(SalesLine.product_id)
                )
            ).all()
        }

        return [
            {
                "id": str(pid),
                "code": code,
                "brand": brand or "",
                "description": description,
                "label": label_of(code, brand),
                "on_hand": str(qty),
                "avg_cost": str(avg),
                "purchases": int(purchase_counts.get(pid, 0)),
                "sales": int(sales_counts.get(pid, 0)),
                "deletable": not purchase_counts.get(pid) and not sales_counts.get(pid),
            }
            for pid, code, brand, description, qty, avg in rows
        ]

    # --- merging ------------------------------------------------------

    async def merge_plan(
        self,
        org_id: uuid.UUID,
        *,
        loser_code: str,
        loser_brand: str | None,
        winner_code: str,
        winner_brand: str | None,
    ) -> ProductMergePlan:
        losing = await self.resolve(org_id, loser_code, loser_brand)
        winning = await self.resolve(org_id, winner_code, winner_brand)
        if losing.id == winning.id:
            raise ValidationError("those are the same product")

        loser_label = label_of(losing.code, await self._brand_name(losing))
        winner_label = label_of(winning.code, await self._brand_name(winning))

        blockers: list[str] = []
        if losing.unit_id != winning.unit_id:
            # Adding 40 metres to 40 kilograms produces 80 of nothing.
            blockers.append("they are measured in different units — merging would add up nonsense")
        if losing.product_type_id != winning.product_type_id:
            blockers.append("they are different product types")

        purchases, sales, movements = await self._usage(losing.id)
        loser_qty, loser_avg = await self._stock(org_id, losing.id)
        winner_qty, winner_avg = await self._stock(org_id, winning.id)

        return ProductMergePlan(
            loser_id=losing.id,
            loser_label=loser_label,
            winner_id=winning.id,
            winner_label=winner_label,
            purchase_lines=purchases,
            sales_lines=sales,
            movements=movements,
            loser_qty=loser_qty,
            winner_qty=winner_qty,
            loser_avg=loser_avg,
            winner_avg=winner_avg,
            blockers=blockers,
        )

    async def _brand_name(self, product: Product) -> str | None:
        if product.brand_id is None:
            return None
        brand = await self._session.get(Brand, product.brand_id)
        return None if brand is None else brand.name

    async def merge_apply(
        self, org_id: uuid.UUID, actor: User, plan: ProductMergePlan, *, dry_run: bool = False
    ) -> dict[str, Any]:
        if not plan.ok:
            raise ValidationError("; ".join(plan.blockers))

        async with guarded(self._session, org_id, dry_run=dry_run) as report:
            # Written before the update, like every other manifest: once
            # the rows have moved there is nothing left to read the old
            # value from.
            manifest = await ReversalService(self._session).record(
                org_id,
                actor,
                operation="merge_product",
                subject=f"{plan.loser_label} → {plan.winner_label}",
                moved=[
                    {
                        "table": table,
                        "id": str(row_id),
                        "column": "product_id",
                        "from": str(plan.loser_id),
                        "to": str(plan.winner_id),
                    }
                    for table, ids in (
                        ("purchase_lines", plan.purchase_lines),
                        ("sales_lines", plan.sales_lines),
                        ("inventory_movements", plan.movements),
                    )
                    for row_id in ids
                ],
                hidden=[{"table": "products", "id": str(plan.loser_id)}],
            )

            for model in (PurchaseLine, SalesLine, InventoryMovement):
                await self._session.execute(
                    update(model)
                    .where(model.product_id == plan.loser_id, model.org_id == org_id)
                    .values(product_id=plan.winner_id)
                )

            losing = await self._session.get(Product, plan.loser_id)
            if losing is not None:
                losing.deleted_at = datetime.datetime.now(datetime.UTC)
            await self._session.flush()

            # The costing step, and the reason this is not two UPDATEs.
            # The winner's average is now a function of a history it did
            # not have a moment ago; the loser's inventory row must fall
            # to zero because it has no movements left to hold it up.
            replay = CostReplayService(self._session)
            for result in await replay.replay_product(org_id, plan.winner_id):
                report.note(
                    f"{plan.winner_label}: {result.qty_after} on hand at {result.avg_after}"
                )
            await replay.replay_product(org_id, plan.loser_id)
            report.note(f"{len(plan.movements)} movement(s) moved, {plan.loser_label} removed")

            await AuditService(self._session).record(
                org_id,
                actor.id,
                action="product.merged",
                entity_type="products",
                entity_id=plan.winner_id,
                before_state={"merged": plan.loser_label},
                after_state={
                    "into": plan.winner_label,
                    "movements": len(plan.movements),
                    "manifest": str(manifest.id),
                },
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

    # --- deleting -----------------------------------------------------

    async def describe(
        self,
        org_id: uuid.UUID,
        actor: User,
        *,
        code: str,
        brand: str | None,
        description: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Rename a product in the catalogue.

        A product's description is written once, by whichever sheet first
        created it, and no later purchase updates it -- so a row can end
        up wearing the wording of a bill that has since been moved to a
        different product (a brand correction copies the description onto
        the new row and leaves the old one behind). Nothing about the
        money changes here; the purchase lines keep each sheet's own
        wording, which is the trail back to the invoice.

        No reversal manifest: the inverse of this is running it again
        with the old text, which the audit row carries.
        """
        wanted = " ".join(description.split())
        if not wanted:
            raise ValidationError("a description cannot be blank")

        product = await self.resolve(org_id, code, brand)
        label = label_of(product.code, await self._brand_name(product))
        was = product.description
        if was == wanted:
            raise ValidationError(f"{label} is already described as '{wanted}'")

        async with guarded(self._session, org_id, dry_run=dry_run) as report:
            product.description = wanted
            await self._session.flush()
            report.note(f"{label}: '{was}' → '{wanted}'")

            await AuditService(self._session).record(
                org_id,
                actor.id,
                action="product.described",
                entity_type="products",
                entity_id=product.id,
                before_state={"description": was},
                after_state={"description": wanted},
                channel="cli",
            )

        return {
            "label": label,
            "was": was,
            "now": wanted,
            "committed": report.committed,
            "notes": report.notes,
        }

    async def delete(
        self, org_id: uuid.UUID, actor: User, *, code: str, brand: str | None, dry_run: bool = False
    ) -> dict[str, Any]:
        """Remove a product nothing has ever happened to.

        Narrow on purpose. A product with history is not a mistake in the
        catalogue, it is part of the record — hiding it would take its
        purchases out of the reports that explain the cost of everything
        else. That case is a merge, and the error says so.
        """
        product = await self.resolve(org_id, code, brand)
        label = label_of(product.code, await self._brand_name(product))
        purchases, sales, movements = await self._usage(product.id)

        blockers = []
        if purchases:
            blockers.append(f"it is on {len(purchases)} purchase line(s)")
        if sales:
            blockers.append(f"it is on {len(sales)} sale line(s)")
        if movements:
            blockers.append(f"it has {len(movements)} stock movement(s)")
        if blockers:
            raise ValidationError(
                f"{label} cannot be deleted: "
                + "; ".join(blockers)
                + ". If it is a duplicate, merge it into the real one instead."
            )

        async with guarded(self._session, org_id, dry_run=dry_run) as report:
            manifest = await ReversalService(self._session).record(
                org_id,
                actor,
                operation="delete_product",
                subject=label,
                hidden=[{"table": "products", "id": str(product.id)}],
            )
            product.deleted_at = datetime.datetime.now(datetime.UTC)
            # An inventory row for a product with no movements is a zero
            # that only exists because something created it. It goes with
            # the product; there is no history in it to lose.
            for row in (
                (
                    await self._session.execute(
                        select(Inventory).where(
                            Inventory.org_id == org_id, Inventory.product_id == product.id
                        )
                    )
                )
                .scalars()
                .all()
            ):
                await self._session.delete(row)
            await self._session.flush()
            report.note(f"{label} removed from the catalogue")

            await AuditService(self._session).record(
                org_id,
                actor.id,
                action="product.deleted",
                entity_type="products",
                entity_id=product.id,
                before_state={"code": product.code, "label": label},
                channel="cli",
            )
            reversal = str(manifest.id)[:8]

        return {
            "label": label,
            "committed": report.committed,
            "notes": report.notes,
            "reversal": reversal,
        }


async def replay_after_reversal(
    session: AsyncSession, org_id: uuid.UUID, manifest: ReversalManifest
) -> list[str]:
    """Put the costs right after a product merge has been undone.

    `ReversalService.apply` moves rows back; it does not know that some
    of those rows are inventory movements and that a weighted average is
    a running function of them. Without this the movements would sit
    under the original product while `inventory` still described the
    merged history, and the guard around the reversal would — correctly —
    roll the whole thing back.

    Shared by the terminal and the browser so the two cannot disagree
    about what undoing a product merge means.
    """
    if manifest.operation not in {"merge_product", "delete_product"}:
        return []
    touched = {
        uuid.UUID(str(entry[side]))
        for entry in manifest.payload.get("moved", [])
        if entry.get("column") == "product_id"
        for side in ("from", "to")
        if entry.get(side)
    }
    replay = CostReplayService(session)
    notes = []
    for product_id in sorted(touched, key=str):
        product = await session.get(Product, product_id)
        code = product.code if product is not None else str(product_id)[:8]
        for result in await replay.replay_product(org_id, product_id):
            notes.append(f"{code}: {result.qty_after} at {result.avg_after}")
    return notes
