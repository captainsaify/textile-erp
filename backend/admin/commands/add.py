"""`erp add purchase` / `erp add sale` -- enter a transaction directly.

The reason this exists alongside WhatsApp: reconstruction. After a bill
is purged, or when one has to be re-entered as it should have been
recorded rather than as it was typed, you need backdating, per-line
brands and itemised charges in one command, without a conversation.

Line syntax is `CODE:QTY:RATE[:BRAND[:DESCRIPTION]]`. Colons rather than
spaces because descriptions have spaces in them and quoting rules are
the sort of thing that goes wrong at 3 a.m.; brand and description are
optional because most lines do not need them.
"""

from __future__ import annotations

import datetime
import decimal
from typing import Annotated

import typer

from backend.admin import console, resolve
from backend.admin.app import cli, run
from backend.admin.harness import AdminContext, AdminError, guarded
from backend.models.enums import SalePaymentType
from backend.services.purchase_service import Draft, DraftLine, PurchaseService
from backend.services.sales_service import SaleDraft, SaleDraftLine, SalesService

add = typer.Typer(no_args_is_help=True, help="Record a purchase or sale directly.")
cli.add_typer(add, name="add")

ZERO = decimal.Decimal("0")


def _parse_charges(raw: list[str]) -> dict[str, decimal.Decimal]:
    """`--charge "GST:1200"`, repeatable."""
    charges: dict[str, decimal.Decimal] = {}
    for entry in raw:
        label, _, amount = entry.partition(":")
        if not amount:
            raise AdminError(f"--charge {entry!r} should look like GST:1200")
        key = " ".join(label.split()).upper()
        if not key:
            raise AdminError(f"--charge {entry!r} has no name.")
        charges[key] = charges.get(key, ZERO) + decimal.Decimal(amount)
    return charges


def _split_line(
    entry: str, *, want_brand: bool
) -> tuple[str, decimal.Decimal, decimal.Decimal, str | None, str | None]:
    parts = entry.split(":")
    if len(parts) < 3:
        raise AdminError(
            f"--line {entry!r} should look like CODE:QTY:RATE"
            + (":BRAND:DESCRIPTION" if want_brand else "")
        )
    code = " ".join(parts[0].split()).upper()
    try:
        qty = decimal.Decimal(parts[1])
        rate = decimal.Decimal(parts[2])
    except decimal.InvalidOperation as exc:
        raise AdminError(f"--line {entry!r}: quantity and rate must be numbers.") from exc
    if qty <= ZERO:
        raise AdminError(f"--line {entry!r}: quantity must be more than zero.")
    brand = parts[3].strip() or None if len(parts) > 3 else None
    description = ":".join(parts[4:]).strip() or None if len(parts) > 4 else None
    return code, qty, rate, brand, description


@add.command("purchase")
def add_purchase(
    supplier: Annotated[str, typer.Option("--supplier", help="Supplier name, exactly")],
    invoice: Annotated[str, typer.Option("--invoice", help="Invoice number")],
    date: Annotated[str, typer.Option("--date", help="Invoice date, YYYY-MM-DD")],
    line: Annotated[list[str], typer.Option("--line", help="CODE:QTY:RATE[:BRAND[:DESC]]")],
    charge: Annotated[list[str] | None, typer.Option("--charge", help="LABEL:AMOUNT")] = None,
    freight: Annotated[str, typer.Option("--freight", help="Freight for the bill")] = "0",
    brand: Annotated[
        str | None, typer.Option("--brand", help="Default brand for every line")
    ] = None,
) -> None:
    """Record a purchase exactly as given, with no conversation.

    Every line's product must already exist under the brand named. That
    is deliberate: creating products silently here is how a typo becomes
    a second product carrying half the stock."""

    async def action(ctx: AdminContext) -> None:
        supplier_row = await resolve.supplier_by_name(ctx.session, ctx.org_id, supplier)
        invoice_date = datetime.date.fromisoformat(date)
        charges = _parse_charges(charge or [])

        service = PurchaseService(ctx.session)
        default_brand_id = None
        if brand is not None:
            default_brand_id = (await resolve.brand_by_name(ctx.session, ctx.org_id, brand)).id

        lines: list[DraftLine] = []
        for entry in line:
            code, qty, rate, line_brand, description = _split_line(entry, want_brand=True)
            product = await resolve.product_by_code(
                ctx.session, ctx.org_id, code, line_brand or brand
            )
            lines.append(
                DraftLine(
                    code=product.code,
                    qty=qty,
                    rate=rate,
                    product_id=product.id,
                    resolved_code=product.code,
                    unit_code=None,
                    description=description,
                    brand_id=product.brand_id or default_brand_id,
                )
            )

        draft = Draft(
            supplier_id=supplier_row.id,
            supplier_name=supplier_row.name,
            invoice_no=invoice.strip(),
            invoice_date=invoice_date,
            brand_id=default_brand_id,
            brand_name=brand,
            lines=lines,
            freight=decimal.Decimal(freight),
            other_charges=sum(charges.values(), ZERO),
            declared_total=None,
            charges=charges,
        )

        console.head(f"New purchase {draft.invoice_no}")
        console.item(f"supplier {supplier_row.name}, {invoice_date}")
        console.table(
            [
                [
                    str(i),
                    ln.code,
                    console.qty(ln.qty),
                    console.money(ln.rate),
                    console.money(ln.line_total),
                ]
                for i, ln in enumerate(lines, start=1)
            ],
            headers=["#", "code", "qty", "rate", "total"],
        )
        for label, amount in charges.items():
            console.item(f"{label}: {console.money(amount)}")

        async with guarded(ctx, what=f"purchase {draft.invoice_no}"):
            # override_duplicate: the operator is reconstructing a bill
            # deliberately, and the duplicate check exists to catch the
            # same bill being *photographed* twice. Refusing here would
            # make re-entering a purged bill impossible.
            confirmed = await service.confirm(ctx.actor, draft, override_duplicate=True)
            console.item(f"recorded, grand total {console.money(confirmed.grand_total)}")

    run(action)


@add.command("sale")
def add_sale(
    customer: Annotated[str, typer.Option("--customer", help="Customer name, exactly")],
    line: Annotated[list[str], typer.Option("--line", help="CODE:QTY:RATE[:BRAND]")],
    charge: Annotated[list[str] | None, typer.Option("--charge", help="LABEL:AMOUNT")] = None,
    payment: Annotated[str, typer.Option("--payment", help="credit | cash")] = "credit",
    freight: Annotated[str, typer.Option("--freight", help="Freight recovered")] = "0",
) -> None:
    """Record a sale exactly as given."""

    async def action(ctx: AdminContext) -> None:
        customer_row = await resolve.customer_by_name(ctx.session, ctx.org_id, customer)
        try:
            payment_type = SalePaymentType(payment.lower())
        except ValueError as exc:
            allowed = ", ".join(p.value for p in SalePaymentType)
            raise AdminError(f"--payment must be one of: {allowed}") from exc
        charges = _parse_charges(charge or [])

        lines: list[SaleDraftLine] = []
        for entry in line:
            code, qty, rate, line_brand, _ = _split_line(entry, want_brand=True)
            product = await resolve.product_by_code(ctx.session, ctx.org_id, code, line_brand)
            lines.append(
                SaleDraftLine(
                    code=product.code,
                    qty=qty,
                    rate=rate,
                    product_id=product.id,
                    resolved_code=product.code,
                    brand_id=product.brand_id,
                )
            )

        draft = SaleDraft(
            customer_id=customer_row.id,
            customer_name=customer_row.name,
            payment_type=payment_type,
            lines=lines,
            freight=decimal.Decimal(freight),
            other_charges=sum(charges.values(), ZERO),
            charges=charges,
        )

        console.head(f"New sale to {customer_row.name}")
        console.table(
            [
                [
                    str(i),
                    ln.code,
                    console.qty(ln.qty),
                    console.money(ln.rate),
                    console.money(ln.line_total),
                ]
                for i, ln in enumerate(lines, start=1)
            ],
            headers=["#", "code", "qty", "rate", "total"],
        )
        for label, amount in charges.items():
            console.item(f"{label}: {console.money(amount)}")

        async with guarded(ctx, what=f"sale to {customer_row.name}"):
            service = SalesService(ctx.session)
            hydrated = await service.hydrate(ctx.org_id, draft)
            warnings = await service.check_warnings(ctx.org_id, hydrated)
            for text in getattr(warnings, "messages", []) or []:
                console.warn(text)
            # below_cost_confirmed: the warning is printed above, and the
            # operator is at a terminal reconstructing a sale that has
            # already happened -- refusing would leave the books wrong
            # rather than keep them right.
            confirmed = await service.record(ctx.actor, hydrated, below_cost_confirmed=True)
            console.item(f"recorded, grand total {console.money(confirmed.grand_total)}")

    run(action)
