"""`erp charge`, `purge`, `restore-purged`, `merge`.

The destructive end of the CLI. Everything here runs inside the same
reconcile-or-roll-back harness as the rest, and `purge` additionally
makes you type the invoice number back rather than answering y.
"""

from __future__ import annotations

import datetime
import decimal
from typing import Annotated

import typer
from sqlalchemy import func, select, update

from backend.admin import console, resolve
from backend.admin.app import cli, confirm, run
from backend.admin.harness import AdminContext, AdminError, guarded
from backend.models import (
    Customer,
    Product,
    PurchaseHeader,
    SalesHeader,
    Supplier,
)
from backend.models.enums import PurchaseStatus
from backend.services.audit_service import AuditService
from backend.services.receipt_correction_service import ChargeService
from backend.services.undo_service import UndoService

merge = typer.Typer(no_args_is_help=True, help="Combine two things that are one thing.")
cli.add_typer(merge, name="merge")


# --- charge -----------------------------------------------------------


@cli.command("charge")
def charge(
    kind: Annotated[str, typer.Argument(help="purchase | sale")],
    reference: Annotated[str, typer.Argument(help="Invoice number, or sale reference")],
    label: Annotated[str, typer.Argument(help="GST, packing, freight …")],
    amount: Annotated[str, typer.Argument(help="Amount")],
    note: Annotated[str | None, typer.Option("--note", help="Why, or who shared it")] = None,
) -> None:
    """Put a charge on a bill or sale that is already confirmed.

    On a purchase it becomes part of what the goods cost and is spread
    across the lines by value. On a sale it credits other income, not
    revenue, so gross margin stays about the goods."""

    async def action(ctx: AdminContext) -> None:
        if kind not in {"purchase", "sale"}:
            raise AdminError("first argument must be `purchase` or `sale`.")
        console.head(f"{kind} {reference}")
        async with guarded(ctx, what=f"{label.upper()} on {reference}"):
            added = await ChargeService(ctx.session).add_in_transaction(
                ctx.actor,
                reference=reference,
                label=label,
                amount=decimal.Decimal(amount),
                note=note,
            )
            console.item(f"{label.upper()} {console.money(decimal.Decimal(amount))} added")
            for attr, text in (
                ("sold_codes", "already sold, cost not restated for"),
                ("restated_codes", "cost restated for"),
            ):
                codes = getattr(added, attr, None)
                if codes:
                    console.item(f"{text}: {', '.join(codes)}")

    run(action)


# --- purge / restore --------------------------------------------------


@cli.command("purge")
def purge(
    kind: Annotated[str, typer.Argument(help="purchase | sale")],
    reference: Annotated[str, typer.Argument(help="Invoice number, or sale reference")],
) -> None:
    """Take a record out of the books entirely.

    It leaves every report, total, ledger, search and reconciliation --
    as far as the books are concerned it never happened. The rows are
    kept hidden, so a purge aimed at the wrong invoice is one mistake
    instead of two: `erp restore-purged` brings the record back.

    A bill still carrying stock is reversed first, as a separate audited
    step. Restoring does *not* put that stock back; the message says so
    and names what to re-enter."""

    async def action(ctx: AdminContext) -> None:
        if kind not in {"purchase", "sale"}:
            raise AdminError("first argument must be `purchase` or `sale`.")

        if kind == "purchase":
            header: PurchaseHeader | SalesHeader = await resolve.purchase_by_invoice(
                ctx.session, ctx.org_id, reference
            )
            label = header.invoice_no  # type: ignore[union-attr]
            live = header.status is PurchaseStatus.CONFIRMED
            paid = header.amount_paid
        else:
            header = await resolve.sale_by_reference(ctx.session, ctx.org_id, reference)
            label = str(header.id)[:8]
            live = header.status == "confirmed"
            paid = header.amount_paid

        console.head(f"Purge {kind} {label}")
        console.item(f"grand total {console.money(header.grand_total)}")
        if paid > 0:
            raise AdminError(
                f"{console.money(paid)} has already been paid against {label}. "
                "Reverse that payment first -- purging would leave the money "
                "recorded against nothing."
            )
        console.item(
            "still carrying stock -- it will be reversed first" if live else "already reversed"
        )
        console.item("reversible with: erp restore-purged " + f"{kind} {label}")

        confirm(
            ctx,
            expected=label,
            prompt=f"Type {label} to confirm: ",
        )

        async with guarded(ctx, what=f"purge of {kind} {label}"):
            if live:
                await UndoService(ctx.session).undo_in_transaction(
                    ctx.actor, entity=kind, reference=reference
                )
                console.item("reversed: stock and journal entries unwound")

            now = datetime.datetime.now(datetime.UTC)
            header.deleted_at = now
            header.purged_at = now
            await ctx.session.flush()
            console.item("hidden from every report, total and reconciliation")

            await AuditService(ctx.session).record(
                ctx.org_id,
                ctx.actor.id,
                action=f"{kind}.purged",
                entity_type="purchase_headers" if kind == "purchase" else "sales_headers",
                entity_id=header.id,
                after_state={"reference": label, "stock_reversed": live},
                channel="cli",
            )

    run(action)


@cli.command("restore-purged")
def restore_purged(
    kind: Annotated[str, typer.Argument(help="purchase | sale")],
    reference: Annotated[str, typer.Argument(help="Invoice number, or sale reference")],
) -> None:
    """Bring back a record that was purged.

    Only records that were *purged* -- a bill soft-deleted because it
    was cancelled is a different state and is left alone."""

    async def action(ctx: AdminContext) -> None:
        if kind not in {"purchase", "sale"}:
            raise AdminError("first argument must be `purchase` or `sale`.")
        if kind == "purchase":
            header: PurchaseHeader | SalesHeader = await resolve.purchase_by_invoice(
                ctx.session, ctx.org_id, reference, include_purged=True
            )
            label = header.invoice_no  # type: ignore[union-attr]
        else:
            header = await resolve.sale_by_reference(
                ctx.session, ctx.org_id, reference, include_purged=True
            )
            label = str(header.id)[:8]

        console.head(f"Restore {kind} {label}")
        async with guarded(ctx, what=f"restore of {kind} {label}"):
            header.deleted_at = None
            header.purged_at = None
            await ctx.session.flush()
            console.item("visible again in reports and totals")
            await AuditService(ctx.session).record(
                ctx.org_id,
                ctx.actor.id,
                action=f"{kind}.restored",
                entity_type="purchase_headers" if kind == "purchase" else "sales_headers",
                entity_id=header.id,
                after_state={"reference": label},
                channel="cli",
            )

        console.warn(
            "the record is back, but its stock is NOT. Purging reversed the "
            "movements, and restoring does not replay them."
        )
        console.item(f"to put the goods back: erp show {kind} {label}, then re-enter the lines")

    run(action)


# --- merge ------------------------------------------------------------


async def _merge_party(
    ctx: AdminContext,
    *,
    loser_name: str,
    winner_name: str,
    model: type[Supplier] | type[Customer],
    header_model: type[PurchaseHeader] | type[SalesHeader],
    fk: str,
    label: str,
) -> None:
    if loser_name.strip().casefold() == winner_name.strip().casefold():
        raise AdminError("those are the same name.")
    loser: Supplier | Customer
    winner: Supplier | Customer
    if model is Supplier:
        loser = await resolve.supplier_by_name(ctx.session, ctx.org_id, loser_name)
        winner = await resolve.supplier_by_name(ctx.session, ctx.org_id, winner_name)
    else:
        loser = await resolve.customer_by_name(ctx.session, ctx.org_id, loser_name)
        winner = await resolve.customer_by_name(ctx.session, ctx.org_id, winner_name)

    console.head(f"Merge {label} {loser.name} → {winner.name}")
    moved = (
        await ctx.session.execute(
            select(func.count())
            .select_from(header_model)
            .where(
                getattr(header_model, fk) == loser.id,
                header_model.deleted_at.is_(None),
            )
        )
    ).scalar_one()
    console.item(f"{moved} transaction(s) to move")
    confirm(ctx, expected=winner.name, prompt=f"Type the surviving name ({winner.name}): ")

    async with guarded(ctx, what=f"merge of {loser.name} into {winner.name}"):
        await ctx.session.execute(
            update(header_model)
            .where(getattr(header_model, fk) == loser.id)
            .values(**{fk: winner.id})
        )
        loser.deleted_at = datetime.datetime.now(datetime.UTC)
        await ctx.session.flush()
        console.item(f"{moved} transaction(s) moved; {loser.name} removed")
        await AuditService(ctx.session).record(
            ctx.org_id,
            ctx.actor.id,
            action=f"{label}.merged",
            entity_type=f"{label}s",
            entity_id=winner.id,
            before_state={"merged": loser.name},
            after_state={"into": winner.name, "moved": moved},
            channel="cli",
        )


@merge.command("supplier")
def merge_supplier(
    loser: Annotated[str, typer.Argument(help="The name that stops existing")],
    into: Annotated[str, typer.Argument(metavar="into NAME", help="Literal word `into`")],
    winner: Annotated[str, typer.Argument(help="The name that survives")],
) -> None:
    """`erp merge supplier "Yakub Asif" into "Asif Panipat"`"""

    async def action(ctx: AdminContext) -> None:
        if into.lower() != "into":
            raise AdminError('usage: erp merge supplier "Old" into "New"')
        await _merge_party(
            ctx,
            loser_name=loser,
            winner_name=winner,
            model=Supplier,
            header_model=PurchaseHeader,
            fk="supplier_id",
            label="supplier",
        )

    run(action)


@merge.command("customer")
def merge_customer(
    loser: Annotated[str, typer.Argument(help="The name that stops existing")],
    into: Annotated[str, typer.Argument(metavar="into NAME", help="Literal word `into`")],
    winner: Annotated[str, typer.Argument(help="The name that survives")],
) -> None:
    """`erp merge customer "Shahid Bhai" into "Zahid Bhai"`"""

    async def action(ctx: AdminContext) -> None:
        if into.lower() != "into":
            raise AdminError('usage: erp merge customer "Old" into "New"')
        await _merge_party(
            ctx,
            loser_name=loser,
            winner_name=winner,
            model=Customer,
            header_model=SalesHeader,
            fk="customer_id",
            label="customer",
        )

    run(action)


@merge.command("brand")
def merge_brand(
    loser: Annotated[str, typer.Argument(help="The brand that stops existing")],
    into: Annotated[str, typer.Argument(metavar="into NAME", help="Literal word `into`")],
    winner: Annotated[str, typer.Argument(help="The brand that survives")],
) -> None:
    """Fold one brand's products into another.

    This is the repair for two brands whose names differ only by case or
    whitespace -- `TOP` and `TOP ` were two rows on the live books, and a
    lookup that compared one but not the other picked between them
    silently."""

    async def action(ctx: AdminContext) -> None:
        if into.lower() != "into":
            raise AdminError('usage: erp merge brand "Old" into "New"')
        losing = await resolve.brand_by_name(ctx.session, ctx.org_id, loser)
        winning = await resolve.brand_by_name(ctx.session, ctx.org_id, winner)
        if losing.id == winning.id:
            raise AdminError("those resolve to the same brand.")

        products = list(
            (
                await ctx.session.execute(
                    select(Product).where(
                        Product.org_id == ctx.org_id,
                        Product.brand_id == losing.id,
                        Product.deleted_at.is_(None),
                    )
                )
            ).scalars()
        )
        console.head(f"Merge brand {losing.name} → {winning.name}")
        clashes = []
        for product in products:
            existing = (
                await ctx.session.execute(
                    select(Product).where(
                        Product.org_id == ctx.org_id,
                        Product.brand_id == winning.id,
                        Product.code == product.code,
                        Product.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                clashes.append(product.code)
        if clashes:
            raise AdminError(
                f"{winning.name} already carries {', '.join(sorted(clashes))}. "
                "Merging would put two products on one code under one brand. "
                "Re-point those lines first: erp fix purchase <inv> --line N --code <new>"
            )

        console.item(f"{len(products)} product(s) to move")
        confirm(ctx, expected=winning.name, prompt=f"Type the surviving brand ({winning.name}): ")

        async with guarded(ctx, what=f"merge of brand {losing.name} into {winning.name}"):
            await ctx.session.execute(
                update(Product).where(Product.brand_id == losing.id).values(brand_id=winning.id)
            )
            await ctx.session.execute(
                update(PurchaseHeader)
                .where(PurchaseHeader.brand_id == losing.id)
                .values(brand_id=winning.id)
            )
            losing.deleted_at = datetime.datetime.now(datetime.UTC)
            await ctx.session.flush()
            console.item(f"{len(products)} product(s) moved; {losing.name} removed")
            await AuditService(ctx.session).record(
                ctx.org_id,
                ctx.actor.id,
                action="brand.merged",
                entity_type="brands",
                entity_id=winning.id,
                before_state={"merged": losing.name},
                after_state={"into": winning.name, "products": len(products)},
                channel="cli",
            )

    run(action)


@merge.command("purchase")
def merge_purchase(
    loser: Annotated[str, typer.Argument(help="The invoice that stops existing")],
    into: Annotated[str, typer.Argument(metavar="into NAME", help="Literal word `into`")],
    winner: Annotated[str, typer.Argument(help="The invoice that survives")],
) -> None:
    """Fold one bill's lines into another.

    The repair for a bill entered twice because a draft could only hold
    one brand -- 007 and 007B were one delivery. Lines are renumbered
    onto the surviving bill and the charges add up; both bills must be
    from the same supplier, and neither may be part-paid."""

    async def action(ctx: AdminContext) -> None:
        if into.lower() != "into":
            raise AdminError("usage: erp merge purchase 007B into 007")
        losing = resolve.confirmed_only(
            await resolve.purchase_by_invoice(ctx.session, ctx.org_id, loser)
        )
        winning = resolve.confirmed_only(
            await resolve.purchase_by_invoice(ctx.session, ctx.org_id, winner)
        )
        if losing.id == winning.id:
            raise AdminError("those are the same bill.")
        if losing.supplier_id != winning.supplier_id:
            raise AdminError(
                "those bills are from different suppliers. Move one first: "
                "erp fix purchase <inv> --supplier <name>"
            )
        for header in (losing, winning):
            if header.amount_paid > 0:
                raise AdminError(
                    f"{header.invoice_no} is part-paid ({console.money(header.amount_paid)}). "
                    "Reverse the payment before merging."
                )

        from backend.models import PurchaseLine

        losing_lines = list(
            (
                await ctx.session.execute(
                    select(PurchaseLine)
                    .where(PurchaseLine.purchase_header_id == losing.id)
                    .order_by(PurchaseLine.line_no)
                )
            ).scalars()
        )
        highest = (
            await ctx.session.execute(
                select(func.coalesce(func.max(PurchaseLine.line_no), 0)).where(
                    PurchaseLine.purchase_header_id == winning.id
                )
            )
        ).scalar_one()

        console.head(f"Merge {losing.invoice_no} → {winning.invoice_no}")
        console.item(f"{len(losing_lines)} line(s) move, renumbered from {highest + 1}")
        console.item(
            f"totals {console.money(winning.grand_total)} + {console.money(losing.grand_total)}"
        )
        confirm(ctx, expected=winning.invoice_no, prompt=f"Type {winning.invoice_no} to confirm: ")

        async with guarded(ctx, what=f"merge of {losing.invoice_no} into {winning.invoice_no}"):
            for offset, line in enumerate(losing_lines, start=1):
                line.purchase_header_id = winning.id
                line.line_no = highest + offset
            winning.subtotal += losing.subtotal
            winning.freight += losing.freight
            winning.other_charges += losing.other_charges
            winning.grand_total += losing.grand_total
            now = datetime.datetime.now(datetime.UTC)
            losing.deleted_at = now
            losing.purged_at = now
            await ctx.session.flush()
            console.item(f"{len(losing_lines)} line(s) moved; {losing.invoice_no} removed")
            console.item(f"new total {console.money(winning.grand_total)}")
            await AuditService(ctx.session).record(
                ctx.org_id,
                ctx.actor.id,
                action="purchase.merged",
                entity_type="purchase_headers",
                entity_id=winning.id,
                before_state={"merged": losing.invoice_no},
                after_state={"into": winning.invoice_no, "lines": len(losing_lines)},
                channel="cli",
            )

    run(action)
