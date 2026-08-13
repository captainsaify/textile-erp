"""Dashboard, accounting and report endpoints -- docs/10_API.md §4.

Every figure here comes from the same service the WhatsApp command
uses. docs/12_Dashboard.md §1 is explicit that there must never be two
implementations of "what is today's profit", so these endpoints are
adapters, not calculations.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select

from backend.api.amounts import money_str, qty_str
from backend.api.deps import CurrentUser, OwnerUser, Paging, Session
from backend.repositories.accounting_repository import LedgerRepository, business_today
from backend.services.dashboard_service import DashboardService
from backend.services.profit_service import ProfitService
from backend.services.report_service import REPORT_TYPES, ReportService

router = APIRouter(prefix="/api/v1", tags=["reporting"])


@router.get("/dashboard")
async def dashboard(user: CurrentUser, session: Session) -> dict[str, Any]:
    from backend.models.enums import UserRole

    data = await DashboardService(session).summary(
        user.org_id, include_partner_capital=user.role is UserRole.OWNER
    )
    return {
        "date": data.today.isoformat(),
        "cash_balance": money_str(data.cash_balance),
        "bank_balance": money_str(data.bank_balance),
        "inventory": {
            "value": money_str(data.stock.total_value),
            "qty": qty_str(data.stock.total_qty),
            "active_products": data.active_products,
            "low_stock_count": data.stock.low_count,
            "negative_stock_count": data.stock.negative_count,
        },
        "today": {
            "sales": money_str(data.today_sales),
            "purchases": money_str(data.today_purchases),
        },
        "month_profit": {
            "revenue": money_str(data.month_profit.revenue),
            "cogs": money_str(data.month_profit.cogs),
            "gross_profit": money_str(data.month_profit.gross_profit),
            "operating_expenses": money_str(data.month_profit.operating_expenses),
            "other_income": money_str(data.month_profit.other_income),
            "net_profit": money_str(data.month_profit.net_profit),
        },
        "receivables": {
            "total": money_str(data.receivables_total),
            "parties": data.receivables_count,
        },
        "payables": {"total": money_str(data.payables_total), "parties": data.payables_count},
        "top_sellers": [
            {"code": t.code, "description": t.description, "revenue": money_str(t.revenue)}
            for t in data.top_sellers
        ],
        "slow_movers": [
            {"code": s.code, "description": s.description, "days_since_sale": s.days_since_sale}
            for s in data.slow_movers
        ],
        # absent, not null-and-hidden, for a non-owner (§6 of the
        # dashboard doc: the section simply isn't there)
        **(
            {
                "partner_capital": [
                    {"partner": p.display_name, "balance": money_str(p.balance)}
                    for p in data.partner_balances
                ]
            }
            if data.partner_balances is not None
            else {}
        ),
    }


@router.get("/reports/profit-loss")
async def profit_loss(
    user: OwnerUser,
    session: Session,
    date_from: Annotated[datetime.date | None, Query()] = None,
    date_to: Annotated[datetime.date | None, Query()] = None,
) -> dict[str, Any]:
    today = await business_today(session, user.org_id)
    start = date_from or today.replace(day=1)
    end = date_to or today
    report = await ProfitService(session).calculate(user.org_id, start, end)
    return {
        "period": {"from": start.isoformat(), "to": end.isoformat()},
        "revenue": money_str(report.revenue),
        "cogs": money_str(report.cogs),
        "gross_profit": money_str(report.gross_profit),
        "operating_expenses": money_str(report.operating_expenses),
        "other_income": money_str(report.other_income),
        "damage_loss": money_str(report.damage_loss),
        "net_profit": money_str(report.net_profit),
    }


@router.get("/ledgers/{ledger}")
async def ledger(
    ledger: str,
    user: CurrentUser,
    session: Session,
    paging: Paging,
) -> dict[str, Any]:
    from fastapi import HTTPException

    if ledger not in {"cash", "bank"}:
        raise HTTPException(status_code=404, detail="no such ledger")
    repo = LedgerRepository(session)
    entries = await repo.recent_entries(user.org_id, ledger, limit=paging.limit)
    # A reversal and the entry it reversed are both real rows and both
    # belong in the list -- nothing is ever deleted. They do not belong
    # in "money in" and "money out", where between them they claimed
    # twice an amount that never moved.
    cancelled = LedgerRepository.cancelled_ids(entries)
    references = await _payment_references(session, user.org_id, entries)
    # The dashboard sorts these by date before drawing them, so the
    # balance column has to run in date order too. `resulting_balance`
    # runs in insertion order and stops agreeing the moment an entry is
    # backdated -- which is how a payment dated 20-07 came to sit
    # mid-page carrying the balance from the end of the book.
    closing = await repo.balance(user.org_id, ledger)
    in_date_order = sorted(entries, key=lambda row: (row.entry_date, row.created_at, row.id))
    moved = sum(
        (row.amount for row in in_date_order if row.id not in cancelled), decimal.Decimal("0")
    )
    balances = LedgerRepository.running_balances(
        in_date_order, cancelled=cancelled, opening=closing - moved
    )
    return {
        "balance": money_str(closing),
        "entries": [
            {
                "date": entry.entry_date.isoformat(),
                "type": entry.entry_type.value,
                "amount": money_str(entry.amount),
                "resulting_balance": money_str(balances[entry.id]),
                "notes": entry.notes,
                "cancelled": entry.id in cancelled,
                # A settlement's handle, so the row can offer its
                # receipt. Absent on everything else -- an expense or a
                # capital contribution has no second document.
                "reference": references.get(entry.id),
            }
            for entry in entries
        ],
    }


async def _payment_references(
    session: Any, org_id: uuid.UUID, entries: list[Any]
) -> dict[uuid.UUID, str]:
    """Match settlement rows back to the audit entry that is their
    reference, so each can offer its receipt.

    Matched on party and amount rather than a stored link. Two identical
    payments to the same party on the same day are genuinely
    interchangeable -- either receipt describes either row -- so the
    ambiguity costs nothing.
    """
    from backend.models import AuditLog

    payment_rows = [e for e in entries if e.source_type in {"supplier_payment", "customer_payment"}]
    if not payment_rows:
        return {}

    audit = (
        (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.org_id == org_id,
                    AuditLog.action.in_(["payment.paid", "payment.received"]),
                    AuditLog.entity_id.in_({e.source_id for e in payment_rows}),
                )
            )
        )
        .scalars()
        .all()
    )
    pool: dict[tuple[uuid.UUID, str], list[str]] = {}
    for entry in audit:
        state = entry.after_state or {}
        pool.setdefault((entry.entity_id, str(state.get("amount", ""))), []).append(
            str(entry.id)[:8]
        )

    references: dict[uuid.UUID, str] = {}
    for row in payment_rows:
        candidates = pool.get((row.source_id, f"{abs(row.amount):.2f}")) or []
        if candidates:
            references[row.id] = candidates.pop()
    return references


@router.get("/expenses")
async def expenses(
    user: CurrentUser,
    session: Session,
    date_from: Annotated[datetime.date | None, Query()] = None,
    date_to: Annotated[datetime.date | None, Query()] = None,
) -> dict[str, Any]:
    """What the business spent, and on what.

    Expenses were visible only as a single `operating_expenses` figure
    inside the month's profit -- a number with nothing behind it. Three
    partners spend from one cash box, so "₹27,940 this month" is not an
    answer to anything; ₹21,200 of flights and ₹1,780 of personal is.

    Not owner-only. Staff record most of these, and a person who can add
    an expense but cannot see the list has no way to tell whether theirs
    went in twice.
    """
    from backend.repositories.accounting_repository import ExpenseRepository

    today = await business_today(session, user.org_id)
    start = date_from or today.replace(day=1)
    end = date_to or today

    repo = ExpenseRepository(session)
    rows = await repo.in_period(user.org_id, start, end)
    grouped = await repo.by_category(user.org_id, start, end)
    total = sum((amount for _, amount, _ in grouped), decimal.Decimal("0"))

    return {
        "period": {"from": start.isoformat(), "to": end.isoformat()},
        "total": money_str(total),
        "count": sum(count for _, _, count in grouped),
        "by_category": [
            {
                "category": category,
                "total": money_str(amount),
                "count": count,
                # Computed here rather than in the browser so the figure
                # in a shared screenshot matches the one in an export.
                "share": (f"{(amount / total * 100):.1f}" if total else "0.0"),
            }
            for category, amount, count in grouped
        ],
        "entries": [
            {
                "id": str(row.id)[:8],
                "date": row.entry_date.isoformat(),
                "category": row.category,
                "amount": money_str(row.amount),
                "paid_via": row.paid_via,
                "description": row.description or "",
                "spent_by": row.spent_by,
            }
            for row in rows
        ],
    }


class ExportRequest(BaseModel):
    type: str
    date_from: datetime.date | None = None
    date_to: datetime.date | None = None


@router.post("/reports/export", status_code=status.HTTP_202_ACCEPTED)
async def request_export(
    body: ExportRequest, user: CurrentUser, session: Session
) -> dict[str, Any]:
    """202 with a job id: building a workbook can outlast a request, so
    the caller polls (docs/11_BackgroundWorkers.md §8)."""
    from fastapi import HTTPException

    if body.type not in REPORT_TYPES:
        raise HTTPException(status_code=400, detail=f"type must be one of {sorted(REPORT_TYPES)}")
    # Not `async with session.begin()`: authenticating the request read
    # the user through this same session, so it has already autobegun and
    # begin() raises (HANDOFF.md §5). This endpoint 500'd on every call
    # until a test finally exercised it.
    today = await business_today(session, user.org_id)
    job = await ReportService(session).enqueue(
        user,
        report_type=body.type,
        start=body.date_from or today.replace(day=1),
        end=body.date_to or today,
    )
    job_id = job.id
    await session.commit()

    from backend.api.commands.ops_commands import _dispatch_report

    _dispatch_report(str(job_id))
    return {"job_id": str(job_id), "status": "queued"}


@router.get("/metrics/monthly")
async def monthly_metrics(
    user: OwnerUser,
    session: Session,
    months: Annotated[int, Query(ge=1, le=24)] = 6,
) -> dict[str, Any]:
    """Revenue, purchases and net profit per calendar month, for the
    dashboard's trend charts.

    Computed by calling ProfitService once per month rather than from a
    rollup table. docs/21 §9 warns that replaying the journal per request
    won't scale, and it won't -- but at this business's volume a handful
    of months is milliseconds, and the alternative (a `daily_org_metrics`
    table) is a second place for "what was our profit" to live. When the
    query gets slow, add the rollup and keep these figures as its
    source of truth rather than recomputing them differently.

    Owner-only: profit is partner-level information (docs/14 #rbac).
    """
    today = await business_today(session, user.org_id)
    profit_service = ProfitService(session)

    points: list[dict[str, Any]] = []
    cursor = today.replace(day=1)
    for _ in range(months):
        end = _month_end(cursor, today)
        report = await profit_service.calculate(user.org_id, cursor, end)
        points.append(
            {
                "month": cursor.strftime("%Y-%m"),
                "label": cursor.strftime("%b %Y"),
                "revenue": money_str(report.revenue),
                "cogs": money_str(report.cogs),
                "expenses": money_str(report.operating_expenses),
                "net_profit": money_str(report.net_profit),
            }
        )
        cursor = (cursor - datetime.timedelta(days=1)).replace(day=1)

    return {"data": list(reversed(points))}


def _month_end(month_start: datetime.date, today: datetime.date) -> datetime.date:
    """The current month stops at today; earlier months run to their
    last day. A month-to-date figure plotted as if it were a full month
    is how a trend chart invents a downturn."""
    if (month_start.year, month_start.month) == (today.year, today.month):
        return today
    next_month = (month_start + datetime.timedelta(days=32)).replace(day=1)
    return next_month - datetime.timedelta(days=1)


@router.get("/reports/export/{job_id}")
async def export_status(job_id: str, user: CurrentUser, session: Session) -> dict[str, Any]:
    import uuid

    from fastapi import HTTPException

    from backend.models import ReportJob

    try:
        job = await session.get(ReportJob, uuid.UUID(job_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="no such report job") from None
    if job is None or job.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="no such report job")
    return {
        "job_id": str(job.id),
        "type": job.report_type,
        "status": job.status,
        "row_count": job.row_count,
        "size_bytes": job.file_size_bytes,
        "error": job.error,
        "expires_at": job.expires_at.isoformat() if job.expires_at else None,
    }


def _qty(value: decimal.Decimal | None) -> str:
    """A quantity a person can read.

    `Decimal.normalize()` alone renders 2480 as `2.48E+3` -- correct,
    and unreadable on a dashboard next to a rupee figure. `format(..,
    "f")` keeps it positional while still dropping the trailing zeros
    that make a count look like a measurement.
    """
    return format(decimal.Decimal(value or 0).normalize(), "f")


@router.get("/metrics/products")
async def product_performance(
    user: OwnerUser,
    session: Session,
    days: Annotated[int, Query(ge=1, le=730)] = 90,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """Which goods actually earn, over a window.

    Margin is computed from `sales_lines.avg_cost_at_sale_time` -- the
    cost snapshot taken when the goods went out -- not from today's
    weighted average. Those differ whenever anything was bought since,
    and using today's figure would silently re-price history: a product
    would look more profitable simply because the last purchase of it
    was cheap. The snapshot is what the sale actually cost.

    Returns per product, so the two questions get different answers:
    `margin_pct` is "does this sell well", `profit` is "does this
    matter". A code with a 60% margin and nine kilos sold is a curiosity;
    one at 8% carrying half the turnover is the business.

    Owner-only: margin is partner-level information (docs/14 #rbac).
    """
    from backend.models import Brand, Product, SalesHeader, SalesLine

    today = await business_today(session, user.org_id)
    since = today - datetime.timedelta(days=days)

    revenue = func.sum(SalesLine.line_total)
    cost = func.sum(SalesLine.qty * SalesLine.avg_cost_at_sale_time)
    sold = func.sum(SalesLine.qty)

    rows = (
        await session.execute(
            select(
                Product.code,
                Brand.name,
                Product.description,
                sold.label("qty"),
                revenue.label("revenue"),
                cost.label("cost"),
            )
            .join(SalesLine, SalesLine.product_id == Product.id)
            .join(SalesHeader, SalesHeader.id == SalesLine.sales_header_id)
            .join(Brand, Brand.id == Product.brand_id, isouter=True)
            .where(
                SalesHeader.org_id == user.org_id,
                SalesHeader.deleted_at.is_(None),
                SalesHeader.sale_date >= since,
                # Returned goods never earned anything; counting the
                # sale without the return would show a margin on stock
                # that came back through the door.
                SalesLine.qty > SalesLine.returned_qty,
            )
            .group_by(Product.code, Brand.name, Product.description)
            .having(sold > 0)
        )
    ).all()

    items: list[dict[str, Any]] = []
    for code, brand, description, qty, gross, spent in rows:
        gross = decimal.Decimal(gross or 0)
        spent = decimal.Decimal(spent or 0)
        qty = decimal.Decimal(qty or 0)
        profit = gross - spent
        items.append(
            {
                "code": code,
                "brand": brand,
                "description": description,
                "qty": _qty(qty),
                "revenue": money_str(gross),
                "cost": money_str(spent),
                "profit": money_str(profit),
                "avg_rate": money_str(gross / qty) if qty else money_str(decimal.Decimal(0)),
                "avg_cost": money_str(spent / qty) if qty else money_str(decimal.Decimal(0)),
                # Margin on revenue, not on cost. "We keep 12% of what
                # comes in" is the sentence a trader checks against a
                # price; markup on cost answers a different question and
                # reads about 40% higher for the same goods.
                "margin_pct": (
                    str((profit / gross * 100).quantize(decimal.Decimal("0.1"))) if gross else "0.0"
                ),
                "below_cost": profit < 0,
            }
        )

    by_profit = sorted(items, key=lambda i: decimal.Decimal(i["profit"]), reverse=True)
    by_margin = sorted(items, key=lambda i: decimal.Decimal(i["margin_pct"]), reverse=True)
    return {
        "days": days,
        "best_by_profit": by_profit[:limit],
        "best_by_margin": by_margin[:limit],
        "losing": [i for i in by_profit if i["below_cost"]],
    }


@router.get("/metrics/brands")
async def brand_performance(
    user: OwnerUser,
    session: Session,
    days: Annotated[int, Query(ge=1, le=730)] = 90,
) -> dict[str, Any]:
    """The same question as `/metrics/products`, asked per label.

    Brands are how this business thinks about its goods -- a code means
    nothing without one, and three products share `55X` on these books.
    So "is BSQ earning" is a question the product view cannot answer.
    """
    from backend.models import Brand, Product, SalesHeader, SalesLine

    today = await business_today(session, user.org_id)
    since = today - datetime.timedelta(days=days)

    rows = (
        await session.execute(
            select(
                # Grouped on the column, not on coalesce(...): the literal
                # binds as a parameter and Postgres will not match the two
                # expressions, so it demands brands.name in GROUP BY anyway.
                # The null becomes a dash below, where it is a display concern.
                Brand.name.label("brand"),
                func.sum(SalesLine.qty).label("qty"),
                func.sum(SalesLine.line_total).label("revenue"),
                func.sum(SalesLine.qty * SalesLine.avg_cost_at_sale_time).label("cost"),
                func.count(func.distinct(SalesHeader.id)).label("sales"),
                func.count(func.distinct(Product.id)).label("codes"),
            )
            .join(SalesLine, SalesLine.product_id == Product.id)
            .join(SalesHeader, SalesHeader.id == SalesLine.sales_header_id)
            .join(Brand, Brand.id == Product.brand_id, isouter=True)
            .where(
                SalesHeader.org_id == user.org_id,
                SalesHeader.deleted_at.is_(None),
                SalesHeader.sale_date >= since,
            )
            .group_by(Brand.name)
        )
    ).all()

    items: list[dict[str, Any]] = []
    for brand, qty, gross_raw, cost_raw, sales, codes in rows:
        gross = decimal.Decimal(gross_raw or 0)
        spent = decimal.Decimal(cost_raw or 0)
        profit = gross - spent
        items.append(
            {
                "brand": brand or "—",
                "qty": _qty(qty),
                "revenue": money_str(gross),
                "profit": money_str(profit),
                "margin_pct": (
                    str((profit / gross * 100).quantize(decimal.Decimal("0.1"))) if gross else "0.0"
                ),
                "sales": sales,
                "codes": codes,
            }
        )
    items.sort(key=lambda i: decimal.Decimal(i["profit"]), reverse=True)
    return {"days": days, "brands": items}


@router.get("/metrics/stock-health")
async def stock_health(
    user: OwnerUser,
    session: Session,
    days: Annotated[int, Query(ge=7, le=730)] = 90,
) -> dict[str, Any]:
    """What to reorder, what is not moving, and how thin the evidence is.

    Deliberately **not** a forecast, and that is a decision rather than
    an omission. At this business's volume -- a fortnight of trading and
    a couple of dozen sales -- a fitted trend is noise drawn
    confidently, and it would look more authoritative on a dashboard
    than the numbers it was made from.

    What this does instead is arithmetic anyone can check: what sold,
    over how many days, therefore how long the stock on hand lasts at
    that rate. Every row carries `sale_count` and `sold_over_days`, so
    a days-of-cover figure built on two sales is visibly a different
    object from one built on fifty. Hiding that difference is what makes
    a dashboard lie.
    """
    from backend.models import Brand, Inventory, Product, SalesHeader, SalesLine

    today = await business_today(session, user.org_id)
    since = today - datetime.timedelta(days=days)

    movement = (
        select(
            SalesLine.product_id.label("product_id"),
            func.sum(SalesLine.qty).label("sold"),
            func.count(func.distinct(SalesHeader.id)).label("sale_count"),
            func.max(SalesHeader.sale_date).label("last_sold"),
        )
        .join(SalesHeader, SalesHeader.id == SalesLine.sales_header_id)
        .where(
            SalesHeader.org_id == user.org_id,
            SalesHeader.deleted_at.is_(None),
            SalesHeader.sale_date >= since,
        )
        .group_by(SalesLine.product_id)
        .subquery()
    )

    rows = (
        await session.execute(
            select(
                Product.code,
                Brand.name,
                Product.description,
                Product.reorder_level,
                Inventory.qty_on_hand,
                Inventory.weighted_avg_cost,
                movement.c.sold,
                movement.c.sale_count,
                movement.c.last_sold,
            )
            .join(Inventory, Inventory.product_id == Product.id)
            .join(Brand, Brand.id == Product.brand_id, isouter=True)
            .join(movement, movement.c.product_id == Product.id, isouter=True)
            .where(Product.org_id == user.org_id, Product.deleted_at.is_(None))
        )
    ).all()

    window = decimal.Decimal(days)
    reorder: list[dict[str, Any]] = []
    dead: list[dict[str, Any]] = []
    for code, brand, description, level, on_hand_raw, cost, sold_raw, sale_count, last in rows:
        on_hand = decimal.Decimal(on_hand_raw or 0)
        sold = decimal.Decimal(sold_raw or 0)
        per_day = sold / window if sold else decimal.Decimal(0)
        cover = int(on_hand / per_day) if per_day > 0 else None

        row: dict[str, Any] = {
            "code": code,
            "brand": brand or "—",
            "description": description,
            "on_hand": _qty(on_hand),
            "value": money_str(on_hand * decimal.Decimal(cost or 0)),
            "sold": _qty(sold),
            "sale_count": sale_count or 0,
            "sold_over_days": days,
            "last_sold": last.isoformat() if last else None,
            "days_of_cover": cover,
            "reorder_level": _qty(level),
        }
        if on_hand <= 0 or (level and on_hand <= decimal.Decimal(level)):
            row["reason"] = "at or below reorder level"
            reorder.append(row)
        elif cover is not None and cover <= 30:
            row["reason"] = f"about {cover} days left at the recent rate"
            reorder.append(row)
        elif sold == 0 and on_hand > 0:
            row["reason"] = f"nothing sold in {days} days"
            dead.append(row)

    reorder.sort(key=lambda r: (r["days_of_cover"] is None, r["days_of_cover"] or 0))
    dead.sort(key=lambda r: decimal.Decimal(r["value"]), reverse=True)
    return {
        "days": days,
        "reorder": reorder,
        "dead_stock": dead,
        "dead_value": money_str(
            sum((decimal.Decimal(r["value"]) for r in dead), decimal.Decimal(0))
        ),
    }
