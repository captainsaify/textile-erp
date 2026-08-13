"""`erp stock …` -- adjust quantity, or rebuild cost from history."""

from __future__ import annotations

import decimal
import uuid
from typing import Annotated

import typer
from sqlalchemy import select

from backend.admin import console, resolve
from backend.admin.app import cli, run
from backend.admin.harness import AdminContext, AdminError, guarded
from backend.models import InventoryMovement, Product
from backend.models.enums import MovementType
from backend.services.audit_service import AuditService
from backend.services.cost_replay_service import CostReplayService

stock = typer.Typer(no_args_is_help=True, help="Correct quantities and costs.")
cli.add_typer(stock, name="stock")

_REASONS = {
    "damaged": MovementType.DAMAGE,
    "adjust-up": MovementType.ADJUSTMENT_INCREASE,
    "adjust-down": MovementType.ADJUSTMENT_DECREASE,
}


@stock.command("recost")
def recost(
    code: Annotated[str | None, typer.Argument(help="Product code; omit with --all")] = None,
    brand: Annotated[
        str | None, typer.Option("--brand", help="Which brand carries the code")
    ] = None,
    every: Annotated[bool, typer.Option("--all", help="Every product in the books")] = False,
) -> None:
    """Rebuild the weighted average cost by replaying every movement.

    The repair for "the average cost looks wrong". It honours rate
    corrections, which are recorded as zero-quantity movements whose
    unit cost carries the new average -- skipping those is what once
    overstated this business's stock by about 1.3 lakh."""

    async def action(ctx: AdminContext) -> None:
        if every == (code is not None):
            raise AdminError("give a code, or --all, but not both.")

        replay = CostReplayService(ctx.session)
        async with guarded(ctx, what="recost"):
            if every:
                results = await replay.replay_all(ctx.org_id)
            else:
                assert code is not None
                product = await resolve.product_by_code(ctx.session, ctx.org_id, code, brand)
                results = await replay.replay_product(ctx.org_id, product.id)

            changed = [r for r in results if r.changed]
            console.item(f"{len(results)} product/warehouse pair(s) replayed")
            if not changed:
                console.item("nothing moved -- the books already agreed with history")
            for result in changed:
                moved = await ctx.session.get(Product, result.product_id)
                label = moved.code if moved is not None else str(result.product_id)[:8]
                console.item(
                    f"{label}: qty {console.qty(result.qty_before)} → "
                    f"{console.qty(result.qty_after)}, "
                    f"avg {console.money(result.avg_before)} → {console.money(result.avg_after)}"
                )

    run(action)


@stock.command("adjust")
def adjust(
    code: Annotated[str, typer.Argument(help="Product code")],
    quantity: Annotated[str, typer.Argument(help="Signed change, e.g. -5 or 12")],
    reason: Annotated[str, typer.Option("--reason", help="damaged | adjust-up | adjust-down")],
    brand: Annotated[
        str | None, typer.Option("--brand", help="Which brand carries the code")
    ] = None,
    note: Annotated[str | None, typer.Option("--note", help="Why")] = None,
) -> None:
    """Move stock without a purchase or a sale behind it.

    Always a typed movement, never an edit of the balance: the balance
    is derived from movements, and writing it directly would make the
    two disagree at the next reconciliation."""

    async def action(ctx: AdminContext) -> None:
        if reason not in _REASONS:
            raise AdminError(f"--reason must be one of: {', '.join(sorted(_REASONS))}")
        delta = decimal.Decimal(quantity)
        if delta == 0:
            raise AdminError("an adjustment of zero would only add noise to the ledger.")

        product = await resolve.product_by_code(ctx.session, ctx.org_id, code, brand)
        console.head(f"{product.code}")
        async with guarded(ctx, what=f"stock adjustment on {product.code}"):
            warehouse_id = (
                await ctx.session.execute(
                    select(InventoryMovement.warehouse_id)
                    .where(
                        InventoryMovement.org_id == ctx.org_id,
                        InventoryMovement.product_id == product.id,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if warehouse_id is None:
                raise AdminError(
                    f"{product.code} has never moved, so there is no warehouse to adjust in. "
                    "Record a purchase first."
                )

            replay = CostReplayService(ctx.session)
            # unit_cost on an adjustment is the *current* average: stock
            # leaving or appearing without a transaction behind it does
            # not change what the remaining stock cost.
            current = await replay.replay(ctx.org_id, product.id, warehouse_id)
            ctx.session.add(
                InventoryMovement(
                    org_id=ctx.org_id,
                    product_id=product.id,
                    warehouse_id=warehouse_id,
                    movement_type=_REASONS[reason],
                    qty_delta=delta,
                    unit_cost=current.avg_after,
                    resulting_qty_on_hand=current.qty_after + delta,
                    resulting_avg_cost=current.avg_after,
                    source_type="admin_adjustment",
                    source_id=uuid.uuid4(),
                    created_by=ctx.actor.id,
                    notes=note,
                )
            )
            await ctx.session.flush()
            result = await replay.replay(ctx.org_id, product.id, warehouse_id)
            console.item(
                f"{reason}: {console.qty(delta)} → on hand {console.qty(result.qty_after)}, "
                f"avg {console.money(result.avg_after)}"
            )
            if result.qty_after < 0:
                raise AdminError(
                    f"that would leave {product.code} at {console.qty(result.qty_after)}. "
                    "Stock cannot go negative -- fix the sales that took it below zero first."
                )

            await AuditService(ctx.session).record(
                ctx.org_id,
                ctx.actor.id,
                action="stock.adjusted",
                entity_type="inventory",
                entity_id=product.id,
                after_state={
                    "code": product.code,
                    "qty_delta": str(delta),
                    "reason": reason,
                    "note": note,
                },
                channel="cli",
            )

    run(action)
