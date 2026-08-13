"""`erp fix …` -- change something that is already in the books.

The two repairs that could not be expressed at all before this file are
`--brand` and `--code` on a line. Brand lives on the *product*, not on
the purchase line, so "this bill's LALA was labelled MKD" had no way to
be said: editing the product's brand would have moved every other bill
that ever used it. What actually has to happen is that the line points
at a *different product* -- the one with the same code under the right
brand -- and that its stock movement goes with it.

Which is why every path here ends in a cost replay. Moving a movement
between products changes the weighted average on both sides, and the
only way to be sure both are right afterwards is to recompute them from
history rather than to adjust them in place.
"""

from __future__ import annotations

import datetime
import decimal
from typing import Annotated

import typer
from sqlalchemy import select

from backend.admin import console, resolve
from backend.admin.app import cli, run
from backend.admin.harness import AdminContext, AdminError, guarded
from backend.models import (
    InventoryMovement,
    Product,
    PurchaseLine,
    SalesLine,
)
from backend.services.audit_service import AuditService
from backend.services.cost_replay_service import CostReplayService
from backend.services.receipt_correction_service import RateChangeService

fix = typer.Typer(no_args_is_help=True, help="Correct something already recorded.")
cli.add_typer(fix, name="fix")


async def _product_for(
    ctx: AdminContext, *, old: Product, code: str | None, brand: str | None
) -> Product:
    """The product this line should point at instead.

    Created when the brand exists but does not yet carry the code --
    which is the normal case for a mislabelled bill, and refusing would
    make the repair impossible rather than safe. Everything except code
    and brand is inherited, so the new row is the same goods under the
    right label."""
    wanted_code = " ".join((code or old.code).split()).upper()
    if brand is None:
        target_brand_id = old.brand_id
        brand_label = "unchanged"
    else:
        target_brand = await resolve.brand_by_name(ctx.session, ctx.org_id, brand)
        target_brand_id = target_brand.id
        brand_label = target_brand.name

    existing = (
        await ctx.session.execute(
            select(Product).where(
                Product.org_id == ctx.org_id,
                Product.code == wanted_code,
                Product.brand_id == target_brand_id,
                Product.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    created = Product(
        org_id=ctx.org_id,
        product_type_id=old.product_type_id,
        code=wanted_code,
        description=old.description,
        unit_id=old.unit_id,
        brand_id=target_brand_id,
        reorder_level=old.reorder_level,
        created_by=ctx.actor.id,
    )
    ctx.session.add(created)
    await ctx.session.flush()
    console.item(f"created product {wanted_code} under {brand_label}")
    return created


async def _move_movements(
    ctx: AdminContext, *, source_type: str, source_id: object, to_product: Product
) -> int:
    movements = list(
        (
            await ctx.session.execute(
                select(InventoryMovement).where(
                    InventoryMovement.org_id == ctx.org_id,
                    InventoryMovement.source_type == source_type,
                    InventoryMovement.source_id == source_id,
                )
            )
        ).scalars()
    )
    for movement in movements:
        movement.product_id = to_product.id
    await ctx.session.flush()
    return len(movements)


async def _replay_both(ctx: AdminContext, *product_ids: object) -> None:
    replay = CostReplayService(ctx.session)
    for product_id in dict.fromkeys(product_ids):
        for result in await replay.replay_product(ctx.org_id, product_id):  # type: ignore[arg-type]
            if result.changed:
                console.item(
                    f"recost: qty {console.qty(result.qty_before)} → "
                    f"{console.qty(result.qty_after)}, "
                    f"avg {console.money(result.avg_before)} → {console.money(result.avg_after)}"
                )


@fix.command("purchase")
def fix_purchase(
    invoice: Annotated[str, typer.Argument(help="Invoice number")],
    line_no: Annotated[
        int | None, typer.Option("--line", help="Line number, from `erp show`")
    ] = None,
    code: Annotated[
        str | None, typer.Option("--code", help="New product code for that line")
    ] = None,
    brand: Annotated[str | None, typer.Option("--brand", help="New brand for that line")] = None,
    description: Annotated[str | None, typer.Option("--desc", help="New line description")] = None,
    rate: Annotated[str | None, typer.Option("--rate", help="Corrected rate")] = None,
    supplier: Annotated[
        str | None, typer.Option("--supplier", help="Move the bill to this supplier")
    ] = None,
    invoice_no: Annotated[
        str | None, typer.Option("--invoice-no", help="Renumber the bill")
    ] = None,
    date: Annotated[str | None, typer.Option("--date", help="Invoice date, YYYY-MM-DD")] = None,
) -> None:
    """Correct a confirmed purchase: its header, or one of its lines."""

    async def action(ctx: AdminContext) -> None:
        header = resolve.confirmed_only(
            await resolve.purchase_by_invoice(ctx.session, ctx.org_id, invoice)
        )
        before = {
            "invoice_no": header.invoice_no,
            "supplier_id": str(header.supplier_id),
            "invoice_date": str(header.invoice_date),
        }
        if not any([line_no, code, brand, description, rate, supplier, invoice_no, date]):
            raise AdminError("nothing to change -- pass at least one option. See ADMIN.md.")
        if (code or brand or description) and line_no is None:
            raise AdminError(
                "--code, --brand and --desc need --line N. `erp show purchase` lists them."
            )

        console.head(f"Purchase {header.invoice_no}")
        async with guarded(ctx, what=f"purchase {header.invoice_no}"):
            if supplier is not None:
                party = await resolve.supplier_by_name(ctx.session, ctx.org_id, supplier)
                console.item(f"supplier → {party.name}")
                header.supplier_id = party.id
            if invoice_no is not None:
                console.item(f"invoice no → {invoice_no}")
                header.invoice_no = invoice_no.strip()
            if date is not None:
                header.invoice_date = datetime.date.fromisoformat(date)
                console.item(f"date → {header.invoice_date}")

            if line_no is not None and (code or brand or description):
                line = (
                    await ctx.session.execute(
                        select(PurchaseLine).where(
                            PurchaseLine.purchase_header_id == header.id,
                            PurchaseLine.line_no == line_no,
                        )
                    )
                ).scalar_one_or_none()
                if line is None:
                    raise AdminError(f"bill {header.invoice_no} has no line {line_no}.")
                old_product = await ctx.session.get(Product, line.product_id)
                if old_product is None:
                    raise AdminError("that line points at a product that no longer exists.")

                if description is not None:
                    line.description = description
                    console.item(f"line {line_no} description → {description}")

                if code or brand:
                    new_product = await _product_for(ctx, old=old_product, code=code, brand=brand)
                    if new_product.id != old_product.id:
                        moved = await _move_movements(
                            ctx,
                            source_type="purchase_line",
                            source_id=line.id,
                            to_product=new_product,
                        )
                        line.product_id = new_product.id
                        console.item(
                            f"line {line_no}: {old_product.code} → {new_product.code}, "
                            f"{moved} movement(s) moved"
                        )
                        await _replay_both(ctx, old_product.id, new_product.id)

            if rate is not None:
                changed = await RateChangeService(ctx.session).change(
                    ctx.actor,
                    invoice_no=header.invoice_no,
                    new_rate=decimal.Decimal(rate),
                    codes=None,
                )
                console.item(f"rate → {console.money(decimal.Decimal(rate))}")
                if getattr(changed, "sold_codes", None):
                    console.warn(
                        "already sold, cost not restated for: " + ", ".join(changed.sold_codes)  # type: ignore[attr-defined]
                    )

            await AuditService(ctx.session).record(
                ctx.org_id,
                ctx.actor.id,
                action="purchase.fixed",
                entity_type="purchase_headers",
                entity_id=header.id,
                before_state=before,
                after_state={
                    "invoice_no": header.invoice_no,
                    "supplier_id": str(header.supplier_id),
                    "invoice_date": str(header.invoice_date),
                    "line": line_no,
                    "code": code,
                    "brand": brand,
                },
                channel="cli",
            )

    run(action)


@fix.command("sale")
def fix_sale(
    reference: Annotated[str, typer.Argument(help="First characters of the sale id")],
    line_no: Annotated[
        int | None, typer.Option("--line", help="Line number, from `erp show`")
    ] = None,
    code: Annotated[
        str | None, typer.Option("--code", help="New product code for that line")
    ] = None,
    brand: Annotated[str | None, typer.Option("--brand", help="New brand for that line")] = None,
    customer: Annotated[
        str | None, typer.Option("--customer", help="Move the sale to this customer")
    ] = None,
    date: Annotated[str | None, typer.Option("--date", help="Sale date, YYYY-MM-DD")] = None,
) -> None:
    """Correct a sale: which customer it belongs to, or what a line sold."""

    async def action(ctx: AdminContext) -> None:
        header = await resolve.sale_by_reference(ctx.session, ctx.org_id, reference)
        if not any([line_no, code, brand, customer, date]):
            raise AdminError("nothing to change -- pass at least one option. See ADMIN.md.")
        if (code or brand) and line_no is None:
            raise AdminError("--code and --brand need --line N. `erp show sale` lists them.")

        before = {"customer_id": str(header.customer_id), "sale_date": str(header.sale_date)}
        console.head(f"Sale {str(header.id)[:8]}")
        async with guarded(ctx, what=f"sale {str(header.id)[:8]}"):
            if customer is not None:
                party = await resolve.customer_by_name(ctx.session, ctx.org_id, customer)
                console.item(f"customer → {party.name}")
                header.customer_id = party.id
            if date is not None:
                header.sale_date = datetime.date.fromisoformat(date)
                console.item(f"date → {header.sale_date}")

            if line_no is not None and (code or brand):
                line = (
                    await ctx.session.execute(
                        select(SalesLine).where(
                            SalesLine.sales_header_id == header.id,
                            SalesLine.line_no == line_no,
                        )
                    )
                ).scalar_one_or_none()
                if line is None:
                    raise AdminError(f"that sale has no line {line_no}.")
                old_product = await ctx.session.get(Product, line.product_id)
                if old_product is None:
                    raise AdminError("that line points at a product that no longer exists.")
                new_product = await _product_for(ctx, old=old_product, code=code, brand=brand)
                if new_product.id != old_product.id:
                    moved = await _move_movements(
                        ctx, source_type="sales_line", source_id=line.id, to_product=new_product
                    )
                    line.product_id = new_product.id
                    console.item(
                        f"line {line_no}: {old_product.code} → {new_product.code}, "
                        f"{moved} movement(s) moved"
                    )
                    await _replay_both(ctx, old_product.id, new_product.id)

            await AuditService(ctx.session).record(
                ctx.org_id,
                ctx.actor.id,
                action="sale.fixed",
                entity_type="sales_headers",
                entity_id=header.id,
                before_state=before,
                after_state={
                    "customer_id": str(header.customer_id),
                    "sale_date": str(header.sale_date),
                    "line": line_no,
                    "code": code,
                    "brand": brand,
                },
                channel="cli",
            )

    run(action)
