"""`dashboard`, `summary`, `profit`, `supplier NAME`, `customer NAME`,
`ledger` -- docs/08_WhatsApp.md #dashboard, #summary, #profit,
#supplier-name, #customer-name, #ledger.
"""

from __future__ import annotations

import re

from backend.api.command_types import CommandResult, RequestContext
from backend.api.formatting import fmt_date, fmt_money, fmt_qty
from backend.api.period import parse_period, period_menu
from backend.core.exceptions import DomainError
from backend.core.security import role_at_least
from backend.models.enums import UserRole
from backend.repositories.accounting_repository import business_today
from backend.repositories.inventory_repository import InventoryRepository
from backend.repositories.party_repository import CustomerRepository, PartyStats, SupplierRepository
from backend.repositories.product_repository import ProductRepository
from backend.repositories.report_repository import ReportRepository
from backend.services.dashboard_service import DashboardData, DashboardService
from backend.services.profit_service import ProfitReport, ProfitService

_STATEMENT_LIMIT = 10


async def handle_dashboard(args: str, ctx: RequestContext) -> CommandResult:
    async with ctx.session_factory() as session:
        include_partner_capital = role_at_least(ctx.user.role, UserRole.OWNER)
        data = await DashboardService(session).summary(
            ctx.user.org_id, include_partner_capital=include_partner_capital
        )

    return CommandResult(reply=_render_dashboard(data))


def _render_dashboard(data: DashboardData) -> str:
    lines = [
        f"📊 Dashboard — {fmt_date(data.today)}",
        "",
        f"💰 Cash: {fmt_money(data.cash_balance)}   🏦 Bank: {fmt_money(data.bank_balance)}",
        f"📦 Inventory: {fmt_money(data.stock.total_value)} "
        f"({fmt_qty(data.stock.total_qty)} units across {data.active_products} products)",
        "",
        f"Today: 🛒 Sales {fmt_money(data.today_sales)} · "
        f"📥 Purchases {fmt_money(data.today_purchases)}",
        f"📈 Profit ({data.today.strftime('%b')}, MTD): {fmt_money(data.month_profit.net_profit)}",
        "",
        f"💸 Receivables: {fmt_money(data.receivables_total)} ({data.receivables_count} customers)",
        f"💳 Payables: {fmt_money(data.payables_total)} ({data.payables_count} suppliers)",
        "",
    ]
    if data.top_sellers:
        lines.append("🏆 Top sellers (this month): " + ", ".join(t.code for t in data.top_sellers))
    if data.slow_movers:
        worst = data.slow_movers[0]
        age = (
            f"{worst.days_since_sale} days no sale"
            if worst.days_since_sale is not None
            else "never sold"
        )
        lines.append(f"🐌 Slow moving: {worst.code} ({age})")
    if data.stock.low_count:
        lines.append(f'📉 Low stock: {data.stock.low_count} items — reply "stock low" for detail')
    if data.stock.negative_count:
        lines.append(
            f"⚠️ Negative stock: {data.stock.negative_count} item"
            f"{'s' if data.stock.negative_count > 1 else ''}"
            ' — reply "stock negative"'
        )
    if data.partner_balances is not None:
        lines.append("")
        parts = " · ".join(
            f"{p.display_name} {fmt_money(p.balance)}" for p in data.partner_balances
        )
        lines.append(f"👥 Partner capital — {parts}")
        if any(p.balance < 0 for p in data.partner_balances):
            lines.append("⚠️ A partner's capital balance is negative.")
    return "\n".join(lines)


async def handle_summary(args: str, ctx: RequestContext) -> CommandResult:
    if args.strip().lower() == "custom":
        return CommandResult(reply="Send the range like:\n*summary 01-07-2026 to 25-07-2026*")
    async with ctx.session_factory() as session:
        today = await business_today(session, ctx.user.org_id)
        try:
            period = parse_period(args, today)
        except DomainError as exc:
            return CommandResult(reply=exc.message)

        profit = await ProfitService(session).calculate(ctx.user.org_id, period.start, period.end)
        reports = ReportRepository(session)
        sales = await reports.sales_total(ctx.user.org_id, period.start, period.end)
        purchases = await reports.purchases_total(ctx.user.org_id, period.start, period.end)

    lines = [
        f"📋 Summary — {period.label}",
        f"🛒 Sales: {fmt_money(sales)}   📥 Purchases: {fmt_money(purchases)}",
        f"💵 Expenses: {fmt_money(profit.operating_expenses)}   "
        f"➕ Other income: {fmt_money(profit.other_income)}",
        f"📈 Net profit: {fmt_money(profit.net_profit)}",
    ]
    # answered first, menu second: a bare `summary` is a real question
    # with a sensible default, not an invitation to pick a period
    return CommandResult(reply="\n".join(lines), interactive=period_menu("summary"))


async def handle_profit(args: str, ctx: RequestContext) -> CommandResult:
    if args.strip().lower() == "custom":
        return CommandResult(reply="Send the range like:\n*profit 01-07-2026 to 25-07-2026*")
    async with ctx.session_factory() as session:
        today = await business_today(session, ctx.user.org_id)
        try:
            period = parse_period(args, today)
        except DomainError as exc:
            return CommandResult(reply=exc.message)

        report = await ProfitService(session).calculate(ctx.user.org_id, period.start, period.end)

    return CommandResult(
        reply=_render_profit(report, period.label), interactive=period_menu("profit")
    )


def _render_profit(report: ProfitReport, label: str) -> str:
    lines = [
        f"📈 Profit & Loss — {label}",
        f"Revenue: {fmt_money(report.revenue)}",
        f"COGS: {fmt_money(report.cogs)}",
        f"Gross profit: {fmt_money(report.gross_profit)}",
        f"Operating expenses: {fmt_money(report.operating_expenses)}",
        f"Other income: {fmt_money(report.other_income)}",
    ]
    if report.damage_loss:
        lines.append(f"Damage/write-off: {fmt_money(report.damage_loss)}")
    lines.append(f"Net profit: {fmt_money(report.net_profit)}")
    return "\n".join(lines)


def _render_party_stats(
    *, icon: str, name: str, label: str, stats: PartyStats, last_label: str
) -> str:
    lines = [
        f"{icon} {name}",
        f"Outstanding {label}: {fmt_money(stats.outstanding)}",
        f"  0–30d: {fmt_money(stats.aging.d0_30)} · 31–60d: {fmt_money(stats.aging.d31_60)} · "
        f"61–90d: {fmt_money(stats.aging.d61_90)} · 90+d: {fmt_money(stats.aging.d90_plus)}",
    ]
    if stats.last_invoice is not None:
        lines.append(
            f"{last_label}: {stats.last_invoice.reference}, "
            f"{fmt_date(stats.last_invoice.date)}, {fmt_money(stats.last_invoice.grand_total)}"
        )
    noun = "purchases" if label == "payable" else "sales"
    lines.append(
        f"{noun.capitalize()} this month: {stats.this_month_count} "
        f"({fmt_money(stats.this_month_total)} total)"
    )
    return "\n".join(lines)


async def handle_supplier(args: str, ctx: RequestContext) -> CommandResult:
    name = args.strip()
    if not name:
        return CommandResult(reply="Usage: supplier <name>")

    async with ctx.session_factory() as session:
        repo = SupplierRepository(session)
        matches = await repo.search(ctx.user.org_id, name, limit=1)
        if not matches:
            return CommandResult(reply=f"No supplier matching '{name}'.")
        supplier = matches[0]
        today = await business_today(session, ctx.user.org_id)
        stats = await repo.stats(ctx.user.org_id, supplier.id, today)

    return CommandResult(
        reply=_render_party_stats(
            icon="🏭", name=supplier.name, label="payable", stats=stats, last_label="Last purchase"
        )
    )


async def handle_customer(args: str, ctx: RequestContext) -> CommandResult:
    name = args.strip()
    if not name:
        return CommandResult(reply="Usage: customer <name>")

    async with ctx.session_factory() as session:
        repo = CustomerRepository(session)
        matches = await repo.search(ctx.user.org_id, name, limit=1)
        if not matches:
            return CommandResult(reply=f"No customer matching '{name}'.")
        customer = matches[0]
        today = await business_today(session, ctx.user.org_id)
        stats = await repo.stats(ctx.user.org_id, customer.id, today)

    return CommandResult(
        reply=_render_party_stats(
            icon="🧑‍💼", name=customer.name, label="receivable", stats=stats, last_label="Last sale"
        )
    )


_LEDGER_PARTY = re.compile(r"^(supplier|customer)\s+(.+)$", re.IGNORECASE)


async def handle_ledger(args: str, ctx: RequestContext) -> CommandResult:
    """`ledger <supplier|customer> <name>` or `ledger <CODE>` --
    docs/08_WhatsApp.md #ledger. Capped at the most recent entries, same
    "reply for more" convention as `stock all`, since there's no cursor-
    based pagination in this codebase yet."""
    text = args.strip()
    if not text:
        return CommandResult(reply="Usage: ledger <supplier|customer> <name>  or  ledger <CODE>")

    party_match = _LEDGER_PARTY.match(text)
    if party_match:
        kind, name = party_match.group(1).lower(), party_match.group(2).strip()
        return await _handle_party_ledger(kind, name, ctx)

    return await _handle_product_ledger(text, ctx)


async def _handle_party_ledger(kind: str, name: str, ctx: RequestContext) -> CommandResult:
    async with ctx.session_factory() as session:
        party_name: str
        if kind == "supplier":
            party_repo = SupplierRepository(session)
            supplier_matches = await party_repo.search(ctx.user.org_id, name, limit=1)
            if not supplier_matches:
                return CommandResult(reply=f"No supplier matching '{name}'.")
            supplier = supplier_matches[0]
            party_name, opening = supplier.name, supplier.opening_balance
            entries = await party_repo.statement(ctx.user.org_id, supplier.id)
        else:
            customer_repo = CustomerRepository(session)
            customer_matches = await customer_repo.search(ctx.user.org_id, name, limit=1)
            if not customer_matches:
                return CommandResult(reply=f"No customer matching '{name}'.")
            customer = customer_matches[0]
            party_name, opening = customer.name, customer.opening_balance
            entries = await customer_repo.statement(ctx.user.org_id, customer.id)

    if not entries:
        return CommandResult(reply=f"No transactions yet for {party_name}.")

    running = opening
    rows: list[tuple[str, str]] = []
    for entry in entries:
        running += entry.amount
        sign = "+" if entry.amount >= 0 else "-"
        rows.append(
            (
                f"{fmt_date(entry.date)} {entry.description} {sign}{fmt_money(abs(entry.amount))}",
                fmt_money(running),
            )
        )

    tail = rows[-_STATEMENT_LIMIT:]
    lines = [f"📒 Ledger — {party_name}"]
    if len(rows) > len(tail):
        lines.append(f"({len(rows) - len(tail)} earlier entries not shown)")
    for description, balance in tail:
        lines.append(f"• {description} → balance {balance}")
    lines.append(f"Current balance: {fmt_money(running)}")
    return CommandResult(reply="\n".join(lines))


async def _handle_product_ledger(code: str, ctx: RequestContext) -> CommandResult:
    async with ctx.session_factory() as session:
        products = ProductRepository(session)
        found = await products.list_by_code(ctx.user.org_id, code)
        if not found:
            return CommandResult(reply=f"Product '{code}' not found.")
        if len(found) > 1:
            labels = ", ".join(p.brand.name if p.brand else "no brand" for p in found)
            return CommandResult(
                reply=f"'{code}' is stocked under {len(found)} brands ({labels}) — "
                "which one? Try 'ledger <CODE>' again once products carry distinct codes, "
                "or check 'stock <CODE>' for each brand's detail."
            )
        product = found[0]
        movements = await InventoryRepository(session).movement_history(
            ctx.user.org_id, product.id, limit=_STATEMENT_LIMIT
        )

    if not movements:
        return CommandResult(reply=f"No movements yet for {product.code}.")

    unit = product.unit.code
    lines = [f"📒 Ledger — {product.code} ({product.description})"]
    for movement in movements:
        sign = "+" if movement.qty_delta > 0 else "-"
        lines.append(
            f"• {fmt_date(movement.created_at.date())} {movement.movement_type.value} "
            f"{sign}{fmt_qty(abs(movement.qty_delta))} {unit} "
            f"→ {fmt_qty(movement.resulting_qty_on_hand)} {unit} on hand"
        )
    return CommandResult(reply="\n".join(lines))
