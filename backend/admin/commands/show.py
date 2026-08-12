"""`erp show …` -- read-only. Nothing here opens a transaction.

These exist to be run *before* a repair, so they print the identifiers
the repair commands need: line numbers, the brand each line is actually
under, and the eight-character sale reference.
"""

from __future__ import annotations

import decimal
from typing import Annotated

import typer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.admin import console, resolve
from backend.admin.app import cli, run
from backend.admin.harness import AdminContext
from backend.models import (
    Brand,
    Inventory,
    Product,
    PurchaseLine,
    SalesLine,
    Supplier,
)

show = typer.Typer(no_args_is_help=True, help="Look at a bill, sale, product or party.")
cli.add_typer(show, name="show")

_ZERO = decimal.Decimal("0")


async def _brand_of(session: AsyncSession, product: Product) -> str:
    if product.brand_id is None:
        return "—"
    name = (
        await session.execute(select(Brand.name).where(Brand.id == product.brand_id))
    ).scalar_one_or_none()
    return name or "—"


@show.command("purchase")
def show_purchase(invoice: Annotated[str, typer.Argument(help="Invoice number, e.g. 007")]) -> None:
    """Lines, charges, supplier and totals for one bill."""

    async def action(ctx: AdminContext) -> None:
        header = await resolve.purchase_by_invoice(ctx.session, ctx.org_id, invoice)
        supplier = (
            await ctx.session.execute(select(Supplier).where(Supplier.id == header.supplier_id))
        ).scalar_one()

        console.head(f"Purchase {header.invoice_no}")
        console.item(f"supplier   {supplier.name}")
        console.item(f"date       {header.invoice_date}")
        console.item(f"status     {header.status.value} / paid: {header.payment_status}")
        console.item(f"id         {str(header.id)[:8]}")

        lines = list(
            (
                await ctx.session.execute(
                    select(PurchaseLine)
                    .where(PurchaseLine.purchase_header_id == header.id)
                    .order_by(PurchaseLine.line_no)
                )
            ).scalars()
        )
        rows = []
        for line in lines:
            product = (
                await ctx.session.execute(select(Product).where(Product.id == line.product_id))
            ).scalar_one()
            rows.append(
                [
                    str(line.line_no),
                    product.code,
                    await _brand_of(ctx.session, product),
                    (line.description or product.description or "")[:28],
                    console.qty(line.qty),
                    console.money(line.rate),
                    console.money(line.line_total),
                ]
            )
        console.head("Lines")
        console.table(rows, headers=["#", "code", "brand", "description", "qty", "rate", "total"])

        console.head("Totals")
        console.item(f"subtotal      {console.money(header.subtotal)}")
        if header.freight:
            console.item(f"freight       {console.money(header.freight)}")
        if header.other_charges:
            console.item(f"other charges {console.money(header.other_charges)}")
        console.item(console.bold(f"grand total   {console.money(header.grand_total)}"))
        if header.amount_paid:
            console.item(f"paid          {console.money(header.amount_paid)}")
        if header.notes:
            console.head("Notes")
            console.item(header.notes)

    run(action)


@show.command("sale")
def show_sale(
    reference: Annotated[str, typer.Argument(help="First characters of the sale id")],
) -> None:
    """Lines, charges, customer and totals for one sale."""

    async def action(ctx: AdminContext) -> None:
        from backend.models import Customer

        header = await resolve.sale_by_reference(ctx.session, ctx.org_id, reference)
        customer = (
            await ctx.session.execute(select(Customer).where(Customer.id == header.customer_id))
        ).scalar_one()

        console.head(f"Sale {str(header.id)[:8]}")
        console.item(f"customer   {customer.name}")
        console.item(f"date       {header.sale_date}")
        console.item(f"payment    {header.payment_type.value} / {header.payment_status}")

        lines = list(
            (
                await ctx.session.execute(
                    select(SalesLine)
                    .where(SalesLine.sales_header_id == header.id)
                    .order_by(SalesLine.line_no)
                )
            ).scalars()
        )
        rows = []
        for line in lines:
            product = (
                await ctx.session.execute(select(Product).where(Product.id == line.product_id))
            ).scalar_one()
            rows.append(
                [
                    str(line.line_no),
                    product.code,
                    await _brand_of(ctx.session, product),
                    (product.description or "")[:24],
                    console.qty(line.qty),
                    console.money(line.rate),
                    console.money(line.avg_cost_at_sale_time),
                    console.money(line.line_total),
                ]
            )
        console.head("Lines")
        console.table(
            rows, headers=["#", "code", "brand", "description", "qty", "rate", "cost", "total"]
        )

        console.head("Totals")
        console.item(f"subtotal      {console.money(header.subtotal)}")
        if header.freight:
            console.item(f"freight       {console.money(header.freight)}")
        if header.other_charges:
            console.item(f"other charges {console.money(header.other_charges)}")
        console.item(console.bold(f"grand total   {console.money(header.grand_total)}"))
        console.item(f"paid          {console.money(header.amount_paid)}")

    run(action)


@show.command("stock")
def show_stock(code: Annotated[str, typer.Argument(help="Product code, e.g. 55X")]) -> None:
    """Every brand carrying this code, with quantity and average cost.

    Run this before any `--code` or `--brand` repair: a code is unique
    per brand, not globally, and this is what says which is which."""

    async def action(ctx: AdminContext) -> None:
        wanted = " ".join(code.split()).upper()
        products = list(
            (
                await ctx.session.execute(
                    select(Product).where(
                        Product.org_id == ctx.org_id,
                        func.upper(Product.code) == wanted,
                        Product.deleted_at.is_(None),
                    )
                )
            ).scalars()
        )
        if not products:
            console.warn(f"no product with code {wanted}")
            return
        console.head(f"{wanted} — {len(products)} product(s)")
        rows = []
        for product in products:
            inv = (
                (
                    await ctx.session.execute(
                        select(Inventory).where(Inventory.product_id == product.id)
                    )
                )
                .scalars()
                .all()
            )
            on_hand = sum((i.qty_on_hand for i in inv), start=_ZERO)
            avg = inv[0].weighted_avg_cost if inv else _ZERO
            rows.append(
                [
                    await _brand_of(ctx.session, product),
                    (product.description or "")[:32],
                    console.qty(on_hand),
                    console.money(avg),
                    console.money(on_hand * avg),
                    str(product.id)[:8],
                ]
            )
        console.table(rows, headers=["brand", "description", "on hand", "avg cost", "value", "id"])

    run(action)


@show.command("party")
def show_party(name: Annotated[str, typer.Argument(help="Supplier or customer name")]) -> None:
    """Anything matching this name, on either side of the books."""

    async def action(ctx: AdminContext) -> None:
        suppliers, customers = await resolve.search_parties(ctx.session, ctx.org_id, name)
        if not suppliers and not customers:
            console.warn(f"nothing matching {name!r}")
            return
        if suppliers:
            console.head("Suppliers")
            console.table(
                [[s.name, s.phone or "—", str(s.id)[:8]] for s in suppliers],
                headers=["name", "phone", "id"],
            )
        if customers:
            console.head("Customers")
            console.table(
                [[c.name, c.phone or "—", str(c.id)[:8]] for c in customers],
                headers=["name", "phone", "id"],
            )
        if len(suppliers) > 1 or len(customers) > 1:
            console.say()
            console.warn("more than one match — if these are the same person, merge them:")
            console.item(console.dim('erp merge customer "Old Name" into "Right Name"'))

    run(action)
