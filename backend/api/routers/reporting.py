"""Dashboard, accounting and report endpoints -- docs/10_API.md §4.

Every figure here comes from the same service the WhatsApp command
uses. docs/12_Dashboard.md §1 is explicit that there must never be two
implementations of "what is today's profit", so these endpoints are
adapters, not calculations.
"""

from __future__ import annotations

import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, status
from pydantic import BaseModel

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
    return {
        "balance": money_str(await repo.balance(user.org_id, ledger)),
        "entries": [
            {
                "date": entry.entry_date.isoformat(),
                "type": entry.entry_type.value,
                "amount": money_str(entry.amount),
                "resulting_balance": money_str(entry.resulting_balance),
                "notes": entry.notes,
                "cancelled": entry.id in cancelled,
            }
            for entry in entries
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
