"""`stock` family and `search` -- docs/08_WhatsApp.md #stock,
#stock-code, #search. Reply copy follows the doc's examples."""

from __future__ import annotations

from backend.api.command_types import CommandResult, RequestContext
from backend.api.formatting import fmt_date, fmt_money, fmt_qty
from backend.api.interactive import MAX_LIST_ROWS, Choice, ListMenu, Section
from backend.repositories.inventory_repository import LowStockRow
from backend.services.stock_service import StockService


def _low_stock_lines(rows: list[LowStockRow]) -> list[str]:
    lines = []
    for row in rows:
        if row.qty_on_hand < 0:
            lines.append(
                f"• {row.code} — {fmt_qty(row.qty_on_hand)} {row.unit_code} (⚠️ negative stock)"
            )
        else:
            reorder = (
                f" (reorder at {fmt_qty(row.reorder_level)} {row.unit_code})"
                if row.reorder_level is not None
                else ""
            )
            lines.append(f"• {row.code} — {fmt_qty(row.qty_on_hand)} {row.unit_code} left{reorder}")
    return lines


async def handle_stock(args: str, ctx: RequestContext) -> CommandResult:
    sub = args.strip()
    async with ctx.session_factory() as session:
        service = StockService(session)
        org_id = ctx.user.org_id

        if not sub:
            summary = await service.summary(org_id)
            lines = [
                f"📦 Stock summary ({summary.active_products} active products)",
                f"Total value: {fmt_money(summary.totals.total_value)}",
            ]
            if summary.totals.low_count:
                lines.append(
                    f'Low stock: {summary.totals.low_count} items (reply "stock low" to see them)'
                )
            else:
                lines.append("Low stock: none")
            if summary.totals.negative_count:
                lines.append(
                    f"Negative stock: {summary.totals.negative_count} item"
                    f"{'s' if summary.totals.negative_count > 1 else ''} ⚠️"
                    ' (reply "stock negative" to see them)'
                )
            return CommandResult(reply="\n".join(lines))

        if sub.lower() in {"low", "negative"}:
            negative_only = sub.lower() == "negative"
            rows = await service.low_stock(org_id, negative_only=negative_only)
            if not rows:
                return CommandResult(
                    reply="✅ No negative stock." if negative_only else "✅ No low stock items."
                )
            title = "⚠️ Negative stock" if negative_only else "📉 Low stock"
            return CommandResult(
                reply="\n".join([f"{title} ({len(rows)} items):", *_low_stock_lines(rows)])
            )

        # "55D MKD" -- the brand comes back this way when the reader taps a
        # row on the "Pick brand" menu below, which sends its own id as
        # text. Brand names contain spaces ("Akil Bhai"), so only the
        # first word is the code and the remainder is all brand.
        code, _, rest = sub.partition(" ")
        brand_hint = rest.strip() or None

        found = await service.details(org_id, code, brand=brand_hint)
        if not found:
            if brand_hint is not None:
                carried = await service.details(org_id, code)
                if carried:
                    brands = ", ".join(
                        e.product.brand.name for e in carried if e.product.brand is not None
                    )
                    return CommandResult(
                        reply=f"'{code.upper()}' is not stocked under '{brand_hint}'."
                        + (f" It is stocked under: {brands}." if brands else "")
                    )
            suggestions = await service.suggest_codes(org_id, code)
            hint = f" Did you mean {', '.join(suggestions)}?" if suggestions else ""
            return CommandResult(reply=f"Product '{code}' not found.{hint}")

        if len(found) > 1:
            # the same code under several brands -- show each rather than
            # guessing which one was meant
            lines = [f"📦 {code.upper()} is stocked under {len(found)} brands:"]
            for entry in found:
                label = entry.product.brand.name if entry.product.brand else "no brand"
                unit_code = entry.product.unit.code
                lines.append(
                    f"• {label} — {entry.product.description}: "
                    f"{fmt_qty(entry.qty_on_hand)} {unit_code} "
                    f"@ {fmt_money(entry.weighted_avg_cost)}/{unit_code}"
                )
            menu = None
            if len(found) <= MAX_LIST_ROWS:
                menu = ListMenu(
                    body=f"Which {code.upper()}?",
                    menu_label="Pick brand",
                    sections=(
                        Section(
                            title="Brands",
                            rows=tuple(
                                Choice(
                                    id=f"stock {code} {e.product.brand.name}"
                                    if e.product.brand
                                    else f"stock {code}",
                                    title=(e.product.brand.name if e.product.brand else "No brand")[
                                        :24
                                    ],
                                    description=(
                                        f"{fmt_qty(e.qty_on_hand)} {e.product.unit.code} "
                                        f"@ {fmt_money(e.weighted_avg_cost)}"
                                    )[:72],
                                )
                                for e in found
                            ),
                        ),
                    ),
                )
            return CommandResult(reply="\n".join(lines), interactive=menu)

        detail = found[0]
        product = detail.product
        unit = product.unit.code
        brand = f" ({product.brand.name})" if product.brand else ""
        lines = [
            f"📦 {product.code} — {product.description}{brand}",
            f"On hand: {fmt_qty(detail.qty_on_hand)} {unit}",
            f"Avg cost: {fmt_money(detail.weighted_avg_cost)}/{unit}",
            f"Stock value: {fmt_money(detail.stock_value)}",
        ]
        if product.reorder_level is not None:
            lines.append(f"Reorder level: {fmt_qty(product.reorder_level)} {unit}")
        if detail.last_movement is not None:
            movement = detail.last_movement
            sign = "+" if movement.qty_delta > 0 else "-"
            lines.append(
                f"Last movement: {movement.movement_type.value} "
                f"{sign}{fmt_qty(abs(movement.qty_delta))} {unit} "
                f"({fmt_date(movement.created_at.date())})"
            )
        return CommandResult(reply="\n".join(lines))


async def handle_search(args: str, ctx: RequestContext) -> CommandResult:
    query = args.strip()
    if not query:
        return CommandResult(reply="Usage: search <text>")

    async with ctx.session_factory() as session:
        results = await StockService(session).search(ctx.user.org_id, query)

    if results.is_empty:
        return CommandResult(reply=f"No matches for '{query}'.")

    lines = [f"🔎 Results for '{query}':"]
    if results.products:
        lines.append("Products:")
        for product, qty in results.products:
            lines.append(
                f"• {product.code} — {product.description}"
                f" ({fmt_qty(qty)} {product.unit.code} on hand)"
            )
    if results.suppliers:
        lines.append("Suppliers:")
        lines.extend(f"• {s.name}" for s in results.suppliers)
    if results.customers:
        lines.append("Customers:")
        lines.extend(f"• {c.name}" for c in results.customers)
    return CommandResult(reply="\n".join(lines))
