"""Downloadable sheets for every page of the dashboard -- docs/28 §2.4.

`POST /reports/export` already exists and stays: it is what the
WhatsApp `export` command uses, and a chat export genuinely has to be a
background job -- the reply is a file that arrives later.

A browser asking for a fifteen-row cashbook should not have to enqueue,
poll and then follow a link. These endpoints create the *same*
`report_jobs` row, run the *same* builder, and stream the result. Same
code, same numbers, same record of who exported what; only the delivery
differs.

Every export is still a `report_jobs` row on purpose. An export is a
copy of the business's figures leaving the system, and one taken from
the browser must be exactly as traceable as one taken from the chat.
"""

from __future__ import annotations

import datetime
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response

from backend.api.deps import CurrentUser, Session
from backend.core.exceptions import ValidationError
from backend.models import User
from backend.repositories.accounting_repository import business_today
from backend.services.report_service import ReportService

router = APIRouter(prefix="/api/v1/exports", tags=["exports"])

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

Since = Annotated[datetime.date | None, Query(alias="from")]
Until = Annotated[datetime.date | None, Query(alias="to")]


async def _build(
    session: Any,
    user: User,
    *,
    report_type: str,
    start: datetime.date | None,
    end: datetime.date | None,
    filters: dict[str, Any] | None = None,
    filename: str,
) -> Response:
    """Enqueue, generate inline, stream.

    Not `async with session.begin()`: authenticating the request read
    the user through this same session, so it has already autobegun and
    a `begin()` here raises (HANDOFF.md §5).
    """
    today = await business_today(session, user.org_id)
    service = ReportService(session)
    try:
        job = await service.enqueue(
            user,
            report_type=report_type,
            # No `from`/`to` means everything, not this month: a browser
            # asking for "the purchases sheet" wants the purchases, and
            # silently returning only the current month would look like
            # data loss.
            start=start or datetime.date(2000, 1, 1),
            end=end or today,
            filters=filters or {},
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    job_id = job.id
    await session.commit()

    result = await service.generate(job_id)
    if result.status != "ready" or result.file_path is None:
        raise HTTPException(status_code=500, detail=result.message)

    return Response(
        content=Path(result.file_path).read_bytes(),
        media_type=XLSX_MIME,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


def _stamp(start: datetime.date | None, end: datetime.date | None) -> str:
    if start is None and end is None:
        return "all"
    return f"{(start or datetime.date.min):%Y%m%d}-{(end or datetime.date.max):%Y%m%d}"


@router.get("/purchases.xlsx")
async def purchases(
    user: CurrentUser, session: Session, date_from: Since = None, date_to: Until = None
) -> Response:
    """Every bill in the period, one worksheet each -- the summary
    counterpart to the per-row sheet on the Purchases page."""
    return await _build(
        session,
        user,
        report_type="purchases",
        start=date_from,
        end=date_to,
        filename=f"purchases-{_stamp(date_from, date_to)}.xlsx",
    )


@router.get("/sales.xlsx")
async def sales(
    user: CurrentUser, session: Session, date_from: Since = None, date_to: Until = None
) -> Response:
    return await _build(
        session,
        user,
        report_type="sales",
        start=date_from,
        end=date_to,
        filename=f"sales-{_stamp(date_from, date_to)}.xlsx",
    )


@router.get("/stock.xlsx")
async def stock(user: CurrentUser, session: Session) -> Response:
    """Stock is a position, not a period -- it is what is on the shelf
    now, so it takes no date range."""
    return await _build(
        session,
        user,
        report_type="stock",
        start=None,
        end=None,
        filename="stock.xlsx",
    )


@router.get("/parties.xlsx")
async def parties(
    user: CurrentUser,
    session: Session,
    role: Annotated[str, Query(pattern="^(supplier|customer)$")] = "supplier",
) -> Response:
    """Who owes what and for how long, every party side by side, each
    one's transactions on their own tab."""
    return await _build(
        session,
        user,
        report_type="ledger",
        start=None,
        end=None,
        filters={"role": role},
        filename=f"{role}s.xlsx",
    )


@router.get("/statement.xlsx")
async def statement(
    user: CurrentUser,
    session: Session,
    party_id: uuid.UUID,
    kind: Annotated[str, Query(pattern="^(supplier|customer)$")] = "supplier",
    date_from: Since = None,
    date_to: Until = None,
) -> Response:
    """One party, every bill and payment in order, with a running
    balance and an opening balance brought forward."""
    key = "supplier_id" if kind == "supplier" else "customer_id"
    return await _build(
        session,
        user,
        report_type="statement",
        start=date_from,
        end=date_to,
        filters={key: str(party_id)},
        filename=f"statement-{str(party_id)[:8]}.xlsx",
    )


@router.get("/cashbook.xlsx")
async def cashbook(
    user: CurrentUser,
    session: Session,
    account: Annotated[str, Query(pattern="^(cash|bank|both)$")] = "both",
    date_from: Since = None,
    date_to: Until = None,
) -> Response:
    """What moved through the cash box and the bank, in order, with a
    running balance. Reversed rows are shown and not counted."""
    return await _build(
        session,
        user,
        report_type="cashbook",
        start=date_from,
        end=date_to,
        filters={} if account == "both" else {"account": account},
        filename=f"cashbook-{account}-{_stamp(date_from, date_to)}.xlsx",
    )
